#!/usr/bin/env python3
"""File-based setup for claude-rotate. No database, no cloud — everything lives
in config.json + tokens/ next to this script. Standard library only, so it runs
before `pip install -r requirements.txt`.

    ./setup.py                       first-time init (creates config.json, dirs)
    ./setup.py add-device <name>     mint a device key and register it
    ./setup.py add-account <name>    store a `claude setup-token` for an account
    ./setup.py add-codex-account <n> device-login a ChatGPT plan for image generation
    ./setup.py onboard <user> <dev>  team flow: mint <user>-<dev> key, restart the
                                     service, print a paste-ready welcome block
                                     (uses config.json "public_url" when set)

add-account reads the token from the terminal with echo off, or from stdin when
stdin is not a terminal (e.g. piped over ssh).
"""
import argparse
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
EXAMPLE = ROOT / "config.example.json"
TOKENS = ROOT / "tokens"
LOGS = ROOT / "logs"
CODEX = ROOT / "codex"
TOKEN_PREFIX = "sk-ant-oat01-"
PLACEHOLDER = "REPLACE-WITH"


def write_private(path: Path, text: str) -> None:
    """Write through a temporary file created 0600 and a rename: no reader ever
    sees half a file, and the secrets inside are never world-readable."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # O_CREAT's mode only applies to a new file
    tmp.replace(path)


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def save_config(cfg: dict) -> None:
    write_private(CONFIG, json.dumps(cfg, indent=2))


def public_url(cfg: dict) -> str:
    return cfg.get("public_url") or "http://<this-host>:8484"


def prepare() -> None:
    """Runs before every subcommand: the directories and a config.json."""
    TOKENS.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    os.chmod(TOKENS, 0o700)
    if not CONFIG.exists():
        write_private(CONFIG, EXAMPLE.read_text())
        print("created config.json from config.example.json — edit accounts/devices "
              "or use the subcommands below")


def cmd_init(args: argparse.Namespace) -> int:
    print("ready. next steps:")
    print("  ./setup.py add-account acct-1   # paste a token from: claude setup-token")
    print("  ./setup.py add-device my-laptop")
    print("  python3 rotator.py")
    return 0


def register_device(name: str) -> str:
    """Mint a key for `name`, store it (dropping the example's placeholder) and return it."""
    key = f"dev-{name}-{secrets.token_hex(8)}"
    cfg = load_config()
    devices = {k: v for k, v in cfg.get("devices", {}).items() if PLACEHOLDER not in v}
    devices[name] = key
    cfg["devices"] = devices
    save_config(cfg)
    return key


def cmd_add_device(args: argparse.Namespace) -> int:
    key = register_device(args.name)
    url = public_url(load_config())
    print(f"device '{args.name}' registered. On that machine set:")
    print(f"  export ANTHROPIC_BASE_URL={url}")
    print(f"  export CLAUDE_CODE_OAUTH_TOKEN={key}")
    return 0


def restart_service() -> str:
    """Restart the systemd service when it runs; say whether the new key is live."""
    if not shutil.which("systemctl"):
        return "(restart rotator.py to activate the key)"
    active = subprocess.run(["systemctl", "is-active", "--quiet", "claude-rotate"],
                            stderr=subprocess.DEVNULL)
    if active.returncode != 0:
        return "(restart rotator.py to activate the key)"
    # Non-root operators need sudo for the restart; -n so we fail into the hint
    # instead of hanging on a password prompt.
    for cmd in (["systemctl", "restart", "claude-rotate"],
                ["sudo", "-n", "systemctl", "restart", "claude-rotate"]):
        if subprocess.run(cmd, stderr=subprocess.DEVNULL).returncode == 0:
            return "(service restarted — key is live)"
    return "(key stored but NOT live — run: sudo systemctl restart claude-rotate)"


def cmd_onboard(args: argparse.Namespace) -> int:
    # Team flow: a member sends you their username + device name; you run
    #   ./setup.py onboard <username> <device>
    # and paste the printed block back to them. Mints the key as
    # <username>-<device>, restarts the service so it's live immediately.
    user, dev = args.username, args.device
    name = f"{user}-{dev}"
    if name in load_config().get("devices", {}):
        print(f"error: device '{name}' already exists — revoke it first (delete from "
              "config.json) or pick another name", file=sys.stderr)
        return 1
    key = register_device(name)
    url = public_url(load_config())
    print(restart_service())
    print()
    print(f"──── paste this back to {user} ────────────────────────────────")
    print(f"Hi {user} — your Claude access for '{dev}' is ready.")
    print()
    print("Add these two lines to your shell profile (~/.zshrc or ~/.bashrc):")
    print()
    print(f"  export ANTHROPIC_BASE_URL={url}")
    print(f"  export CLAUDE_CODE_OAUTH_TOKEN={key}")
    print()
    print("Open a new terminal and run 'claude' as usual — no login needed.")
    print(f"Usage dashboard: {url}/rotate/panel?key={key}")
    print("The key is personal to this device — don't share or commit it.")
    print("──────────────────────────────────────────────────────────────")
    return 0


def read_token() -> str:
    print("Run 'claude setup-token' in a browser session logged into that account,")
    prompt = "then paste the sk-ant-oat01-... token here (input hidden): "
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    print(prompt)
    return sys.stdin.readline().strip()


def cmd_add_account(args: argparse.Namespace) -> int:
    token = read_token()
    if not token.startswith(TOKEN_PREFIX):
        print(f"error: token should start with {TOKEN_PREFIX}", file=sys.stderr)
        return 1
    write_private(TOKENS / f"{args.name}.token", token)
    cfg = load_config()
    if not any(a["name"] == args.name for a in cfg["accounts"]):
        cfg["accounts"].append({"name": args.name, "token_file": f"tokens/{args.name}.token"})
        save_config(cfg)
    print(f"account '{args.name}' stored. Restart rotator.py to load it.")
    return 0


def cmd_add_codex_account(args: argparse.Namespace) -> int:
    # ChatGPT-plan account for /v1/images/generations. Codex has no long-lived
    # token on Plus/Pro, so each account gets its own CODEX_HOME with a device
    # login that Codex refreshes itself. Never copy another machine's auth.json:
    # both copies rotate the same refresh token and sign each other out.
    if not shutil.which("codex"):
        print("error: codex CLI not on PATH (npm i -g @openai/codex)", file=sys.stderr)
        return 1
    home = CODEX / args.name
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(CODEX, 0o700)
    os.chmod(home, 0o700)
    print("Turn on 'device code sign-in for Codex' in ChatGPT → Settings → Security first.")
    login = subprocess.run(["codex", "login", "--device-auth"],
                           env=dict(os.environ, CODEX_HOME=f"codex/{args.name}"), cwd=ROOT)
    if login.returncode != 0:
        return login.returncode
    cfg = load_config()
    cfg.setdefault("codex_accounts", [])
    if not any(a["name"] == args.name for a in cfg["codex_accounts"]):
        cfg["codex_accounts"].append({"name": args.name, "codex_home": f"codex/{args.name}"})
        save_config(cfg)
    print(f"codex account '{args.name}' stored. Restart rotator.py to load it.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="File-based setup for claude-rotate (config.json + tokens/).")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="first-time init (the default)").set_defaults(func=cmd_init)
    p = sub.add_parser("add-device", help="mint a device key and register it")
    p.add_argument("name")
    p.set_defaults(func=cmd_add_device)
    p = sub.add_parser("onboard", help="team flow: mint <user>-<dev>, restart, print a welcome block")
    p.add_argument("username")
    p.add_argument("device")
    p.set_defaults(func=cmd_onboard)
    p = sub.add_parser("add-account", help="store a `claude setup-token` for an account")
    p.add_argument("name")
    p.set_defaults(func=cmd_add_account)
    p = sub.add_parser("add-codex-account", help="device-login a ChatGPT plan for image generation")
    p.add_argument("name")
    p.set_defaults(func=cmd_add_codex_account)
    parser.set_defaults(func=cmd_init)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    os.chdir(ROOT)
    prepare()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
