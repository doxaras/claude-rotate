#!/usr/bin/env bash
# File-based setup for claude-rotate. No database, no cloud — everything lives
# in config.json + tokens/ next to this script.
#
#   ./setup.sh                       first-time init (creates config.json, dirs)
#   ./setup.sh add-device <name>     mint a device key and register it
#   ./setup.sh add-account <name>    store a `claude setup-token` for an account
#   ./setup.sh onboard <user> <dev>  team flow: mint <user>-<dev> key, restart the
#                                    service, print a paste-ready welcome block
#                                    (uses config.json "public_url" when set)
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p tokens logs
chmod 700 tokens

if [ ! -f config.json ]; then
    cp config.example.json config.json
    chmod 600 config.json
    echo "created config.json from config.example.json — edit accounts/devices or use the subcommands below"
fi

case "${1:-init}" in
init)
    echo "ready. next steps:"
    echo "  ./setup.sh add-account acct-1   # paste a token from: claude setup-token"
    echo "  ./setup.sh add-device my-laptop"
    echo "  python3 rotator.py"
    ;;
add-device)
    name="${2:?usage: ./setup.sh add-device <name>}"
    key="dev-${name}-$(openssl rand -hex 8)"
    python3 - "$name" "$key" <<'EOF'
import json, sys
cfg = json.load(open("config.json"))
cfg.setdefault("devices", {})
cfg["devices"] = {k: v for k, v in cfg["devices"].items() if "REPLACE-WITH" not in v}
cfg["devices"][sys.argv[1]] = sys.argv[2]
json.dump(cfg, open("config.json", "w"), indent=2)
EOF
    url=$(python3 -c "import json; print(json.load(open('config.json')).get('public_url') or 'http://<this-host>:8484')")
    echo "device '$name' registered. On that machine set:"
    echo "  export ANTHROPIC_BASE_URL=$url"
    echo "  export CLAUDE_CODE_OAUTH_TOKEN=$key"
    ;;
onboard)
    # Team flow: a member sends you their username + device name; you run
    #   ./setup.sh onboard <username> <device>
    # and paste the printed block back to them. Mints the key as
    # <username>-<device>, restarts the service so it's live immediately.
    user="${2:?usage: ./setup.sh onboard <username> <device>  (e.g. onboard maria laptop)}"
    dev="${3:?usage: ./setup.sh onboard <username> <device>  (e.g. onboard maria laptop)}"
    name="${user}-${dev}"
    if python3 -c "import json,sys; sys.exit(0 if '$name' in json.load(open('config.json')).get('devices', {}) else 1)"; then
        echo "error: device '$name' already exists — revoke it first (delete from config.json) or pick another name" >&2
        exit 1
    fi
    "$0" add-device "$name" > /dev/null
    key=$(python3 -c "import json; print(json.load(open('config.json'))['devices']['$name'])")
    url=$(python3 -c "import json; print(json.load(open('config.json')).get('public_url') or 'http://<this-host>:8484')")
    if command -v systemctl > /dev/null && systemctl is-active --quiet claude-rotate 2> /dev/null; then
        systemctl restart claude-rotate
        echo "(service restarted — key is live)"
    else
        echo "(restart rotator.py to activate the key)"
    fi
    echo
    echo "──── paste this back to $user ────────────────────────────────"
    echo "Hi $user — your Claude access for '$dev' is ready."
    echo
    echo "Add these two lines to your shell profile (~/.zshrc or ~/.bashrc):"
    echo
    echo "  export ANTHROPIC_BASE_URL=$url"
    echo "  export CLAUDE_CODE_OAUTH_TOKEN=$key"
    echo
    echo "Open a new terminal and run 'claude' as usual — no login needed."
    echo "Usage dashboard: $url/rotate/panel?key=$key"
    echo "The key is personal to this device — don't share or commit it."
    echo "──────────────────────────────────────────────────────────────"
    ;;
add-account)
    name="${2:?usage: ./setup.sh add-account <name>}"
    echo "Run 'claude setup-token' in a browser session logged into that account,"
    read -r -s -p "then paste the sk-ant-oat01-... token here (input hidden): " token
    echo
    case "$token" in sk-ant-oat01-*) ;; *) echo "error: token should start with sk-ant-oat01-" >&2; exit 1;; esac
    printf '%s' "$token" > "tokens/${name}.token"
    chmod 600 "tokens/${name}.token"
    python3 - "$name" <<'EOF'
import json, sys
cfg = json.load(open("config.json"))
if not any(a["name"] == sys.argv[1] for a in cfg["accounts"]):
    cfg["accounts"].append({"name": sys.argv[1], "token_file": f"tokens/{sys.argv[1]}.token"})
json.dump(cfg, open("config.json", "w"), indent=2)
EOF
    echo "account '$name' stored. Restart rotator.py to load it."
    ;;
*)
    echo "unknown subcommand: $1" >&2; exit 1 ;;
esac
