"""CLI entry point for Kara.

STUDY GUIDE
-----------
* Interactive command-line chat loop — read input, send to Kara, print replies.
* Delegates slash commands (/models, /new, etc.) to shared gateway command handlers.
* Wires a tool-call callback so you see when Kara uses memory or web tools.
* Key concepts: ``while True`` loops, ``input()``, nested functions, ``if __name__ == "__main__"``.
"""
from memory import embeddings
import config
from memory import context_budget
from gateway import commands as gw_commands
from kara import KaraSession, get_system_instruction
from tools import registry

CLI_SESSION_KEY = "kara:cli:local"


def main():
    # LEARN: KaraSession wraps the LLM + SQLite history; RuntimeError means missing API key or Ollama down.
    try:
        session = KaraSession(CLI_SESSION_KEY, channel="cli")
    except RuntimeError as e:
        print(e)
        return

    ollama_ok = embeddings.is_available()

    # LEARN: f-strings embed variables in strings; the ternary inside formats ON/OFF status.
    print("=========================================")
    print(" Kara - Personal AI Agent")
    print(" Ollama + Local Brain (core / learnings / sessions)")
    print(f" Brain: {config.BRAIN_DIR}")
    print(f" Provider: {session.provider_name} ({session.provider.id})")
    print(f" Model: {session.model_name}")
    print(
        f" Semantic memory: {'ON (' + config.EMBED_MODEL + ')' if ollama_ok else 'OFF (no reachable embed provider)'}"
    )
    print(f" Context window: {config.MODEL_CONTEXT_TOKENS} tokens")
    print(" Commands: /models  /model  /model <name>  /new  exit")
    print(" Gateway (24/7): uv run python -m gateway.run")
    print("=========================================")

    warning = context_budget.check_configured_window(
        get_system_instruction("cli"),
        registry.schemas_for_groups(set(registry.ALWAYS_ON)),
    )
    if warning:
        print(f"\n[!] {warning}")

    # LEARN: Infinite loop until user types exit/quit or presses Ctrl+D (EOFError).
    while True:
        try:
            user_input = input("\nYou: ")
            if user_input.lower() in ["exit", "quit"]:
                session.end_session()
                break

            # LEARN: handle_command returns None for normal chat text, or a string for slash commands.
            cmd_reply = gw_commands.handle_command(session, user_input)
            if cmd_reply is not None:
                print(f"\n{cmd_reply}")
                continue

            # LEARN: Nested function passed as callback — Kara calls this when the model invokes a tool.
            def on_tool(name: str, args: dict) -> None:
                print(f"\n  [Kara is using tool: {name}({args})]")

            reply = session.handle_message(user_input, on_tool_call=on_tool)
            print(f"\nAgent: {reply}")

        except EOFError:
            session.end_session()
            break
        except KeyboardInterrupt:
            print("\nBye!")
            break
        except Exception as e:
            print(f"\n[!] Unexpected Error: {e}")


# LEARN: This guard runs main() only when you execute ``python agent.py`` directly, not when imported.
if __name__ == "__main__":
    main()
