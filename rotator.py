"""claude-rotate Phase 1: account-rotating auth proxy for Claude Code (Max subscriptions).

Devices point at this proxy and authenticate with an internal device key:

    export ANTHROPIC_BASE_URL=http://ais-mac-mini:8484   # over tailscale
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
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

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
    return cfg


CFG = load_config()
DEVICE_BY_KEY = {v: k for k, v in CFG["devices"].items()}
ACCOUNTS = {a["name"]: a for a in CFG["accounts"]}
THRESHOLD = CFG.get("switch_threshold", 0.8)
THRESHOLD_7D = CFG.get("switch_threshold_7d", 0.98)
STRATEGY = CFG.get("strategy", "consume-first")  # or "least-used"
COOLDOWN_S = CFG.get("switch_cooldown_s", 300)
SWITCH_MARGIN = CFG.get("switch_margin", 0.05)
CF_MARGIN_S = CFG.get("consume_first_margin_s", 3600)
HOLD_MAX_S = CFG.get("hold_max_s", 0)  # 0 = return 429 when every account is spent


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
    STATE_PATH.write_text(json.dumps(STATE, indent=2))


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


def usable(name: str) -> bool:
    return (not disabled(name)
            and utilization(name) < THRESHOLD and util_7d(name) < THRESHOLD_7D)


def weekly_reset(name: str) -> float:
    return STATE["accounts"].get(name, {}).get("reset_7d") or float("inf")


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
    If nothing is usable, the non-disabled account whose 5h window resets soonest.
    """
    under = [n for n in ACCOUNTS if usable(n)]
    if under:
        if STRATEGY == "consume-first":
            return min(under, key=lambda n: (priority(n), weekly_reset(n), utilization(n)))
        return min(under, key=lambda n: (priority(n), utilization(n)))
    pool = [n for n in ACCOUNTS if not disabled(n)] or list(ACCOUNTS)
    return min(pool, key=lambda n: STATE["accounts"].get(n, {}).get("reset_5h", 0))


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

        quota_hit = info.get("status") == "rejected" or (
            status == 429 and (utilization(account) >= 1.0 or util_7d(account) >= 1.0))
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
            over = utilization(account) >= THRESHOLD or util_7d(account) >= THRESHOLD_7D
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
                    if (util_7d(account) >= THRESHOLD_7D
                            or priority(nxt) < priority(account)
                            or utilization(nxt) <= utilization(account) - SWITCH_MARGIN):
                        reason = f"utilization>={THRESHOLD}"
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
                STATE["active_account"] = nxt
                STATE["last_switch_ts"] = now
                STATE["events"].append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "type": "switch",
                    "from": account, "to": nxt,
                    "reason": reason,
                    "util": utilization(account),
                })
                STATE["events"] = STATE["events"][-200:]
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
        if info.get("reset_5h"):
            info["reset_5h_in_min"] = max(0, round((info["reset_5h"] - now) / 60))
        if info.get("reset_7d"):
            info["reset_7d_in_h"] = max(0, round((info["reset_7d"] - now) / 3600, 1))
        accounts[name] = info
    return {
        "active_account": STATE["active_account"],
        "switch_threshold": THRESHOLD,
        "switch_threshold_7d": THRESHOLD_7D,
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


def aggregate_audit() -> dict:
    """Roll up audit.jsonl by device/model/account, with a last-hour window and alerts."""
    import datetime as dt

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

    if AUDIT_PATH.exists():
        with AUDIT_PATH.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = rec.get("usage") or {}
                try:
                    ts = dt.datetime.strptime(rec.get("ts", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
                except ValueError:
                    ts = 0
                if ts < day_ago:
                    continue
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
    hold_deadline = time.time() + HOLD_MAX_S if HOLD_MAX_S else None
    retries = 0
    noted = False  # note_response already ran for the response we're returning
    while True:
        account = STATE["active_account"]
        fwd_headers["authorization"] = f"Bearer {ACCOUNTS[account]['token']}"
        pace = STATE["accounts"].get(account, {}).get("pace_until", 0) - time.time()
        if pace > 0:
            await asyncio.sleep(min(pace, 60))
        upstream = await client.send(
            client.build_request(request.method, url, headers=fwd_headers, content=body),
            stream=True)
        if upstream.status_code != 429:
            break
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
            break
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

    rec = {"device": device, "account": account, "path": "/" + path,
           "model": model, "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP and k.lower() != "content-encoding"}

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
