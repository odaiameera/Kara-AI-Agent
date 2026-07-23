"""Launcher — CLI, gateway, update, or install.

STUDY GUIDE
-----------
* Single entry point that dispatches to CLI, gateway, update, or install based on argv.
* Uses lazy imports so only the chosen subcommand's code is loaded.
* Key concepts: ``sys.argv``, conditional imports, subcommand routing pattern.
"""
import sys


def main():
    # LEARN: sys.argv[0] is the script name; argv[1] is the first user argument (subcommand).
    if len(sys.argv) < 2:
        from agent import main as run

        run()
        return

    cmd = sys.argv[1].lower()
    # LEARN: Lazy import inside branches — faster startup and avoids loading Telegram unless needed.
    if cmd == "telegram" or cmd == "gateway":
        from gateway.run import main as run

        run()
    elif cmd == "update":
        from update import main as run

        run()
    elif cmd == "install":
        from install_gateway import main as run

        run()
    elif cmd == "uninstall":
        from install_gateway import uninstall

        uninstall()
    else:
        from agent import main as run

        run()


if __name__ == "__main__":
    main()
