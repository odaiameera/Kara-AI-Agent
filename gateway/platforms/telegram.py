"""Telegram platform adapter for the Kara gateway.

STUDY GUIDE
-----------
* Registers Telegram command and message handlers with python-telegram-bot.
* Enforces user allow-list, splits long replies, shows typing indicator.
* Key concepts: ``async def`` handlers, ``asyncio.to_thread``, ``filters``, handler registration.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
import embeddings
import models
import session_db
from gateway import commands as gw_commands
from gateway import restart as gw_restart
from gateway import sessions as gw_sessions
from gateway.platforms import tg_format
from tools.scheduler_tools import (
    reset_scheduler_request_context,
    set_scheduler_request_context,
)

log = logging.getLogger("kara.gateway.telegram")
TELEGRAM_MAX_MESSAGE = 4096
# LEARN: Telegram's "typing" indicator auto-expires after ~5s, so a single
# send_chat_action isn't enough for a multi-step tool loop — it must be resent
# on an interval shorter than that expiry for as long as Kara is working.
TYPING_REFRESH_SECONDS = 4


def _is_allowed(user_id: int | None) -> bool:
    if user_id is None:
        return False
    allowed = config.telegram_allowed_user_ids()
    return bool(allowed) and user_id in allowed


def _split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


async def _reply(update: Update, text: str) -> None:
    if not update.message:
        return
    for chunk in _split_message(text):
        await update.message.reply_text(chunk)


async def _reply_rich(update: Update, text: str) -> None:
    """Send Kara's reply as Telegram HTML, converting from the model's Markdown.

    Each chunk falls back to plain text if Telegram rejects the HTML, so a
    formatting glitch degrades to a readable message instead of a failed send.
    """
    if not update.message:
        return
    for chunk in tg_format.split_source(text):
        html = tg_format.to_telegram_html(chunk)
        try:
            await update.message.reply_text(html, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            log.warning("HTML reply rejected (%s); sending plain text", e)
            await update.message.reply_text(chunk)


# LEARN: Handlers below split into a sync helper (blocking work: HTTP, SQLite,
# LLM calls) and an async wrapper that runs it via asyncio.to_thread. This keeps
# the event loop free so the bot can keep receiving updates while one is busy.


async def _typing_refresh_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            # LEARN: A dropped typing ping shouldn't kill the loop or the reply.
            log.debug("send_chat_action failed; will retry on next tick", exc_info=True)
        await asyncio.sleep(TYPING_REFRESH_SECONDS)


@contextlib.asynccontextmanager
async def _typing_indicator(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None):
    """Keep Telegram's "typing..." indicator alive for the whole ``async with`` block.

    Telegram expires the indicator after ~5s, so a background task resends it
    every TYPING_REFRESH_SECONDS while Kara's tool loop runs, then stops it.
    """
    if chat_id is None:
        yield
        return
    task = asyncio.create_task(_typing_refresh_loop(context, chat_id))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _start_reply(user_id: int) -> str:
    key = session_db.build_session_key("telegram", user_id)
    session = gw_sessions.get_session(key, channel="telegram")
    ollama = "ON" if embeddings.is_available() else "OFF (keyword fallback)"
    resumed = ""
    if len(session_db.load_messages(key)) > 1:
        resumed = "\n(Resumed previous conversation.)"
    return (
        f"Kara online.\nProvider: {session.provider_name}\nModel: {session.model_name}\n"
        f"Semantic memory: {ollama}{resumed}\n"
        "Commands: /providers  /provider <id>  /models  /model  /model <provider>/<model>  /new  /restart"
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    await _reply(update, await asyncio.to_thread(_start_reply, user.id))


async def models_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    # LEARN: format_models_list makes HTTP calls to every provider — never on the loop.
    chat = update.effective_chat
    async with _typing_indicator(context, chat.id if chat else None):
        reply = await asyncio.to_thread(models.format_models_list)
    await _reply(update, reply)


async def providers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    key = session_db.build_session_key("telegram", user.id)
    session = gw_sessions.get_session(key, channel="telegram")
    cmd = "/provider" if not context.args else f"/provider {' '.join(context.args)}"
    await _reply(update, await asyncio.to_thread(gw_commands.handle_command, session, cmd) or "")


def _model_reply(user_id: int, cmd: str) -> str:
    key = session_db.build_session_key("telegram", user_id)
    session = gw_sessions.get_session(key)
    return gw_commands.handle_command(session, cmd) or ""


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    cmd = "/model" if not context.args else f"/model {' '.join(context.args)}"
    await _reply(update, await asyncio.to_thread(_model_reply, user.id, cmd))


def _new_session_reply(user_id: int) -> str:
    key = session_db.build_session_key("telegram", user_id)
    session = gw_sessions.new_session(key)
    return f"New session started. Model: {session.model_name}"


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    # LEARN: new_session runs an LLM summary of the old session — can take seconds.
    chat = update.effective_chat
    async with _typing_indicator(context, chat.id if chat else None):
        reply = await asyncio.to_thread(_new_session_reply, user.id)
    await _reply(update, reply)


def _restart_reply(user_id: int, chat_id: int | None) -> str:
    key = session_db.build_session_key("telegram", user_id)
    session = gw_sessions.get_session(key)
    if chat_id is not None:
        gw_restart.queue_restart_notification(chat_id)
    return gw_commands.handle_command(session, "/restart") or "Restarting..."


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    chat = update.effective_chat
    await _reply(
        update,
        await asyncio.to_thread(_restart_reply, user.id, chat.id if chat else None),
    )


async def auth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    cmd = "/auth" if not context.args else f"/auth {' '.join(context.args)}"
    key = session_db.build_session_key("telegram", user.id)
    session = gw_sessions.get_session(key, channel="telegram")
    await _reply(update, gw_commands.handle_command(session, cmd) or "")


async def codex_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return
    key = session_db.build_session_key("telegram", user.id)
    session = gw_sessions.get_session(key, channel="telegram")
    await _reply(update, gw_commands.handle_command(session, "/codex-status") or "")


def _chat_reply(user_id: int, chat_id: int | None, text: str) -> tuple[str, bool]:
    # LEARN: Returns (reply, is_rich). Slash commands are plain, terse output;
    # only Kara's LLM answers carry Markdown worth rendering as rich HTML.
    token = None
    if chat_id is not None:
        token = set_scheduler_request_context(
            platform="telegram", chat_id=chat_id, user_id=user_id
        )
    try:
        key = session_db.build_session_key("telegram", user_id)
        session = gw_sessions.get_session(key, channel="telegram")
        cmd_result = gw_commands.handle_command(session, text)
        if cmd_result is not None:
            if text.lower() == "/restart" and chat_id is not None:
                gw_restart.queue_restart_notification(chat_id)
            return cmd_result, False
        return session.handle_message(text), True
    finally:
        if token is not None:
            reset_scheduler_request_context(token)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return
    if not _is_allowed(user.id):
        await _reply(update, "Unauthorized.")
        return

    text = update.message.text.strip()
    chat = update.effective_chat

    try:
        async with _typing_indicator(context, chat.id if chat else None):
            # LEARN: to_thread runs blocking sync code (session setup + tool loop) in a
            # thread pool so the async event loop isn't blocked while Kara thinks.
            reply, is_rich = await asyncio.to_thread(
                _chat_reply, user.id, chat.id if chat else None, text
            )
        if is_rich:
            await _reply_rich(update, reply)
        else:
            await _reply(update, reply)
    except Exception as e:
        log.exception("Error handling message")
        await _reply(update, f"Error: {e}")


def register_handlers(app: Application) -> None:
    # LEARN: Handlers match update types — CommandHandler for /commands, MessageHandler for plain text.
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("models", models_cmd))
    app.add_handler(CommandHandler("providers", providers_cmd))
    app.add_handler(CommandHandler("provider", providers_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CommandHandler("auth", auth_cmd))
    app.add_handler(CommandHandler("codex_status", codex_status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
