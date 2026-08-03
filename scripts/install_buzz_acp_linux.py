"""Install Kara's Buzz ACP harness as an owner-only systemd user service."""
from __future__ import annotations

import argparse
import json
import shutil
import shlex
import subprocess
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
UNIT_NAME = "kara-buzz-acp.service"


def ensure_stable_checkout(path: Path = PACKAGE_DIR) -> None:
    """Refuse to make a production service depend on Buzz's disposable scratch area."""
    if ".scratch" in path.resolve().parts:
        raise SystemExit(
            "Install Kara from a stable checkout outside .scratch; integrate and review "
            "the changes in the main repository first"
        )


def rendered_unit() -> tuple[str, str]:
    template = (PACKAGE_DIR / "deploy" / "systemd" / UNIT_NAME).read_text(encoding="utf-8")
    python = PACKAGE_DIR / ".venv" / "bin" / "python"
    buzz = shutil.which("buzz")
    buzz_acp = shutil.which("buzz-acp")
    if not python.is_file():
        raise SystemExit("Kara virtualenv is missing; run `uv sync` first")
    if not buzz or not buzz_acp:
        raise SystemExit("buzz and buzz-acp must both be installed and on PATH")
    service_path = ":".join(dict.fromkeys((str(Path(buzz).parent), str(Path(buzz_acp).parent))))
    unit = (
        template.replace("__KARA_REPO__", str(PACKAGE_DIR))
        .replace("__BUZZ_ACP__", buzz_acp)
        .replace("__SERVICE_PATH__", service_path)
        .replace("__KARA_PYTHON__", str(python))
        .replace("__KARA_IDENTITY_PREFLIGHT__", str(PACKAGE_DIR / "buzz_identity.py"))
    )
    env_example = (PACKAGE_DIR / "deploy" / "buzz.env.example").read_text(encoding="utf-8")
    env_example = (
        env_example.replace("__KARA_PYTHON__", str(python))
        .replace("__KARA_ACP_SERVER__", str(PACKAGE_DIR / "acp_server.py"))
    )
    return unit, env_example


def validate_auth_tag(value: str) -> None:
    """Validate the owner-attestation shape without logging its contents."""
    try:
        tag = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("BUZZ_AUTH_TAG must be valid compact JSON") from exc
    if not (
        isinstance(tag, list)
        and len(tag) == 4
        and tag[0] == "auth"
        and all(isinstance(item, str) for item in tag)
    ):
        raise ValueError("BUZZ_AUTH_TAG must be a four-string auth tag array")


def validate_config(path: Path) -> None:
    auth_tag = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith("BUZZ_AUTH_TAG="):
            continue
        parsed = shlex.split(line, comments=True, posix=True)
        if len(parsed) != 1:
            raise ValueError("BUZZ_AUTH_TAG must be one quoted EnvironmentFile value")
        auth_tag = parsed[0].partition("=")[2]
        break
    if auth_tag is None:
        raise ValueError("BUZZ_AUTH_TAG is missing from the EnvironmentFile")
    validate_auth_tag(auth_tag)


def install(dry_run: bool = False) -> Path:
    ensure_stable_checkout()
    config_dir = Path.home() / ".config" / "kara"
    user_unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = user_unit_dir / UNIT_NAME
    env_path = config_dir / "buzz.env"
    content, env_example = rendered_unit()
    if dry_run:
        return unit_path
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    user_unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    unit_path.write_text(content, encoding="utf-8")
    if not env_path.exists():
        env_path.write_text(env_example, encoding="utf-8")
    env_path.chmod(0o600)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Installed {unit_path}")
    print(f"Review {env_path}, then run: systemctl --user enable --now {UNIT_NAME}")
    return unit_path


def uninstall(dry_run: bool = False) -> Path:
    unit_path = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    if dry_run:
        return unit_path
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    unit_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Removed {unit_path}; credentials were preserved under ~/.config/kara")
    return unit_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    if args.validate_config:
        path = Path.home() / ".config" / "kara" / "buzz.env"
        validate_config(path)
        print(f"Validated {path} without printing secret values")
        return
    path = uninstall(args.dry_run) if args.uninstall else install(args.dry_run)
    if args.dry_run:
        print(path)


if __name__ == "__main__":
    main()
