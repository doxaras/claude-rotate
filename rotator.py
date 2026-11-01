"""claude-rotate Phase 1: account-rotating auth proxy for Claude Code (Max subscriptions).

Devices point at this proxy and authenticate with an internal device key:

    export ANTHROPIC_BASE_URL=http://my-server:8484   # over tailscale
    export CLAUDE_CODE_OAUTH_TOKEN=<device key from config.json>

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

GET /rotate/status (device-key auth) returns live state for the admin page.
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from oai_compat import SseToOai, anthropic_to_oai, oai_to_anthropic

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
AUDIT_PATH = ROOT / "logs" / "audit.jsonl"

UPSTREAM = "https://api.anthropic.com"
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}

app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=15.0))
state_lock = asyncio.Lock()


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    loaded = []
    for acct in cfg["accounts"]:
        path = ROOT / acct["token_file"]
        if path.exists():
            acct["token"] = path.read_text().strip()
            loaded.append(acct)
        else:
            print(f"warning: skipping account '{acct['name']}' — {acct['token_file']} not found",
                  file=sys.stderr)
    if not loaded:
        raise SystemExit("no accounts with tokens configured — run: ./setup.sh add-account <name>")
    cfg["accounts"] = loaded
    # The example config ships a placeholder device key that is PUBLIC (it's in
    # the repo). It must never be accepted as a live credential: refuse to start
    # while one remains, so add-account-without-add-device can't open the proxy
    # to anyone who read the example (GitHub issue #2).
    bad = [d for d, k in (cfg.get("devices") or {}).items() if "REPLACE-WITH" in k]
    if bad:
        raise SystemExit(
            f"placeholder device key(s) in config.json: {', '.join(bad)} — the example "
            "value is public and must never be a valid credential. "
            "Run: ./setup.sh add-device <name> (it replaces the placeholder).")
    return cfg


CFG = load_config()
DEVICE_BY_KEY = {v: k for k, v in CFG["devices"].items()}
ACCOUNTS = {a["name"]: a for a in CFG["accounts"]}
THRESHOLD = CFG.get("switch_threshold", 0.8)
THRESHOLD_7D = CFG.get("switch_threshold_7d", 0.98)
STRATEGY = CFG.get("strategy", "consume-first")  # or "least-used" / "most-weekly-left" / "weekly-pace"
COOLDOWN_S = CFG.get("switch_cooldown_s", 300)
SWITCH_MARGIN = CFG.get("switch_margin", 0.05)
SWITCH_MARGIN_7D = CFG.get("switch_margin_7d", 0.03)  # most-weekly-left hysteresis
PACE_RATIO = CFG.get("pace_ratio", 1.25)  # weekly-pace hysteresis (multiplicative)
RESERVE_SESSIONS = CFG.get("reserve_sessions", 1)  # weekly-pace: fallbacks to keep
RESERVE_HEADROOM = CFG.get("reserve_headroom", 0.03)  # room a fallback needs in both windows
CF_MARGIN_S = CFG.get("consume_first_margin_s", 3600)
# Above the switch thresholds an account is held in reserve, not dropped: it
# stays usable up to HARD_LIMIT (Anthropic rejects at 1.0) for emergencies
# (nothing else under its thresholds) and is spent on purpose once the window
# holding it there resets within the spend horizon (use it or lose it).
HARD_LIMIT = CFG.get("hard_limit", 1.0)
RESERVE_SPEND_5H_S = CFG.get("reserve_spend_5h_s", 3600)
RESERVE_SPEND_7D_S = CFG.get("reserve_spend_7d_s", 43200)
HOLD_MAX_S = CFG.get("hold_max_s", 0)  # 0 = return 429 when every account is spent
# A closed/free account (403/404) is marked bad and skipped. The mark must not be
# permanent (a plan can be renewed, a block lifted), so an account is reconsidered
# BAD_RECOVER_S after it was last marked bad — doubling each time it refuses again
# (bad_count), capped at BAD_RECOVER_MAX_S — and only a 200 fully clears it.
# 0 = never reconsider.
BAD_RECOVER_S = CFG.get("bad_recover_s", 600)
BAD_RECOVER_MAX_S = CFG.get("bad_recover_max_s", 86400)
BAD_KEYS = ("bad_status", "last_bad_seen", "bad_count")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "active_account": CFG["accounts"][0]["name"],
        "accounts": {a["name"]: {} for a in CFG["accounts"]},
        "events": [],
    }


STATE = load_state()


def save_state() -> None:
    # Write-then-rename so a crash mid-write can never leave a truncated
    # state.json (which would refuse to parse on the next start).
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(STATE, indent=2))
    tmp.replace(STATE_PATH)


def audit(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def utilization(name: str) -> float:
    info = STATE["accounts"].get(name, {})
    now = time.time()
    # A window that has reset no longer counts against the account.
    if info.get("reset_5h") and now >= info["reset_5h"]:
        return 0.0
    return info.get("util_5h", 0.0)


def util_7d(name: str) -> float:
    info = STATE["accounts"].get(name, {})
    if info.get("reset_7d") and time.time() >= info["reset_7d"]:
        return 0.0
    return info.get("util_7d", 0.0)


def priority(name: str) -> int:
    """Lower = preferred. Accounts without a priority share the default tier."""
    return ACCOUNTS[name].get("priority", 100)


def disabled(name: str) -> bool:
    return bool(ACCOUNTS[name].get("disabled"))


def account_bad(name: str) -> bool:
    """A dead account: Anthropic refused a /v1/messages request outright instead
    of returning traffic (403 = closed/permission blocked, 404 = model not
    available on this acct, e.g. a free tier asked to serve a premium model).
    Unlike quota rejection this is not a utilisation header, so it is tracked
    here in state. It stays dead until it answers 200 again or bad_recover_wait()
    elapses since it was last marked bad; after that it is only on probation
    (see on_probation) and gets re-probed when traffic is forced onto it."""
    info = STATE["accounts"].get(name, {})
    if info.get("bad_status") not in (403, 404):
        return False
    wait = bad_recover_wait(name)
    return not wait or time.time() - info.get("last_bad_seen", 0) < wait


def bad_recover_wait(name: str) -> float:
    """How long a bad mark holds: BAD_RECOVER_S, doubled for every repeat refusal
    since the last 200, capped at BAD_RECOVER_MAX_S (0 = forever)."""
    if not BAD_RECOVER_S:
        return 0
    n = max(1, STATE["accounts"].get(name, {}).get("bad_count", 1))
    return min(BAD_RECOVER_S * 2 ** min(n - 1, 30), max(BAD_RECOVER_MAX_S, BAD_RECOVER_S))


def on_probation(name: str) -> bool:
    """Refused a request (403/404) and has not answered 200 since — whether or
    not the bad mark has expired. Such an account is only used when traffic is
    forced off the active one; no voluntary switch moves onto it, so a stale low
    utilisation can't make a strategy keep re-picking a closed account."""
    return STATE["accounts"].get(name, {}).get("bad_status") in (403, 404)


def under_threshold(name: str) -> bool:
    return (not disabled(name) and not account_bad(name)
            and utilization(name) < THRESHOLD and util_7d(name) < THRESHOLD_7D)


def rejected(name: str) -> bool:
    """Last response said quota rejected, and no window has reset since."""
    info = STATE["accounts"].get(name, {})
    if info.get("status") != "rejected":
        return False
    now, seen = time.time(), info.get("last_seen", 0)
    return not any(info.get(k) and seen < info[k] <= now for k in ("reset_5h", "reset_7d"))


def hard_ok(name: str) -> bool:
    """Can still serve a request at all: not disabled, not a dead/closed account,
    under the hard limit in both windows, not currently rejected."""
    return (not disabled(name) and not account_bad(name) and not rejected(name)
            and utilization(name) < HARD_LIMIT and util_7d(name) < HARD_LIMIT)


def binding_reset(name: str) -> float:
    """Reset time of the window with the least room left — when the account's
    remaining capacity stops being the bottleneck (or is lost, if unused)."""
    info = STATE["accounts"].get(name, {})
    room_5h = HARD_LIMIT - utilization(name)
    room_7d = HARD_LIMIT - util_7d(name)
    key = "reset_5h" if room_5h <= room_7d else "reset_7d"
    return info.get(key) or float("inf")


def reserve_spendable(name: str) -> bool:
    """In the reserve band (over a threshold, under the hard limit) and every
    window holding it there resets within its spend horizon: that reserve is
    about to be lost unused, so spend it now."""
    if not hard_ok(name) or under_threshold(name):
        return False
    info, now = STATE["accounts"].get(name, {}), time.time()
    for util, thr, key, horizon in (
            (utilization(name), THRESHOLD, "reset_5h", RESERVE_SPEND_5H_S),
            (util_7d(name), THRESHOLD_7D, "reset_7d", RESERVE_SPEND_7D_S)):
        if util >= thr and not (info.get(key) and info[key] - now <= horizon):
            return False
    return True


def usable(name: str) -> bool:
    """Normal traffic may go here: under both thresholds, or reserve that is
    about to expire."""
    return under_threshold(name) or reserve_spendable(name)


def weekly_reset(name: str) -> float:
    return STATE["accounts"].get(name, {}).get("reset_7d") or float("inf")


def weekly_pace(name: str) -> float:
    """Share of the weekly quota that must be used per hour from now on to not
    lose any of it: weight * (1 - weekly util) / hours until the weekly reset.
    Higher = more perishable. An unknown or passed reset counts as a full week;
    the hours are floored at 1 so the score stays finite near the reset."""
    left = max(0.0, 1.0 - util_7d(name))
    reset = STATE["accounts"].get(name, {}).get("reset_7d")
    now = time.time()
    hours = (reset - now) / 3600 if reset and reset > now else 168.0
    return ACCOUNTS[name].get("weight", 1.0) * left / max(hours, 1.0)


def fresh_session(name: str) -> bool:
    """5h window not started (or already reset) — a session to rotate into."""
    return utilization(name) < 0.01


def can_fall_back_to(name: str) -> bool:
    """Somewhere to rotate into: usable, with real room left in both windows."""
    return (usable(name)
            and THRESHOLD - utilization(name) >= RESERVE_HEADROOM
            and THRESHOLD_7D - util_7d(name) >= RESERVE_HEADROOM)


def earliest_relief() -> float:
    """Soonest moment any account's 5h window resets (for hold-until-reset)."""
    now = time.time()
    resets = [i.get("reset_5h") for i in STATE["accounts"].values()
              if i.get("reset_5h") and i["reset_5h"] > now]
    return min(resets) if resets else now + 300


def pick_account() -> str:
    """Usable = not disabled, both windows under their thresholds (a passed
    reset counts as 0). Priority tiers come first: a lower `priority` account
    always beats a higher one, and the strategy orders accounts *within* a tier.

    consume-first (default): burn the usable account whose weekly window resets
    soonest — weekly quota is use-it-or-lose-it, so spend the perishable one.
    least-used: lowest 5h utilization.
    most-weekly-left: lowest weekly utilization (most weekly quota left), among
    accounts that still have 5h session room; ties broken by lower 5h use.
    weekly-pace: highest weekly_pace() — the account whose remaining weekly
    quota is likeliest to expire unused; ties broken by lower 5h use.
    Within a tier, reserve about to expire (reserve_spendable) goes first,
    soonest reset first — it is lost otherwise.
    Emergency (nothing usable): the reserve band — any hard_ok account, the one
    whose binding window resets soonest, so traffic runs to the real limit
    instead of stopping at the thresholds.
    If nothing is hard_ok: the non-disabled account whose 5h window resets
    soonest, skipping week-exhausted accounts (a 5h reset doesn't help them);
    if every account is week-exhausted, the one whose weekly window resets soonest.
    """
    under = [n for n in ACCOUNTS if usable(n)]
    if under:
        if STRATEGY == "consume-first":
            key = lambda n: (weekly_reset(n), utilization(n))
        elif STRATEGY == "most-weekly-left":
            key = lambda n: (util_7d(n), utilization(n))
        elif STRATEGY == "weekly-pace":
            key = lambda n: (-weekly_pace(n), utilization(n))
        else:
            key = lambda n: (utilization(n),)
        return min(under, key=lambda n: (
            on_probation(n),  # a known-refusing account only as a last resort
            priority(n),
            (0, binding_reset(n)) if reserve_spendable(n) else (1, 0),
            key(n)))
    reserve = [n for n in ACCOUNTS if hard_ok(n)]
    if reserve:
        return min(reserve, key=lambda n: (on_probation(n), priority(n), binding_reset(n)))
    pool = ([n for n in ACCOUNTS if not disabled(n) and not account_bad(n)]
            or [n for n in ACCOUNTS if not disabled(n)] or list(ACCOUNTS))
    week_ok = [n for n in pool if util_7d(n) < HARD_LIMIT]
    if week_ok:
        return min(week_ok, key=lambda n: STATE["accounts"].get(n, {}).get("reset_5h", 0))
    return min(pool, key=weekly_reset)


def record_switch(frm: str, to: str, reason: str, now: float) -> None:
    """Make `to` the active account and log the switch event. Caller holds
    state_lock and saves state."""
    STATE["active_account"] = to
    STATE["last_switch_ts"] = now
    STATE["events"].append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "type": "switch",
        "from": frm, "to": to,
        "reason": reason,
        "util": utilization(frm),
    })
    STATE["events"] = STATE["events"][-200:]


async def note_response(account: str, headers: httpx.Headers, status: int,
                        bad: bool = False) -> str | None:
    """Update account state from unified rate-limit headers; maybe switch.

    bad: True when this account is a closed/free account (403/404 on messages)
    — it is marked unusable and a hard switch away is forced, so the very next
    request (and a mid-stream retry) lands on a working account.

    Returns what happened: "burst" (per-minute 429, paced — no rotation),
    "switched" (active account changed, retry can ride it), "exhausted"
    (quota gone and nowhere better to go), or None.
    """
    def _f(h):
        v = headers.get(h)
        return float(v) if v is not None else None

    async with state_lock:
        info = STATE["accounts"].setdefault(account, {})
        for key, header in (
            ("util_5h", "anthropic-ratelimit-unified-5h-utilization"),
            ("util_7d", "anthropic-ratelimit-unified-7d-utilization"),
            ("reset_5h", "anthropic-ratelimit-unified-5h-reset"),
            ("reset_7d", "anthropic-ratelimit-unified-7d-reset"),
        ):
            val = _f(header)
            if val is not None:
                info[key] = val
        if headers.get("anthropic-ratelimit-unified-status"):
            info["status"] = headers["anthropic-ratelimit-unified-status"]
        now = time.time()
        info["last_seen"] = now

        if status == 200 and info.get("bad_status"):
            # A successful reply means the account can serve again — clear the
            # closed/free marker so it is reconsidered for rotation.
            for k in BAD_KEYS:
                info.pop(k, None)

        quota_hit = info.get("status") == "rejected" or (
            status == 429 and (utilization(account) >= 1.0 or util_7d(account) >= 1.0))
        if bad:
            # Closed/free account refused the request outright (403/404). Mark it
            # dead so pick_account skips it, and force a hard switch below.
            # A repeat refusal (re-probe of an expired mark) doubles the wait.
            info["bad_count"] = info.get("bad_count", 0) + 1 if info.get("bad_status") else 1
            info["bad_status"] = status
            info["last_bad_seen"] = now
            quota_hit = True  # hard stop: immediately rotate to a working account
        if status == 429 and not quota_hit:
            # Per-minute burst limit, not spent quota. Rotating would just move
            # the burst to the next account and drop its warm prompt cache —
            # pace this account briefly instead.
            try:
                pace = min(float(headers.get("retry-after", "")), 60.0)
            except ValueError:
                pace = 15.0
            info["pace_until"] = now + pace
            save_state()
            return "burst"

        verdict = None
        if STATE["active_account"] != account:
            # Another request already rotated; a retry rides the new active.
            verdict = "switched" if quota_hit else None
        else:
            cooled = now - STATE.get("last_switch_ts", 0) >= COOLDOWN_S
            over = not usable(account)  # expiring reserve is being spent on purpose
            nxt = pick_account()
            reason = None
            if nxt != account:
                if bad:
                    # Only rotate to an account that can actually serve now.
                    # If every other account is also dead (all bad), stay put and
                    # surface the refusal instead of bouncing between dead ones.
                    reason = "account_unusable" if hard_ok(nxt) else None
                elif quota_hit:
                    reason = "quota_exhausted"  # hard stop: no cooldown, no margin
                elif disabled(account):
                    reason = "account_disabled"  # hard rule: leave immediately
                elif over and cooled and (usable(nxt) or not hard_ok(account)):
                    # Hysteresis: only move to a meaningfully better account, so
                    # two accounts hovering at the line never ping-pong. A move
                    # to a preferred tier, off a week-exhausted account, or onto
                    # expiring reserve skips the margin. If nxt is itself only
                    # reserve (emergency), stay put: the active account keeps
                    # its warm cache until it is actually rejected.
                    if (util_7d(account) >= THRESHOLD_7D
                            or priority(nxt) < priority(account)
                            or reserve_spendable(nxt)
                            or utilization(nxt) <= utilization(account) - SWITCH_MARGIN):
                        reason = f"utilization>={THRESHOLD}"
                elif cooled and usable(nxt) and not over and not on_probation(nxt):
                    # Voluntary moves never land on an account that last refused
                    # outright — it gets re-probed only when traffic is forced.
                    if priority(nxt) < priority(account):
                        # A preferred account has room again — pull traffic back.
                        reason = "priority_recovery"
                    elif reserve_spendable(nxt) and not reserve_spendable(account):
                        # Another account's last few % reset soon — spend them
                        # before they are lost; come back when it is rejected.
                        reason = "reserve_expiring"
                    elif (STRATEGY == "consume-first"
                          and weekly_reset(nxt) < weekly_reset(account) - CF_MARGIN_S):
                        # Use-it-or-lose-it: proactively jump to an account whose
                        # weekly window expires clearly sooner than the active one's.
                        reason = "consume_first"
                    elif (STRATEGY == "most-weekly-left"
                          and util_7d(nxt) <= util_7d(account) - SWITCH_MARGIN_7D):
                        # Proactively move to the account with clearly more weekly
                        # quota left; the margin stops near-equal accounts churning
                        # (each switch costs the warm prompt cache).
                        reason = "most_weekly_left"
                    elif (STRATEGY == "weekly-pace"
                          and weekly_pace(nxt) >= weekly_pace(account) * PACE_RATIO
                          and (not fresh_session(nxt)
                               or sum(can_fall_back_to(n) for n in ACCOUNTS if n != nxt)
                               >= RESERVE_SESSIONS)):
                        # Proactively move to clearly more perishable quota — but a
                        # voluntary move must not start a fresh 5h window unless
                        # somewhere else (the account being left counts) still has
                        # room to rotate into when the new session runs out. Forced
                        # switches above may always use the reserve.
                        reason = "weekly_pace"
            if reason:
                record_switch(account, nxt, reason, now)
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


@app.get("/rotate/status")
async def rotate_status(request: Request):
    if device_from_request(request) is None:
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    now = time.time()
    accounts = {}
    for name in ACCOUNTS:
        info = dict(STATE["accounts"].get(name, {}))
        info["effective_util_5h"] = utilization(name)
        info["effective_util_7d"] = util_7d(name)
        info["priority"] = priority(name)
        info["disabled"] = disabled(name)
        info["account_bad"] = account_bad(name)
        info["on_probation"] = on_probation(name)
        if account_bad(name) and bad_recover_wait(name):
            info["bad_recover_in_min"] = max(0, round(
                (info.get("last_bad_seen", 0) + bad_recover_wait(name) - now) / 60))
        if info.get("reset_5h"):
            info["reset_5h_in_min"] = max(0, round((info["reset_5h"] - now) / 60))
        if info.get("reset_7d"):
            info["reset_7d_in_h"] = max(0, round((info["reset_7d"] - now) / 3600, 1))
        info["weekly_pace_pct_per_h"] = round(weekly_pace(name) * 100, 3)
        info["fresh_session"] = fresh_session(name)
        info["hard_ok"] = hard_ok(name)
        info["reserve_spendable"] = reserve_spendable(name)
        accounts[name] = info
    return {
        "active_account": STATE["active_account"],
        "switch_threshold": THRESHOLD,
        "switch_threshold_7d": THRESHOLD_7D,
        "hard_limit": HARD_LIMIT,
        "strategy": STRATEGY,
        "hold_max_s": HOLD_MAX_S,
        "accounts": accounts,
        "recent_events": STATE["events"][-10:],
    }


def device_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not key:
        key = request.query_params.get("key", "")
    return DEVICE_BY_KEY.get(key)


# API list prices per MTok (input, output), matched by substring of the model id.
# Cost columns are *API-equivalent* dollars: what this usage would cost pay-per-token.
# Override in config.json under "prices_per_mtok".
PRICES = CFG.get("prices_per_mtok", {
    "fable": [10.0, 50.0], "mythos": [10.0, 50.0],
    "opus": [5.0, 25.0],
    "sonnet": [3.0, 15.0],
    "haiku": [1.0, 5.0],
})
DEFAULT_PRICE = [5.0, 25.0]
ALERT_CFG = CFG.get("alerts", {
    "device_tokens_per_hour": 5_000_000,
    "expensive_model_patterns": ["opus", "fable", "mythos"],
    "util_warn": 0.8,
})


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
            if any(p in model for p in ALERT_CFG["expensive_model_patterns"]):
                key = f'{rec.get("device", "?")} / {model}'
                expensive_hour[key] = expensive_hour.get(key, 0) + 1

    alerts = []
    for dev, toks in device_hour_tokens.items():
        if toks > ALERT_CFG["device_tokens_per_hour"]:
            alerts.append({"level": "warn",
                           "text": f"{dev} consumed {toks:,} tokens in the last hour "
                                   f"(threshold {ALERT_CFG['device_tokens_per_hour']:,})"})
    for key, n in expensive_hour.items():
        alerts.append({"level": "info", "text": f"expensive model in use: {key} — {n} requests in the last hour"})
    for name in ACCOUNTS:
        u = utilization(name)
        if u >= ALERT_CFG["util_warn"]:
            alerts.append({"level": "warn", "text": f"{name} 5h window at {u:.0%} (switch threshold {THRESHOLD:.0%})"})

    # Roster covers every registered device, traffic or not.
    devices = {}
    for name, key in CFG["devices"].items():
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


PANEL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>claude-rotate</title>
<style>
 body{font-family:-apple-system,sans-serif;margin:2rem;background:#12151a;color:#e6e6e6}
 h1{font-size:1.3rem} h2{font-size:1rem;margin-top:1.6rem;color:#9ecbff}
 table{border-collapse:collapse;margin-top:.4rem;min-width:520px}
 td,th{padding:.3rem .8rem;border-bottom:1px solid #2a2f38;text-align:right;font-variant-numeric:tabular-nums}
 td:first-child,th:first-child{text-align:left}
 .gauge{display:inline-block;width:220px;height:12px;background:#2a2f38;border-radius:6px;overflow:hidden;vertical-align:middle;margin:0 .6rem}
 .gauge i{display:block;height:100%;background:#4caf7d} .gauge i.hot{background:#e0664d}
 .active{color:#7dd87d;font-weight:600} .alert-warn{color:#ffb454} .alert-info{color:#9ecbff}
 .muted{color:#8a919c;font-size:.85rem}
</style></head><body>
<h1>claude-rotate <span class="muted" id="upd"></span></h1>
<h2>Devices</h2><table id="t-roster"></table>
<div id="accounts"></div>
<h2>Alerts</h2><div id="alerts" class="muted">none</div>
<h2>Consumption by device (24h)</h2><table id="t-device"></table>
<h2>Consumption by model (24h)</h2><table id="t-model"></table>
<h2>Consumption by account (24h)</h2><table id="t-account"></table>
<h2>Recent switches</h2><div id="events" class="muted">none</div>
<p class="muted">Cost = API-equivalent dollars at list prices (incl. cache read 0.1x / write 1.25x) — actual spend is covered by the Max subscriptions.</p>
<script>
const KEY = new URLSearchParams(location.search).get('key');
function fmt(n){return n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':''+n}
function table(el, data){
  let h='<tr><th></th><th>req</th><th>in</th><th>out</th><th>cache r</th><th>cache w</th><th>$ equiv</th></tr>';
  for(const [k,v] of Object.entries(data))
    h+=`<tr><td>${k}</td><td>${v.requests}</td><td>${fmt(v.input_tokens)}</td><td>${fmt(v.output_tokens)}</td><td>${fmt(v.cache_read)}</td><td>${fmt(v.cache_write)}</td><td>$${v.cost_usd.toFixed(2)}</td></tr>`;
  el.innerHTML=h;
}
async function refresh(){
  const [st, stats] = await Promise.all([
    fetch('/rotate/status?key='+KEY).then(r=>r.json()),
    fetch('/rotate/stats?key='+KEY).then(r=>r.json())]);
  let rh='<tr><th>device</th><th>status</th><th>last seen</th><th>last model</th><th>via account</th><th>req 1h</th><th>tokens 1h</th><th>key</th></tr>';
  for(const [name,d] of Object.entries(stats.devices||{})){
    const status = d.online ? '<span class="active">● online</span>' : '<span class="muted">○ idle</span>';
    const seen = d.last_seen_min==null ? 'never' : d.last_seen_min<1 ? 'just now'
      : d.last_seen_min<60 ? d.last_seen_min+' min ago' : (d.last_seen_min/60).toFixed(1)+' h ago';
    rh+=`<tr><td>${name}</td><td>${status}</td><td>${seen}</td><td>${d.last_model||'—'}</td><td>${d.last_account||'—'}</td><td>${d.req_1h}</td><td>${fmt(d.tokens_1h)}</td><td class="muted">${d.key_suffix}</td></tr>`;
  }
  document.getElementById('t-roster').innerHTML=rh;
  let h='';
  for(const [name,a] of Object.entries(st.accounts)){
    const u=a.effective_util_5h||0, w=a.util_7d||0;
    const act=name===st.active_account?' <span class="active">● active</span>':'';
    const tags=(a.disabled?' <span class="alert-warn">(disabled)</span>':'')
      +(a.priority!==100?` <span class="muted">prio ${a.priority}</span>`:'');
    h+=`<p><b>${name}</b>${tags}${act}<br>5h <span class="gauge"><i class="${u>=st.switch_threshold?'hot':''}" style="width:${Math.min(100,u*100)}%"></i></span>${(u*100).toFixed(0)}%`
      +(a.reset_5h_in_min!=null?` <span class="muted">resets in ${a.reset_5h_in_min} min</span>`:'')
      +`<br>7d <span class="gauge"><i style="width:${Math.min(100,w*100)}%"></i></span>${(w*100).toFixed(0)}%`
      +(a.reset_7d_in_h!=null?` <span class="muted">resets in ${a.reset_7d_in_h} h</span>`:'')+`</p>`;
  }
  document.getElementById('accounts').innerHTML=h;
  document.getElementById('alerts').innerHTML = stats.alerts.length
    ? stats.alerts.map(a=>`<div class="alert-${a.level}">⚠ ${a.text}</div>`).join('') : 'none';
  table(document.getElementById('t-device'), stats.by_device);
  table(document.getElementById('t-model'), stats.by_model);
  table(document.getElementById('t-account'), stats.by_account);
  document.getElementById('events').innerHTML = st.recent_events.length
    ? st.recent_events.map(e=>`<div>${e.ts} — ${e.from} → ${e.to} (${e.reason})</div>`).join('') : 'none';
  document.getElementById('upd').textContent='updated '+new Date().toLocaleTimeString();
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


@app.get("/rotate/panel")
async def rotate_panel(request: Request):
    if device_from_request(request) is None:
        return JSONResponse({"error": "unknown device key — open /rotate/panel?key=<device key>"},
                            status_code=401)
    return Response(content=PANEL_HTML, media_type="text/html")


def messages_unusable(upstream, url: str) -> bool:
    """A /v1/messages response that signals this account can't do the request at
    all: 403 (closed / permission-blocked) or 404 (model not available on this
    account, e.g. a free tier asked to serve a premium model). Quota exhaustion
    shows up as 429 instead, handled separately."""
    return (method_is_messages(url)
            and getattr(upstream, "status_code", None) in (403, 404))


def method_is_messages(url: str) -> bool:
    """True when the upstream path is the Anthropic /v1/messages endpoint itself —
    not /v1/messages/count_tokens or /v1/messages/batches/..., whose 404s (e.g. an
    unknown batch id) say nothing about the account."""
    return url.split("?", 1)[0].rstrip("/") == "/v1/messages"


def pick_working() -> str | None:
    """The best account that can actually serve right now, or None if none.
    Distinguishes 'a dead account and at least one other works' (return the good
    one) from 'every account is dead' (None) — so the next request isn't fired
    at a closed/free account when a working one is idle."""
    ok = [n for n in ACCOUNTS if hard_ok(n)]
    if not ok:
        return None
    return min(ok, key=lambda n: (on_probation(n), priority(n), binding_reset(n)))


async def revert_refusals(refused: dict, start: str, status: int) -> None:
    """A request ended refused, and every previously healthy account it was
    tried on refused it with the same 403/404: that is the request's fault
    (unknown model name, a header the API rejects), not the accounts'. Undo the
    bad marks it made on those accounts and go back to the account it started
    on, so one bad request can't take the whole herd out of rotation. Accounts
    already on probation keep their re-mark."""
    async with state_lock:
        for name, prior in refused.items():
            info = STATE["accounts"].setdefault(name, {})
            for k in BAD_KEYS:
                if prior.get(k) is None:
                    info.pop(k, None)
                else:
                    info[k] = prior[k]
        now = time.time()
        if STATE["active_account"] != start:
            record_switch(STATE["active_account"], start, "request_refused_revert", now)
        STATE["events"].append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "refusal",
            "from": start, "to": start,
            "reason": f"{status} from every account tried ({', '.join(refused)}); "
                      "treated as a bad request, bad marks reverted"})
        STATE["events"] = STATE["events"][-200:]
        save_state()


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
    start = STATE["active_account"]
    # Accounts that were clean (not on probation) before this request and then
    # refused it: name -> (their bad-mark fields before, the refusal status).
    refused: dict = {}
    while True:
        account = STATE["active_account"]
        # Never fire a request at an account we already know is a closed/free
        # dead-end when a working account is available — leave before sending.
        if account_bad(account):
            async with state_lock:
                good = pick_working()
                if good is not None and good != STATE["active_account"]:
                    record_switch(STATE["active_account"], good, "account_unusable",
                                  time.time())
                    save_state()
                    account = good
        fwd_headers["authorization"] = f"Bearer {ACCOUNTS[account]['token']}"
        pace = STATE["accounts"].get(account, {}).get("pace_until", 0) - time.time()
        if pace > 0:
            await asyncio.sleep(min(pace, 60))
        upstream = await client.send(
            client.build_request(method, url, headers=fwd_headers, content=body),
            stream=True)
        if upstream.status_code != 429 and not messages_unusable(upstream, url):
            return upstream, account, retries, noted, verdict
        # A 429 (quota/burst) or a /v1/messages refusal on a closed/free account
        # (403 permission-blocked, or 404 model-not-found) both mean: rotate.
        note_status = upstream.status_code
        bad = note_status in (403, 404)
        if bad and account not in refused and not on_probation(account):
            info = STATE["accounts"].get(account, {})
            refused[account] = ({k: info.get(k) for k in BAD_KEYS}, note_status)
        verdict = await note_response(account, upstream.headers, note_status, bad=bad)
        wait = 0.0
        retry = verdict in ("switched", "burst") and retries < 5
        if (not retry and bad and refused
                and len({st for _, st in refused.values()}) == 1):
            # The request ends refused and every account that was healthy before
            # it refused it the same way: the request is at fault, so don't
            # leave those accounts marked dead.
            await revert_refusals({n: p for n, (p, _) in refused.items()}, start,
                                  next(iter(refused.values()))[1])
        # Holding only makes sense for spent quota (429), not for refusals.
        if not retry and verdict == "exhausted" and hold_deadline and not bad:
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
                STATE["events"].append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "hold",
                    "from": account, "to": account,
                    "reason": f"all accounts spent; holding {round(wait)}s"})
                STATE["events"] = STATE["events"][-200:]
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
                rec["util_5h"] = upstream.headers.get(
                    "anthropic-ratelimit-unified-5h-utilization")
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code)

        return StreamingResponse(relay(), status_code=200, headers=resp_headers,
                                 media_type="text/event-stream")

    content = await upstream.aread()
    await upstream.aclose()
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code)
    rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
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


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    device = device_from_request(request)
    if device is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error",
                                        "message": "claude-rotate: unknown device key"}},
            status_code=401)

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
                rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
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
    rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
    if rec["path"].startswith("/v1/messages") or rec.get("usage"):
        audit(rec)
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code)
    return Response(content=content, status_code=upstream.status_code, headers=resp_headers)


if __name__ == "__main__":
    uvicorn.run(app, host=CFG.get("bind", "127.0.0.1"), port=CFG.get("port", 8484),
                log_level="warning")
