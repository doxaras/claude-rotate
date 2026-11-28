"""claude-rotate Phase 1: account-rotating auth proxy for Claude Code (Max subscriptions).

Devices point at this proxy and authenticate with an internal device key:

    export ANTHROPIC_BASE_URL=http://my-server:8484   # over tailscale
    export CLAUDE_CODE_OAUTH_TOKEN=<device key from confidential.json>

(or, to keep the device's own claude.ai login, ANTHROPIC_CUSTOM_HEADERS=
"X-Rotate-Device-Key: <device key>" instead of CLAUDE_CODE_OAUTH_TOKEN).

Per request, the proxy:
  1. maps the device key -> device name (401 if unknown),
  2. swaps in the active account's real setup-token,
  3. forwards to api.anthropic.com (transparent, incl. SSE streaming),
  4. records usage + account utilization (from anthropic-ratelimit-unified-*
     headers) to logs/audit.jsonl and state.json,
  5. rotates accounts: consume-first by default (burn the account whose weekly
     window resets soonest — use-it-or-lose-it), with cooldown + hysteresis so
     accounts never ping-pong at the threshold,
  6. tells spent quota apart from per-minute burst 429s — a burst paces the
     same account (keeps the warm prompt cache) instead of rotating,
  7. optionally holds a request open until the soonest window reset when every
     account is spent (hold_max_s), so unattended runs finish on their own.

GET /rotate/status (device-key auth) returns live state for the admin page;
GET /rotate/panel serves that page, whose HTML, CSS and JS live in panel/.
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import ClassVar, TypeVar

import httpx
import json5
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import codex_image
from confidential import CONFIDENTIAL_SECTIONS, read_confidential
from oai_compat import SseToOai, anthropic_to_oai, oai_to_anthropic

ROOT = Path(__file__).parent
# Two files, split by who writes them: confidential.json holds the secrets and is
# written by setup.py (account tokens, codex homes, device keys); config.json5 holds
# the settings and is edited by hand (JSON5, so it can carry comments).
CONFIDENTIAL_PATH = ROOT / "confidential.json"
SETTINGS_PATH = ROOT / "config.json5"
LEGACY_CONFIG_PATH = ROOT / "config.json"  # the single-file layout; ./setup.py migrate splits it
STATE_PATH = ROOT / "state.json"
AUDIT_PATH = ROOT / "logs" / "audit.jsonl"

UPSTREAM = "https://api.anthropic.com"
# Alternative to sending the device key as the bearer token; see device_from_request.
DEVICE_KEY_HEADER = "x-rotate-device-key"
SESSION_HEADER = "x-claude-code-session-id"  # logged as the audit record's session_id
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
    DEVICE_KEY_HEADER,  # the device key never goes upstream
}

app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=15.0))
state_lock = asyncio.Lock()


T = TypeVar("T")


def from_json(cls: type[T], data: dict, where: str) -> T:
    """Dataclass `cls` from its JSON object. A key it has no field for is
    dropped with a warning: a misspelt config.json5 key, or a file from another
    version, must not stop the proxy from starting."""
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        print(f"warning: ignoring unknown key(s) in {where}: {', '.join(unknown)}",
              file=sys.stderr)
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class Account_config:
    """A Claude account: its setup-token from confidential.json and its
    account_options from config.json5."""
    name: str
    token: str
    priority: int = 100  # lower = preferred tier
    disabled: bool = False  # benched: never picked, left at once when active
    switch_threshold: float | None = None  # None = the global switch_threshold
    switch_threshold_7d: float | None = None  # None = the global switch_threshold_7d


@dataclass(slots=True)
class Alerts_config:
    """config.json5's "alerts": when the panel warns."""
    device_tokens_per_hour: int = 5_000_000  # one device's tokens in the last hour
    expensive_model_patterns: list[str] = field(
        default_factory=lambda: ["opus", "fable", "mythos"])  # model id substrings
    util_warn: float = 0.8  # an account's 5h utilization


@dataclass(slots=True)
class Codex_config:
    """config.json5's "codex": image generation through the Codex CLI."""
    bin: str = "codex"
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "low"
    timeout_s: float = 300  # per image
    concurrency: int = 2  # images generated at once


# API list prices per MTok (input, output), matched by substring of the model id.
# Cost columns are *API-equivalent* dollars: what this usage would cost pay-per-token.
# config.json5's "prices_per_mtok" replaces this table as a whole.
DEFAULT_PRICES = {
    "fable": [10.0, 50.0], "mythos": [10.0, 50.0],
    "opus": [5.0, 25.0],
    "sonnet": [3.0, 15.0],
    "haiku": [1.0, 5.0],
}


@dataclass(slots=True)
class Config:
    """The settings from config.json5, every one optional, with the accounts and
    devices from confidential.json. config.example.json5 documents the settings."""
    accounts: list[Account_config]  # only those with a token
    codex_accounts: list[codex_image.Codex_account_config]  # only those signed in
    devices: dict[str, str]  # device name -> device key
    bind: str = "127.0.0.1"
    port: int = 8484
    public_url: str = ""  # read by setup.py
    switch_threshold: float = 0.8
    switch_threshold_7d: float = 0.98
    strategy: str = "consume-first"  # or "least-used"
    switch_cooldown_s: float = 300
    switch_margin: float = 0.05
    consume_first_margin_s: float = 3600
    hold_max_s: float = 0  # 0 = return 429 when every account is spent
    prices_per_mtok: dict[str, list[float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    alerts: Alerts_config = field(default_factory=Alerts_config)
    codex: Codex_config = field(default_factory=Codex_config)


def load_config() -> Config:
    """config.json5 and confidential.json as one Config. account_options are
    merged into the accounts they name."""
    if not CONFIDENTIAL_PATH.exists():
        if LEGACY_CONFIG_PATH.exists():
            raise SystemExit("config.json is the old single-file layout — run: ./setup.py migrate")
        raise SystemExit("no confidential.json — run: ./setup.py, then add-account and add-device")
    confidential = read_confidential(CONFIDENTIAL_PATH)
    cfg = json5.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
    misplaced = [k for k in CONFIDENTIAL_SECTIONS if k in cfg]
    if misplaced:
        raise SystemExit(f"config.json5 has {', '.join(misplaced)} — those live in "
                         "confidential.json (written by ./setup.py); per-account settings go "
                         "under account_options")
    options = cfg.pop("account_options", None) or {}
    unknown = set(options) - set(confidential.accounts) - set(confidential.codex_accounts)
    if unknown:
        print(f"warning: account_options for unknown account(s): {', '.join(sorted(unknown))}",
              file=sys.stderr)
    loaded = []
    for name, token in confidential.accounts.items():
        if isinstance(token, str) and token.strip():
            loaded.append(from_json(Account_config,
                                    {**options.get(name, {}), "name": name, "token": token.strip()},
                                    f"config.json5 account_options {name!r}"))
        else:
            print(f"warning: skipping account '{name}' — no token in confidential.json "
                  f"(run: ./setup.py add-account {name})", file=sys.stderr)
    codex = []
    for name, codex_home in confidential.codex_accounts.items():
        home = Path(codex_home).expanduser()
        if not home.is_absolute():
            home = ROOT / home
        if (home / "auth.json").exists():
            codex.append(from_json(codex_image.Codex_account_config,
                                   {**options.get(name, {}), "name": name, "codex_home": str(home)},
                                   f"config.json5 account_options {name!r}"))
        else:
            print(f"warning: skipping codex account '{name}' — no auth.json in "
                  f"{home} (run: ./setup.py add-codex-account {name})",
                  file=sys.stderr)
    devices = confidential.devices
    if not loaded and not codex:
        raise SystemExit("no accounts with tokens configured — run: ./setup.py add-account <name>")
    # The old example config shipped a placeholder device key that is PUBLIC (it's
    # in the repo's history). It must never be accepted as a live credential:
    # refuse to start while one remains, so a migrated install that never ran
    # add-device can't open the proxy to anyone who read the example (GitHub issue #2).
    bad = [d for d, k in devices.items() if "REPLACE-WITH" in k]
    if bad:
        raise SystemExit(
            f"placeholder device key(s) in confidential.json: {', '.join(bad)} — the example "
            "value is public and must never be a valid credential. "
            "Run: ./setup.py add-device <name> (it replaces the placeholder).")
    if "alerts" in cfg:
        cfg["alerts"] = from_json(Alerts_config, cfg["alerts"] or {}, "config.json5 alerts")
    if "codex" in cfg:
        cfg["codex"] = from_json(Codex_config, cfg["codex"] or {}, "config.json5 codex")
    return from_json(Config, {**cfg, "accounts": loaded, "codex_accounts": codex,
                              "devices": devices}, "config.json5")


CFG = load_config()
DEVICE_BY_KEY = {v: k for k, v in CFG.devices.items()}
ACCOUNTS = {a.name: a for a in CFG.accounts}
THRESHOLD = CFG.switch_threshold
THRESHOLD_7D = CFG.switch_threshold_7d
STRATEGY = CFG.strategy
COOLDOWN_S = CFG.switch_cooldown_s
SWITCH_MARGIN = CFG.switch_margin
CF_MARGIN_S = CFG.consume_first_margin_s
HOLD_MAX_S = CFG.hold_max_s
CODEX_ACCOUNTS = {a.name: a for a in CFG.codex_accounts}
CODEX_CFG = CFG.codex
# Each image is a `codex exec` process (~40s); cap how many run at once.
codex_slots = asyncio.Semaphore(CODEX_CFG.concurrency)


@dataclass(slots=True)
class Account:
    """What a Claude account's last replies said (the anthropic-ratelimit-unified-*
    headers). An account with no reply yet has the defaults: nothing used, no
    known reset."""
    util_5h: float = 0.0
    util_7d: float = 0.0
    reset_5h: float | None = None  # epoch seconds
    reset_7d: float | None = None
    status: str | None = None  # "allowed", "rejected", ...
    last_seen: float | None = None
    pace_until: float = 0.0  # a per-minute burst 429 holds requests back until then


@dataclass(slots=True)
class Event:
    """One switch or hold, for /rotate/status. In JSON the two accounts are
    "from" and "to" (`from` is a Python keyword)."""
    ts: str
    type: str  # "switch" or "hold"
    from_account: str
    to_account: str
    reason: str
    util: float | None = None  # a switch: the 5h utilization of the account it left

    JSON_KEYS: ClassVar[dict[str, str]] = {"from_account": "from", "to_account": "to"}

    @classmethod
    def from_json(cls, data: dict) -> "Event":
        field_names = {v: k for k, v in cls.JSON_KEYS.items()}
        return from_json(cls, {field_names.get(k, k): v for k, v in data.items()},
                         "a state.json event")

    def to_json(self) -> dict:
        return {self.JSON_KEYS.get(k, k): v for k, v in asdict(self).items()}


@dataclass(slots=True)
class State:
    """What the proxy keeps across restarts; persisted as state.json."""
    active_account: str | None = None
    accounts: dict[str, Account] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)  # newest last, the last 200 kept
    last_switch_ts: float = 0
    codex: dict[str, codex_image.Codex_account] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> "State":
        state = from_json(cls, data, "state.json")
        state.accounts = {name: from_json(Account, info, f"state.json account {name!r}")
                          for name, info in state.accounts.items()}
        events = []
        for event in state.events:
            try:
                events.append(Event.from_json(event))
            except TypeError as e:  # a required field missing
                print(f"warning: dropping a state.json event ({e}): {event}", file=sys.stderr)
        state.events = events
        state.codex = {name: from_json(codex_image.Codex_account, info,
                                       f"state.json codex account {name!r}")
                       for name, info in state.codex.items()}
        return state

    def to_json(self) -> dict:
        data = asdict(self)
        data["events"] = [e.to_json() for e in self.events]
        return data


def load_state() -> State:
    if not STATE_PATH.exists():
        return State(active_account=CFG.accounts[0].name if CFG.accounts else None,
                     accounts={a.name: Account() for a in CFG.accounts})
    return State.from_json(json.loads(STATE_PATH.read_text()))


STATE = load_state()


def save_state() -> None:
    # Write-then-rename so a crash mid-write can never leave a truncated
    # state.json (which would refuse to parse on the next start).
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(STATE.to_json(), indent=2))
    tmp.replace(STATE_PATH)


def audit(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def utilization(name: str) -> float:
    info = STATE.accounts.get(name, Account())
    now = time.time()
    # A window that has reset no longer counts against the account.
    if info.reset_5h and now >= info.reset_5h:
        return 0.0
    return info.util_5h


def util_7d(name: str) -> float:
    info = STATE.accounts.get(name, Account())
    if info.reset_7d and time.time() >= info.reset_7d:
        return 0.0
    return info.util_7d


def priority(name: str) -> int:
    """Lower = preferred. Accounts without a priority share the default tier."""
    return ACCOUNTS[name].priority


def disabled(name: str) -> bool:
    return ACCOUNTS[name].disabled


def threshold(name: str) -> float:
    """The 5h utilization at which this account is left: its own
    switch_threshold from account_options, else the global one."""
    own = ACCOUNTS[name].switch_threshold
    return THRESHOLD if own is None else own


def threshold_7d(name: str) -> float:
    """The weekly utilization that makes this account unusable: its own
    switch_threshold_7d from account_options, else the global one."""
    own = ACCOUNTS[name].switch_threshold_7d
    return THRESHOLD_7D if own is None else own


def usable(name: str) -> bool:
    return (not disabled(name)
            and utilization(name) < threshold(name) and util_7d(name) < threshold_7d(name))


WEEK_S = 7 * 24 * 3600


def weekly_reset(name: str) -> float:
    """The account's next weekly reset. A stored reset that has already passed
    is rolled forward by whole weeks: the new window's reset is only learned
    from the account's next reply, and until then the stale value would look
    like the soonest reset of all and draw a consume-first switch onto an
    account whose week has just started."""
    reset = STATE.accounts.get(name, Account()).reset_7d
    if not reset:
        return float("inf")
    now = time.time()
    if reset <= now:
        reset += ((now - reset) // WEEK_S + 1) * WEEK_S
    return reset


def relief_ts(name: str, now: float) -> float:
    """When this account is next usable: `now`, or the later of its blocking
    windows' resets. A week-exhausted account isn't helped by a 5h reset; an
    exhausted window with no known reset never clears (7d) or clears now (5h)."""
    info = STATE.accounts.get(name, Account())
    t = now
    if utilization(name) >= threshold(name):
        t = max(t, info.reset_5h or now)
    if util_7d(name) >= threshold_7d(name):
        t = max(t, info.reset_7d or float("inf"))
    return t


def earliest_relief() -> float:
    """Soonest moment any non-disabled account becomes usable again (relief_ts),
    for hold-until-reset and Retry-After; 5 minutes out if no account has a
    known reset ahead."""
    now = time.time()
    pool = [n for n in ACCOUNTS if not disabled(n)] or list(ACCOUNTS)
    reliefs = [t for t in (relief_ts(n, now) for n in pool) if now < t < float("inf")]
    return min(reliefs) if reliefs else now + 300


def pick_account() -> str:
    """Usable = not disabled, both windows under their thresholds (a passed
    reset counts as 0). Priority tiers come first: a lower `priority` account
    always beats a higher one, and the strategy orders accounts *within* a tier.

    consume-first (default): burn the usable account whose weekly window resets
    soonest — weekly quota is use-it-or-lose-it, so spend the perishable one.
    least-used: lowest 5h utilization.
    If nothing is usable, the non-disabled account that becomes usable soonest
    (relief_ts) -- never a week-exhausted account just because its 5h window
    reset long ago.
    """
    under = [n for n in ACCOUNTS if usable(n)]
    if under:
        if STRATEGY == "consume-first":
            return min(under, key=lambda n: (priority(n), weekly_reset(n), utilization(n)))
        return min(under, key=lambda n: (priority(n), utilization(n)))
    pool = [n for n in ACCOUNTS if not disabled(n)] or list(ACCOUNTS)
    now = time.time()
    return min(pool, key=lambda n: (relief_ts(n, now), priority(n)))


async def note_response(account: str, headers: httpx.Headers, status: int) -> str | None:
    """Update account state from unified rate-limit headers; maybe switch.

    Returns what happened: "burst" (per-minute 429, paced — no rotation),
    "switched" (active account changed, retry can ride it), "exhausted"
    (quota gone and nowhere better to go), or None.
    """
    def _f(h):
        v = headers.get(h)
        return float(v) if v is not None else None

    async with state_lock:
        info = STATE.accounts.setdefault(account, Account())
        for key, header in (
            ("util_5h", "anthropic-ratelimit-unified-5h-utilization"),
            ("util_7d", "anthropic-ratelimit-unified-7d-utilization"),
            ("reset_5h", "anthropic-ratelimit-unified-5h-reset"),
            ("reset_7d", "anthropic-ratelimit-unified-7d-reset"),
        ):
            val = _f(header)
            if val is not None:
                setattr(info, key, val)
        if headers.get("anthropic-ratelimit-unified-status"):
            info.status = headers["anthropic-ratelimit-unified-status"]
        now = time.time()
        info.last_seen = now

        quota_hit = info.status == "rejected" or (
            status == 429 and (utilization(account) >= 1.0 or util_7d(account) >= 1.0))
        if status == 429 and not quota_hit:
            # Per-minute burst limit, not spent quota. Rotating would just move
            # the burst to the next account and drop its warm prompt cache —
            # pace this account briefly instead.
            try:
                pace = min(float(headers.get("retry-after", "")), 60.0)
            except ValueError:
                pace = 15.0
            info.pace_until = now + pace
            save_state()
            return "burst"

        verdict = None
        if STATE.active_account != account:
            # Another request already rotated; a retry rides the new active.
            verdict = "switched" if quota_hit else None
        else:
            cooled = now - STATE.last_switch_ts >= COOLDOWN_S
            over = (utilization(account) >= threshold(account)
                    or util_7d(account) >= threshold_7d(account))
            nxt = pick_account()
            reason = None
            if nxt != account:
                if quota_hit:
                    reason = "quota_exhausted"  # hard stop: no cooldown, no margin
                elif disabled(account):
                    reason = "account_disabled"  # hard rule: leave immediately
                elif over and cooled:
                    # Hysteresis: only move to a meaningfully better account, so
                    # two accounts hovering at the line never ping-pong. A move
                    # to a preferred tier, or off a week-exhausted account,
                    # skips the margin.
                    if (util_7d(account) >= threshold_7d(account)
                            or priority(nxt) < priority(account)
                            or utilization(nxt) <= utilization(account) - SWITCH_MARGIN):
                        reason = f"utilization>={threshold(account)}"
                elif cooled and usable(nxt):
                    if priority(nxt) < priority(account):
                        # A preferred account has room again — pull traffic back.
                        reason = "priority_recovery"
                    elif (STRATEGY == "consume-first"
                          and weekly_reset(nxt) < weekly_reset(account) - CF_MARGIN_S):
                        # Use-it-or-lose-it: proactively jump to an account whose
                        # weekly window expires clearly sooner than the active one's.
                        reason = "consume_first"
            if reason:
                STATE.active_account = nxt
                STATE.last_switch_ts = now
                STATE.events.append(Event(
                    ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    type="switch",
                    from_account=account, to_account=nxt,
                    reason=reason,
                    util=utilization(account),
                ))
                STATE.events = STATE.events[-200:]
                verdict = "switched"
            elif quota_hit:
                verdict = "exhausted"
        save_state()
        return verdict


def usage_from_sse(text: str, sink: dict) -> None:
    for line in text.splitlines():
        if line.startswith("data:") and '"usage"' in line:
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage") or obj.get("message", {}).get("usage")
            if usage:
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                    if usage.get(k):
                        sink[k] = max(sink.get(k, 0), usage[k])


def summarize_usage(usage: dict | None) -> dict | None:
    if not usage:
        return None
    return {k: usage.get(k, 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


# Audit-record key -> the unified rate-limit header it copies (as the header's
# string, the way util_5h always was). With both windows' utilization and
# resets in every record, the audit log alone is a per-request time series of
# each account's quota - e.g. to see between which two requests the weekly
# percent stepped, and what those requests cost.
AUDIT_RATE_LIMIT_HEADERS = {
    "util_5h": "anthropic-ratelimit-unified-5h-utilization",
    "util_7d": "anthropic-ratelimit-unified-7d-utilization",
    "reset_5h": "anthropic-ratelimit-unified-5h-reset",
    "reset_7d": "anthropic-ratelimit-unified-7d-reset",
    "ratelimit_status": "anthropic-ratelimit-unified-status",
}


def rate_limit_fields(headers: httpx.Headers) -> dict:
    """The audit record's rate-limit fields: util_5h always (None without the
    header, as before), the others only when the response carried them."""
    fields = {key: headers.get(header) for key, header in AUDIT_RATE_LIMIT_HEADERS.items()}
    return {key: value for key, value in fields.items() if value is not None or key == "util_5h"}


@app.get("/rotate/status")
async def rotate_status(request: Request):
    if device_from_request(request) is None:
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    now = time.time()
    accounts = {}
    for name in ACCOUNTS:
        info = asdict(STATE.accounts.get(name, Account()))
        info["effective_util_5h"] = utilization(name)
        info["effective_util_7d"] = util_7d(name)
        info["priority"] = priority(name)
        info["disabled"] = disabled(name)
        info["switch_threshold"] = threshold(name)
        info["switch_threshold_7d"] = threshold_7d(name)
        if info.get("reset_5h"):
            info["reset_5h_in_min"] = max(0, round((info["reset_5h"] - now) / 60))
        if info.get("reset_7d"):
            info["reset_7d_in_h"] = max(0, round((info["reset_7d"] - now) / 3600, 1))
        accounts[name] = info
    return {
        "active_account": STATE.active_account,
        "switch_threshold": THRESHOLD,
        "switch_threshold_7d": THRESHOLD_7D,
        "strategy": STRATEGY,
        "hold_max_s": HOLD_MAX_S,
        "accounts": accounts,
        "codex_accounts": {
            name: dict(asdict(STATE.codex.get(name, codex_image.Codex_account())),
                       usable=codex_image.usable(name, CODEX_ACCOUNTS, STATE.codex,
                                                 THRESHOLD, THRESHOLD_7D, now),
                       switch_threshold=codex_image.account_threshold(
                           CODEX_ACCOUNTS[name], THRESHOLD),
                       switch_threshold_7d=codex_image.account_threshold_7d(
                           CODEX_ACCOUNTS[name], THRESHOLD_7D))
            for name in CODEX_ACCOUNTS},
        "recent_events": [e.to_json() for e in STATE.events[-10:]],
    }


def device_from_request(request: Request) -> str | None:
    # A device that keeps its own claude.ai login (so features that talk to
    # claude.ai directly - Claude in Chrome, claude.ai connectors, /usage - keep
    # working) sends its key in DEVICE_KEY_HEADER via ANTHROPIC_CUSTOM_HEADERS;
    # its Authorization then carries that login's token, which is never forwarded.
    key = request.headers.get(DEVICE_KEY_HEADER, "")
    if key:
        return DEVICE_BY_KEY.get(key)
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not key:
        key = request.query_params.get("key", "")
    return DEVICE_BY_KEY.get(key)


PRICES = CFG.prices_per_mtok  # see DEFAULT_PRICES
DEFAULT_PRICE = [5.0, 25.0]  # a model matching no pattern
ALERT_CFG = CFG.alerts


def price_for(model: str | None) -> list[float]:
    for pat, p in PRICES.items():
        if model and pat in model:
            return p
    return DEFAULT_PRICE


def api_cost(model: str | None, usage: dict) -> float:
    """API-equivalent $ for one request: in + out + cache read (0.1x) + cache write (1.25x)."""
    p_in, p_out = price_for(model)
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
    ) / 1_000_000


# Incremental tail of audit.jsonl: every stat aggregate_audit() serves is a
# rolling 24h window, so only that window is kept parsed in memory and each
# call reads just the bytes appended since the previous one — instead of
# re-parsing the whole (unbounded, append-only) file per panel view.
_AUDIT_CACHE: dict = {"offset": 0, "records": deque(), "sig": b""}


def _tail_audit() -> None:
    import datetime as dt

    cache = _AUDIT_CACHE
    if not AUDIT_PATH.exists():
        cache["offset"] = 0
        cache["sig"] = b""
        cache["records"].clear()
        return
    day_ago = time.time() - 86400
    with AUDIT_PATH.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(64)
        # A shrunken file, or changed leading bytes, means audit.jsonl was
        # rotated or rewritten out from under us — the cached window no
        # longer describes this file, so rebuild from the top. (Size alone
        # can't catch truncate-and-rewrite-bigger; the head fingerprint can.)
        if size < cache["offset"] or not head.startswith(cache["sig"]):
            cache["offset"] = 0
            cache["records"].clear()
        cache["sig"] = head
        if size == cache["offset"]:
            return
        f.seek(cache["offset"])
        chunk = f.read()
    # Consume complete lines only; a trailing partial line (an append in
    # flight from a writer outside this process) is left for the next call.
    end = chunk.rfind(b"\n") + 1
    cache["offset"] += end
    for line in chunk[:end].splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            rec["_ts"] = dt.datetime.strptime(
                rec.get("ts", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            continue
        if rec["_ts"] >= day_ago:
            _AUDIT_CACHE["records"].append(rec)


def aggregate_audit() -> dict:
    """Roll up the last 24h of audit.jsonl by device/model/account, with alerts."""
    now = time.time()
    hour_ago = now - 3600
    day_ago = now - 86400
    by_device: dict = {}
    by_model: dict = {}
    by_account: dict = {}
    device_hour_tokens: dict = {}
    device_hour_reqs: dict = {}
    device_meta: dict = {}
    expensive_hour: dict = {}

    def bump(bucket, key, usage, cost):
        b = bucket.setdefault(key, {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cache_read": 0, "cache_write": 0, "cost_usd": 0.0})
        b["requests"] += 1
        b["input_tokens"] += usage.get("input_tokens", 0)
        b["output_tokens"] += usage.get("output_tokens", 0)
        b["cache_read"] += usage.get("cache_read_input_tokens", 0)
        b["cache_write"] += usage.get("cache_creation_input_tokens", 0)
        b["cost_usd"] += cost

    _tail_audit()
    records = _AUDIT_CACHE["records"]
    while records and records[0]["_ts"] < day_ago:
        records.popleft()
    for rec in records:
        usage = rec.get("usage") or {}
        ts = rec["_ts"]
        cost = api_cost(rec.get("model"), usage)
        dev = rec.get("device", "?")
        meta = device_meta.setdefault(dev, {})
        if ts >= meta.get("last_ts", 0):
            meta.update({"last_ts": ts, "last_model": rec.get("model"),
                         "last_account": rec.get("account")})
        bump(by_device, rec.get("device", "?"), usage, cost)
        bump(by_model, rec.get("model") or "?", usage, cost)
        bump(by_account, rec.get("account", "?"), usage, cost)
        if ts >= hour_ago:
            total = sum(usage.get(k, 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens"))
            device_hour_tokens[rec.get("device", "?")] = (
                device_hour_tokens.get(rec.get("device", "?"), 0) + total)
            device_hour_reqs[dev] = device_hour_reqs.get(dev, 0) + 1
            model = rec.get("model") or ""
            if any(p in model for p in ALERT_CFG.expensive_model_patterns):
                key = f'{rec.get("device", "?")} / {model}'
                expensive_hour[key] = expensive_hour.get(key, 0) + 1

    alerts = []
    for dev, toks in device_hour_tokens.items():
        if toks > ALERT_CFG.device_tokens_per_hour:
            alerts.append({"level": "warn",
                           "text": f"{dev} consumed {toks:,} tokens in the last hour "
                                   f"(threshold {ALERT_CFG.device_tokens_per_hour:,})"})
    for key, n in expensive_hour.items():
        alerts.append({"level": "info", "text": f"expensive model in use: {key} — {n} requests in the last hour"})
    for name in ACCOUNTS:
        u = utilization(name)
        if u >= ALERT_CFG.util_warn:
            alerts.append({"level": "warn", "text": f"{name} 5h window at {u:.0%} (switch threshold {threshold(name):.0%})"})

    # Roster covers every registered device, traffic or not.
    devices = {}
    for name, key in CFG.devices.items():
        meta = device_meta.get(name, {})
        last_ts = meta.get("last_ts")
        devices[name] = {
            "key_suffix": "…" + key[-4:],
            "online": bool(last_ts and now - last_ts < 600),
            "last_seen_min": round((now - last_ts) / 60) if last_ts else None,
            "last_model": meta.get("last_model"),
            "last_account": meta.get("last_account"),
            "req_1h": device_hour_reqs.get(name, 0),
            "tokens_1h": device_hour_tokens.get(name, 0),
        }

    return {"window": "last 24h", "devices": devices, "by_device": by_device,
            "by_model": by_model, "by_account": by_account, "alerts": alerts}


@app.get("/rotate/stats")
async def rotate_stats(request: Request):
    if device_from_request(request) is None:
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    return aggregate_audit()


# The panel's page, style and script live in panel/ and are read per request,
# so editing them needs no restart. Only the page asks for a device key: the
# stylesheet and script hold no data — they fetch it from /rotate/status and
# /rotate/stats with the page's ?key=.
PANEL_DIR = ROOT / "panel"
PANEL_ASSETS = {"panel.css": "text/css", "panel.js": "text/javascript"}


@app.get("/rotate/panel")
async def rotate_panel(request: Request):
    if device_from_request(request) is None:
        return JSONResponse({"error": "unknown device key — open /rotate/panel?key=<device key>"},
                            status_code=401)
    return FileResponse(PANEL_DIR / "panel.html", media_type="text/html")


@app.get("/rotate/panel/{asset}")
async def rotate_panel_asset(asset: str):
    if asset not in PANEL_ASSETS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(PANEL_DIR / asset, media_type=PANEL_ASSETS[asset])


async def send_upstream(method: str, url: str, fwd_headers: dict, body: bytes):
    """The rotation loop shared by the transparent proxy and the OpenAI endpoint:
    pick the active account, pace bursts, retry through switches, optionally hold
    until the soonest window reset. Returns (upstream, account, retries, noted,
    verdict) — noted means note_response already ran for the returned response;
    verdict is note_response's word for the LAST 429 seen ("burst" / "switched" /
    "exhausted" / None)."""
    hold_deadline = time.time() + HOLD_MAX_S if HOLD_MAX_S else None
    retries = 0
    noted = False
    verdict = None
    while True:
        account = STATE.active_account
        fwd_headers["authorization"] = f"Bearer {ACCOUNTS[account].token}"
        pace = STATE.accounts.get(account, Account()).pace_until - time.time()
        if pace > 0:
            await asyncio.sleep(min(pace, 60))
        upstream = await client.send(
            client.build_request(method, url, headers=fwd_headers, content=body),
            stream=True)
        if upstream.status_code != 429:
            return upstream, account, retries, noted, verdict
        verdict = await note_response(account, upstream.headers, 429)
        wait = 0.0
        retry = verdict in ("switched", "burst") and retries < 5
        if not retry and verdict == "exhausted" and hold_deadline:
            # Every account is spent: hold the request open until the soonest
            # window reset instead of failing, so unattended runs finish.
            wait = min(earliest_relief() + 5, hold_deadline) - time.time()
            retry = wait > 0
        if not retry:
            noted = True
            return upstream, account, retries, noted, verdict
        await upstream.aread()
        await upstream.aclose()
        retries += 1
        if wait > 0:
            async with state_lock:
                STATE.events.append(Event(
                    ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"), type="hold",
                    from_account=account, to_account=account,
                    reason=f"all accounts spent; holding {round(wait)}s"))
                STATE.events = STATE.events[-200:]
                save_state()
            await asyncio.sleep(wait)


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI-compatible front door (Phase 3): any OpenAI-format client or router
    rides the rotated Max capacity through here. Translation to /v1/messages with
    the oauth beta header; NO client headers are forwarded, so caller identity
    stops at this proxy — upstream sees one consumer per device key. When every
    account's window is spent this returns 503 + Retry-After (a breaker-friendly
    signal for routers), unlike the transparent proxy below, which stays
    transparent because Claude Code handles Anthropic's own 429s natively."""
    device = device_from_request(request)
    if device is None:
        return JSONResponse({"error": {"message": "claude-rotate: unknown device key",
                                       "type": "authentication_error"}}, status_code=401)
    if not ACCOUNTS:
        return JSONResponse({"error": {"message": "claude-rotate: no claude accounts configured",
                                       "type": "invalid_request_error"}}, status_code=503)
    try:
        oai = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse({"error": {"message": "body is not valid JSON",
                                       "type": "invalid_request_error"}}, status_code=400)
    abody, err = oai_to_anthropic(oai)
    if err:
        return JSONResponse({"error": {"message": err, "type": "invalid_request_error"}},
                            status_code=400)
    fwd = {"content-type": "application/json", "accept-encoding": "identity",
           "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}
    upstream, account, retries, noted, verdict = await send_upstream(
        "POST", "/v1/messages", fwd, json.dumps(abody).encode())

    rec = {"device": device, "account": account, "path": "/v1/chat/completions",
           "model": abody.get("model"), "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    resp_headers = {"x-rotate-account": account}
    for h in ("anthropic-ratelimit-unified-5h-utilization",
              "anthropic-ratelimit-unified-7d-utilization",
              "anthropic-ratelimit-unified-5h-reset",
              "anthropic-ratelimit-unified-status"):
        if upstream.headers.get(h):
            resp_headers[h] = upstream.headers[h]

    if upstream.status_code == 429 and verdict == "exhausted":
        await upstream.aread()
        await upstream.aclose()
        retry_after = max(1, round(earliest_relief() - time.time()))
        rec["status"] = 503
        audit(rec)
        return JSONResponse(
            {"error": {"message": "claude-rotate: every account's window is spent; "
                                  f"retry in {retry_after}s",
                       "type": "overloaded_error"}},
            status_code=503, headers=resp_headers | {"retry-after": str(retry_after)})

    if abody.get("stream") and upstream.status_code == 200 \
            and "text/event-stream" in upstream.headers.get("content-type", ""):
        tr = SseToOai()

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    out = tr.feed(chunk.decode("utf-8", errors="replace"))
                    if out:
                        yield out.encode()
                yield tr.tail().encode()
            finally:
                await upstream.aclose()
                rec["usage"] = summarize_usage(tr.usage)
                rec.update(rate_limit_fields(upstream.headers))
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code)

        return StreamingResponse(relay(), status_code=200, headers=resp_headers,
                                 media_type="text/event-stream")

    content = await upstream.aread()
    await upstream.aclose()
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code)
    rec.update(rate_limit_fields(upstream.headers))
    if upstream.status_code != 200:
        try:
            msg = json.loads(content).get("error", {}).get("message") \
                or content[:200].decode(errors="replace")
        except (json.JSONDecodeError, AttributeError):
            msg = content[:200].decode(errors="replace")
        audit(rec)
        return JSONResponse({"error": {"message": f"upstream: {msg}",
                                       "type": "api_error"}},
                            status_code=upstream.status_code, headers=resp_headers)
    data = json.loads(content)
    rec["usage"] = summarize_usage(data.get("usage"))
    audit(rec)
    return JSONResponse(anthropic_to_oai(data), headers=resp_headers)


@app.post("/v1/images/generations")
async def openai_images(request: Request):
    """OpenAI-compatible image endpoint (Phase 4) backed by ChatGPT-plan Codex
    accounts — see codex_image.py. One image per call, b64_json only (there is
    nowhere to host a URL). Every account spent -> 503 + Retry-After, same
    breaker-friendly contract as /v1/chat/completions."""
    device = device_from_request(request)
    if device is None:
        return JSONResponse({"error": {"message": "claude-rotate: unknown device key",
                                       "type": "authentication_error"}}, status_code=401)
    if not CODEX_ACCOUNTS:
        return JSONResponse({"error": {"message": "claude-rotate: no codex accounts "
                                                  "configured (./setup.py add-codex-account)",
                                       "type": "invalid_request_error"}}, status_code=503)
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse({"error": {"message": "body is not valid JSON",
                                       "type": "invalid_request_error"}}, status_code=400)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": {"message": "prompt is required",
                                       "type": "invalid_request_error"}}, status_code=400)
    if body.get("response_format", "b64_json") != "b64_json":
        return JSONResponse({"error": {"message": "only response_format=b64_json is supported",
                                       "type": "invalid_request_error"}}, status_code=400)

    async with codex_slots:
        result = await codex_image.generate(
            prompt, body.get("size"), accounts=CODEX_ACCOUNTS,
            state=STATE.codex, runner=codex_image.run_codex,
            codex_bin=CODEX_CFG.bin,
            model=CODEX_CFG.model,
            effort=CODEX_CFG.reasoning_effort,
            timeout_s=CODEX_CFG.timeout_s,
            threshold=THRESHOLD, threshold_7d=THRESHOLD_7D)
    async with state_lock:
        save_state()

    rec = {"device": device, "account": result["account"], "path": "/v1/images/generations",
           "model": CODEX_CFG.model, "kind": "image",
           "elapsed_s": result["elapsed_s"]}
    if len(result["tried"]) > 1:
        rec["retries"] = len(result["tried"]) - 1
    headers = {"x-rotate-account": result["account"] or "",
               "x-rotate-elapsed-s": str(result["elapsed_s"])}
    if result["ok"]:
        rec["status"] = 200
        audit(rec)
        return JSONResponse(codex_image.to_openai(result), headers=headers)
    if result["account"] is None:
        rec["status"] = 503
        audit(rec)
        return JSONResponse(
            {"error": {"message": "claude-rotate: no codex account has image quota left "
                                  f"({result['error']}); retry in {result['retry_after']}s",
                       "type": "overloaded_error"}},
            status_code=503, headers=headers | {"retry-after": str(result["retry_after"])})
    rec["status"] = 502
    audit(rec)
    return JSONResponse({"error": {"message": f"codex: {result['error']}",
                                   "type": "api_error"}}, status_code=502, headers=headers)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    device = device_from_request(request)
    if device is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error",
                                        "message": "claude-rotate: unknown device key"}},
            status_code=401)
    if not ACCOUNTS:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": "claude-rotate: no claude accounts configured"}},
            status_code=503)

    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    fwd_headers["accept-encoding"] = "identity"
    fwd_headers.pop("x-api-key", None)

    model = None
    if body and request.method == "POST":
        try:
            model = json.loads(body).get("model")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            pass

    url = "/" + path + ("?" + str(request.url.query) if request.url.query else "")
    upstream, account, retries, noted, _verdict = await send_upstream(
        request.method, url, fwd_headers, body)

    rec = {"device": device, "account": account, "path": "/" + path,
           "model": model, "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    # Claude Code names its session in every request; with it an audit record
    # can be matched to the transcript entry it produced.
    session_id = request.headers.get(SESSION_HEADER)
    if session_id:
        rec["session_id"] = session_id
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP and k.lower() != "content-encoding"}
    resp_headers["x-rotate-account"] = account

    if "text/event-stream" in upstream.headers.get("content-type", ""):
        usage_acc: dict = {}

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    usage_from_sse(chunk.decode("utf-8", errors="replace"), usage_acc)
                    yield chunk
            finally:
                await upstream.aclose()
                rec["usage"] = summarize_usage(usage_acc)
                rec.update(rate_limit_fields(upstream.headers))
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code)

        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 headers=resp_headers,
                                 media_type=upstream.headers.get("content-type"))

    content = await upstream.aread()
    await upstream.aclose()
    try:
        rec["usage"] = summarize_usage(json.loads(content).get("usage"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    rec.update(rate_limit_fields(upstream.headers))
    if rec["path"].startswith("/v1/messages") or rec.get("usage"):
        audit(rec)
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code)
    return Response(content=content, status_code=upstream.status_code, headers=resp_headers)


if __name__ == "__main__":
    uvicorn.run(app, host=CFG.bind, port=CFG.port,
                log_level="warning")
