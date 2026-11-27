#!/usr/bin/env python3
"""File-based setup for claude-rotate. No database, no cloud — everything lives
in two files next to this script, split by who writes them:

    confidential.json  the secrets, written only by this script (chmod 600):
                       "accounts" name -> setup-token, "codex_accounts" name ->
                       CODEX_HOME, "devices" name -> device key
    config.json5       the settings, yours to edit (JSON5: comments allowed);
                       created from config.example.json5, which documents them

    ./setup.py                       first-time init (creates both files, dirs)
    ./setup.py add-device <name>     mint a device key and register it
    ./setup.py add-account <name>    store a `claude setup-token` for an account
    ./setup.py add-codex-account <n> device-login a ChatGPT plan for image generation
    ./setup.py onboard <user> <dev>  team flow: mint <user>-<dev> key, restart the
                                     service, print a paste-ready welcome block
                                     (uses config.json5 "public_url" when set)
    ./setup.py migrate               split an old single-file config.json into the two

Every subcommand migrates an old config.json first. add-account reads the token
from the terminal with echo off, or from stdin when stdin is not a terminal (e.g.
piped over ssh). Reading config.json5 (public_url) needs the json5 package from
requirements.txt; everything else is standard library.
"""
import argparse
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIDENTIAL = ROOT / "confidential.json"
SETTINGS = ROOT / "config.json5"
EXAMPLE = ROOT / "config.example.json5"
LEGACY = ROOT / "config.json"
LEGACY_BACKUP = ROOT / "config.json.migrated"
LEGACY_TOKENS = ROOT / "tokens"
LOGS = ROOT / "logs"
CODEX = ROOT / "codex"
CONFIDENTIAL_KEYS = ("accounts", "codex_accounts", "devices")
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


def load_confidential() -> dict:
    confidential = json.loads(CONFIDENTIAL.read_text())
    for key in CONFIDENTIAL_KEYS:
        confidential.setdefault(key, {})
    return confidential


def save_confidential(confidential: dict) -> None:
    write_private(CONFIDENTIAL, json.dumps(confidential, indent=2) + "\n")


def public_url() -> str:
    url = ""
    if SETTINGS.exists():
        try:
            import json5
        except ImportError:
            sys.exit("error: reading config.json5 needs the json5 package — "
                     "run: pip install -r requirements.txt")
        url = json5.loads(SETTINGS.read_text()).get("public_url") or ""
    return url or "http://<this-host>:8484"


def migrate() -> bool:
    """Split an old single-file config.json into confidential.json and config.json5,
    reading the account tokens from their token_file; per-account settings
    (priority, disabled) move to config.json5's account_options. config.json is
    renamed to config.json.migrated; tokens/ is left alone. Returns whether it ran."""
    if CONFIDENTIAL.exists() or not LEGACY.exists():
        return False
    if LEGACY_BACKUP.exists():
        sys.exit(f"error: {LEGACY_BACKUP.name} already exists — move it away, then run ./setup.py migrate")
    legacy = json.loads(LEGACY.read_text())
    options: dict = {}
    accounts = {}
    for acct in legacy.get("accounts") or []:
        name = acct["name"]
        extra = {k: v for k, v in acct.items() if k not in ("name", "token_file", "token")}
        if extra:
            options[name] = extra
        path = ROOT / acct["token_file"] if acct.get("token_file") else None
        if path and path.is_file():
            accounts[name] = path.read_text().strip()
        else:
            print(f"warning: account '{name}' has no token file ({acct.get('token_file')}) — "
                  f"not migrated; run: ./setup.py add-account {name}", file=sys.stderr)
    codex = {}
    for acct in legacy.get("codex_accounts") or []:
        codex[acct["name"]] = acct["codex_home"]
        extra = {k: v for k, v in acct.items() if k not in ("name", "codex_home")}
        if extra:
            options[acct["name"]] = extra
    settings = {k: v for k, v in legacy.items() if k not in CONFIDENTIAL_KEYS}
    if options:
        settings["account_options"] = {**(settings.get("account_options") or {}), **options}
    save_confidential({"accounts": accounts, "codex_accounts": codex,
                       "devices": legacy.get("devices") or {}})
    if SETTINGS.exists():
        print("config.json5 already exists — left as it is; the old settings stay in "
              f"{LEGACY_BACKUP.name}")
    else:
        SETTINGS.write_text(
            f"// Migrated from config.json by ./setup.py migrate on {time.strftime('%Y-%m-%d %H:%M')}.\n"
            "// config.example.json5 documents every key - copy its comments over as you like.\n"
            + json.dumps(settings, indent=2) + "\n")
    LEGACY.rename(LEGACY_BACKUP)
    print(f"migrated config.json: {len(accounts)} account(s), {len(codex)} codex account(s) and "
          f"{len(legacy.get('devices') or {})} device(s) -> confidential.json, the settings -> "
          "config.json5.")
    print(f"{LEGACY_BACKUP.name}" + (" and tokens/" if LEGACY_TOKENS.exists() else "")
          + " are no longer read; delete them once rotator.py runs.")
    return True


def prepare() -> bool:
    """Runs before every subcommand: the directories, a migration of an old
    config.json, and the two files when missing. Returns whether it migrated."""
    LOGS.mkdir(exist_ok=True)
    migrated = migrate()
    if not CONFIDENTIAL.exists():
        save_confidential({key: {} for key in CONFIDENTIAL_KEYS})
        print("created confidential.json (account tokens and device keys — fill it with the "
              "subcommands below)")
    if not SETTINGS.exists():
        SETTINGS.write_text(EXAMPLE.read_text())
        print("created config.json5 from config.example.json5 — edit the settings there")
    return migrated


def cmd_init(args: argparse.Namespace) -> int:
    print("ready. next steps:")
    print("  ./setup.py add-account acct-1   # paste a token from: claude setup-token")
    print("  ./setup.py add-device my-laptop")
    print("  python3 rotator.py")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    # prepare() has already run the migration if there was one to run.
    if args.migrated:
        pass
    elif not LEGACY.exists():
        print("nothing to migrate — confidential.json and config.json5 are in use")
    else:
        print("config.json is still here but confidential.json exists too — config.json is "
              "not read any more; delete it once you have checked confidential.json")
    return 0


def register_device(name: str) -> str:
    """Mint a key for `name`, store it (dropping any placeholder) and return it."""
    key = f"dev-{name}-{secrets.token_hex(8)}"
    confidential = load_confidential()
    devices = {k: v for k, v in confidential["devices"].items() if PLACEHOLDER not in v}
    devices[name] = key
    confidential["devices"] = devices
    save_confidential(confidential)
    return key


def cmd_add_device(args: argparse.Namespace) -> int:
    key = register_device(args.name)
    url = public_url()
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
    if name in load_confidential()["devices"]:
        print(f"error: device '{name}' already exists — revoke it first (delete it from "
              "confidential.json) or pick another name", file=sys.stderr)
        return 1
    url = public_url()
    key = register_device(name)
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
    confidential = load_confidential()
    confidential["accounts"][args.name] = token
    save_confidential(confidential)
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
    confidential = load_confidential()
    confidential["codex_accounts"].setdefault(args.name, f"codex/{args.name}")
    save_confidential(confidential)
    print(f"codex account '{args.name}' stored. Restart rotator.py to load it.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="File-based setup for claude-rotate (confidential.json + config.json5).")
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
    sub.add_parser("migrate", help="split an old config.json into confidential.json + config.json5"
                   ).set_defaults(func=cmd_migrate)
    parser.set_defaults(func=cmd_init)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    os.chdir(ROOT)
    args.migrated = prepare()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
