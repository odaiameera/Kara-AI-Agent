"""Kara gateway — single long-lived process (Hermes-style).

"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.ext import Application

import config
from memory import context_budget
from scheduling import scheduler
from scheduling import runner as scheduled_runner
from memory import session_db
from gateway import restart as gw_restart
from gateway import sessions as gw_sessions
from gateway.platforms import telegram as telegram_adapter

log = logging.getLogger("kara.gateway")

def _setup_logging() -> None:
    config.ensure_brain()
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # python-telegram-bot uses httpx, whose INFO log includes the full request
    # URL. Telegram embeds the bot token in that URL, so INFO-level transport
    # logging would persist a live credential in gateway.log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # LEARN: Avoid duplicate handlers if main() is called twice in the same process.
    if sys.stdout is not None and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    log_path = str(config.GATEWAY_LOG)
    if not any(
        isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == log_path
        for h in root.handlers
    ):
        # LEARN: RotatingFileHandler caps log size (2MB) and keeps 3 backup files.
        file_handler = RotatingFileHandler(
            config.GATEWAY_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

def _warn_on_context_budget() -> None:
    """Surface a context window too small to hold Kara's own prompt."""
    import kara

    warning = context_budget.check_configured_window(
        kara.get_system_instruction("telegram"),
        kara.registry.schemas_for_groups(set(kara.registry.ALWAYS_ON)),
    )
    if warning:
        log.warning("%s", warning)

def _refresh_fingerprint() -> None:
    gw_restart.save_fingerprint(gw_restart.compute_code_fingerprint())

async def _restart_monitor(app: Application) -> None:
    # LEARN: async def + await asyncio.sleep — cooperative polling without blocking the event loop.
    # File hashing is sync disk I/O, so it runs in a worker thread via to_thread.
    await asyncio.to_thread(_refresh_fingerprint)
    started = time.time()
    grace = float(os.getenv("GATEWAY_CODE_GRACE", "30"))
    while True:
        await asyncio.sleep(gw_restart.POLL_INTERVAL)
        code_changed = await asyncio.to_thread(gw_restart.code_updated)
        if code_changed and (time.time() - started) < grace:
            # Keep the original fingerprint during startup grace. Saving the new
            # fingerprint here would mark code imported after process startup as
            # loaded even though this process still has the old modules in memory.
            # Once grace expires, the same pending change triggers one restart.
            continue
        if not (gw_restart.restart_requested() or code_changed):
            continue
        reason = "code update" if code_changed else "restart flag"
        log.info("Gateway restart triggered (%s)", reason)
        gw_sessions.shutdown_all()
        if gw_restart.claim_restart_leadership():
            gw_restart.clear_restart_flag()
            gw_restart.save_fingerprint(gw_restart.compute_code_fingerprint())
            gw_restart.release_instance_lock()
            gw_restart.release_restart_leadership()
            gw_restart.spawn_replacement()
            log.info("Replacement spawned; exiting pid %s.", os.getpid())
        else:
            log.info("Another instance is leading the restart; exiting pid %s.", os.getpid())
        # LEARN: app.stop() from a background task leaves run_polling() in a zombie state
        # on Windows — post_shutdown never runs and no replacement starts. Hard-exit instead.
        os._exit(0)

async def _post_init(app: Application) -> None:
    # LEARN: create_task schedules a coroutine to run concurrently in the background.
    asyncio.create_task(_restart_monitor(app))
    asyncio.create_task(_send_online_notifications(app))
    asyncio.create_task(scheduled_runner.scheduler_loop(app.bot))

async def _send_online_notifications(app: Application) -> None:
    """Ping Telegram chats that requested a restart once polling is live."""
    chat_ids = gw_restart.consume_restart_notifications()
    if not chat_ids:
        return
    # LEARN: Brief pause so the bot finishes connecting before we send the ping.
    await asyncio.sleep(2)
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text="Gateway back online.")
            log.info("Sent online notification to chat %s", chat_id)
        except Exception as e:
            log.warning("Failed to send online notification to %s: %s", chat_id, e)

async def _post_shutdown(_app: Application) -> None:
    gw_sessions.shutdown_all()
    gw_restart.release_restart_leadership()
    gw_restart.release_instance_lock()
    gw_restart.clear_pid()
    log.info("Gateway stopped.")

def validate_config() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
    if not config.telegram_allowed_user_ids():
        raise SystemExit(
            "Set TELEGRAM_ALLOWED_USER_IDS in .env (message @userinfobot for your id)."
        )

def main() -> None:
    _setup_logging()
    log.info("Kara gateway booting (pid %s)", os.getpid())
    validate_config()
    session_db.init_db()
    scheduler.init_db()
    gw_restart.release_restart_leadership()

    if not gw_restart.acquire_instance_lock():
        log.error("Another Kara gateway is already running; exiting.")
        sys.exit(0)

    gw_restart.write_pid()

    if config.RESTART_NOTIFY_FILE.exists():
        time.sleep(float(os.getenv("GATEWAY_SPAWN_DELAY", "2")))

    if gw_restart.restart_requested():
        gw_restart.clear_restart_flag()
    gw_restart.save_fingerprint(gw_restart.compute_code_fingerprint())

    log.info("Kara gateway starting")
    log.info("Brain: %s", config.BRAIN_DIR)
    log.info("Logs: %s", config.GATEWAY_LOG)
    log.info("Context window: %s tokens", config.MODEL_CONTEXT_TOKENS)
    _warn_on_context_budget()

    # LEARN: Builder pattern chains .token().post_init().build() to configure the Telegram app.
    # concurrent_updates is required for /stop to work at all: with the default
    # sequential processing, a /stop sent during a long turn would not be
    # dispatched until that turn finished — exactly when it is no longer useful.
    # gateway.sessions serializes turns per user with a lock, so concurrency here
    # does not let two messages race the same session.
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    telegram_adapter.register_handlers(app)

    log.info("Telegram adapter online — polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
