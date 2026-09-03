"""End-to-end tests for the claude-rotate HTTP layer against a mocked
Anthropic upstream (httpx.MockTransport) via the real ASGI app:

auth, header rewriting, SSE relay, quota-429 rotate+retry, burst-429 pacing,
429 passthrough when hold is off, hold-until-reset, and the admin endpoints.
"""
import asyncio
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# --- harness -----------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.save_state = lambda: None
R.ACCOUNTS = {"a": {"name": "a", "token": "tok-a"}, "b": {"name": "b", "token": "tok-b"}}
R.DEVICE_BY_KEY = {"devkey-1": "laptop"}
R.CFG["devices"] = {"laptop": "devkey-1"}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
R.STRATEGY, R.COOLDOWN_S, R.SWITCH_MARGIN, R.CF_MARGIN_S = "consume-first", 300, 0.05, 3600
R.HOLD_MAX_S = 0


def fresh_state(active="a", accounts=None):
    R.STATE = {"active_account": active, "accounts": accounts or {}, "events": [],
               "last_switch_ts": 0}
    R.AUDIT_PATH.write_text("")


def unified(u5="0.5", u7="0.2", status="allowed", **extra):
    now = time.time()
    h = {"anthropic-ratelimit-unified-5h-utilization": u5,
         "anthropic-ratelimit-unified-7d-utilization": u7,
         "anthropic-ratelimit-unified-5h-reset": str(now + 3600),
         "anthropic-ratelimit-unified-7d-reset": str(now + 86400),
         "anthropic-ratelimit-unified-status": status}
    h.update(extra)
    return h


def use_upstream(handler):
    R.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url=R.UPSTREAM, timeout=30.0)


def call(path="/v1/messages", key="devkey-1", body=None, headers=None, method="POST"):
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app),
                                     base_url="http://proxy", timeout=30.0) as ac:
            hdrs = {"authorization": f"Bearer {key}"} if key else {}
            hdrs.update(headers or {})
            content = json.dumps(body or {"model": "claude-opus-5", "messages": []})
            return await ac.request(method, path, content=content, headers=hdrs)
    return asyncio.run(_go())


def audit_recs():
    return [json.loads(l) for l in R.AUDIT_PATH.read_text().splitlines() if l.strip()]


# --- 1. auth -----------------------------------------------------------------
fresh_state()
use_upstream(lambda req: httpx.Response(200))
r = call(key="wrong-key")
check("unknown device key → 401", r.status_code == 401
      and r.json()["error"]["type"] == "authentication_error")
check("401 without any credentials", call(key=None).status_code == 401)

# --- 2. transparent forwarding + header rewriting ----------------------------
fresh_state()
seen = {}


def ok_handler(req):
    seen["auth"] = req.headers.get("authorization")
    seen["accept-encoding"] = req.headers.get("accept-encoding")
    seen["x-api-key"] = req.headers.get("x-api-key")
    seen["beta"] = req.headers.get("anthropic-beta")
    # Genuinely gzipped body so the proxy-side httpx auto-decompresses and the
    # stale content-encoding header must be dropped (load-bearing fact #2).
    body = gzip.compress(json.dumps({"usage": {"input_tokens": 7, "output_tokens": 3}}).encode())
    return httpx.Response(200, content=body,
                          headers={**unified(), "content-encoding": "gzip",
                                   "content-type": "application/json"})


use_upstream(ok_handler)
r = call(headers={"x-api-key": "should-be-dropped", "anthropic-beta": "oauth-2025-04-20"})
check("success passes through", r.status_code == 200)
check("device key swapped for account token", seen["auth"] == "Bearer tok-a")
check("accept-encoding forced to identity", seen["accept-encoding"] == "identity")
check("x-api-key stripped", seen["x-api-key"] is None)
check("client anthropic-beta preserved", seen["beta"] == "oauth-2025-04-20")
check("content-encoding dropped from response", "content-encoding" not in r.headers)
check("state updated from unified headers",
      R.STATE["accounts"]["a"]["util_5h"] == 0.5 and R.STATE["accounts"]["a"]["util_7d"] == 0.2)
recs = audit_recs()
check("audit written with usage + device + account",
      recs and recs[-1]["device"] == "laptop" and recs[-1]["account"] == "a"
      and recs[-1]["usage"]["input_tokens"] == 7 and recs[-1]["model"] == "claude-opus-5")

# --- 3. SSE relay ------------------------------------------------------------
fresh_state()
SSE = ('event: message_start\n'
       'data: {"message": {"usage": {"input_tokens": 11, "output_tokens": 1}}}\n\n'
       'event: message_delta\n'
       'data: {"usage": {"output_tokens": 33}}\n\n')
use_upstream(lambda req: httpx.Response(
    200, content=SSE.encode(),
    headers={**unified(), "content-type": "text/event-stream"}))
r = call()
check("SSE body relayed untouched", r.text == SSE)
recs = audit_recs()
check("SSE usage captured to audit",
      recs and recs[-1]["usage"] == {"input_tokens": 11, "output_tokens": 33,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0})

# --- 4. quota 429 → rotate → transparent retry -------------------------------
fresh_state("a", {"b": {"util_5h": 0.1, "util_7d": 0.1}})
calls = []


def quota_handler(req):
    calls.append(req.headers["authorization"])
    if req.headers["authorization"] == "Bearer tok-a":
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(u5="1.0", status="rejected"))
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.1"))


use_upstream(quota_handler)
r = call()
check("quota 429 hidden from client, retry succeeds", r.status_code == 200)
check("retry rode the next account", calls == ["Bearer tok-a", "Bearer tok-b"])
check("active account rotated", R.STATE["active_account"] == "b")
recs = audit_recs()
check("audit records final account + retry count",
      recs[-1]["account"] == "b" and recs[-1]["retries"] == 1 and recs[-1]["status"] == 200)

# --- 5. burst 429 → pace same account, no rotation ---------------------------
fresh_state("a", {"a": {"util_5h": 0.3, "util_7d": 0.1}, "b": {"util_5h": 0.0}})
calls = []


def burst_handler(req):
    calls.append(req.headers["authorization"])
    if len(calls) == 1:
        return httpx.Response(429, json={"error": "burst"},
                              headers={**unified(u5="0.3"), "retry-after": "1"})
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.3"))


use_upstream(burst_handler)
t0 = time.time()
r = call()
elapsed = time.time() - t0
check("burst 429 hidden from client", r.status_code == 200)
check("burst retried on the SAME account", calls == ["Bearer tok-a", "Bearer tok-a"])
check("no rotation on burst", R.STATE["active_account"] == "a")
check("pace honored (~retry-after)", 0.9 <= elapsed < 5)

# --- 6. every account spent, hold off → 429 passthrough ----------------------
now = time.time()
spent = {"a": {"util_5h": 1.0, "reset_5h": now + 100},
         "b": {"util_5h": 1.0, "reset_5h": now + 2000}}
fresh_state("a", dict(spent))
R.HOLD_MAX_S = 0
# Pin the active account's reset as the soonest so pick_account's fallback
# stays on it → "exhausted" (not a bounce to the other spent account).
use_upstream(lambda req: httpx.Response(429, json={"error": "limit"},
                                        headers=unified(
                                            u5="1.0", status="rejected",
                                            **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)})))
r = call()
check("all spent + hold off → 429 passthrough", r.status_code == 429)
check("audit records the 429", audit_recs()[-1]["status"] == 429)

# --- 7. every account spent, hold on → wait for reset, then succeed ----------
now = time.time()
fresh_state("a", {"a": {"util_5h": 1.0, "reset_5h": now + 100},
                  "b": {"util_5h": 1.0, "reset_5h": now + 2000}})
R.HOLD_MAX_S = 2  # deadline caps the wait so the test stays fast
calls = []


def hold_handler(req):
    calls.append(1)
    if len(calls) == 1:
        # Keep the active account's reset the soonest (see scenario 6) so the
        # verdict is "exhausted" and the hold path actually engages.
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(
                                  u5="1.0", status="rejected",
                                  **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}))
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.0"))


use_upstream(hold_handler)
t0 = time.time()
r = call()
elapsed = time.time() - t0
check("held request eventually succeeds", r.status_code == 200 and len(calls) == 2)
check("hold waited for the deadline window", 1.5 <= elapsed < 10)
check("hold event recorded", any(e["type"] == "hold" for e in R.STATE["events"]))
R.HOLD_MAX_S = 0

# --- 8. admin endpoints ------------------------------------------------------
fresh_state()
check("status needs auth", call(path="/rotate/status", key="nope", method="GET").status_code == 401)
r = call(path="/rotate/status", method="GET")
check("status shape", r.status_code == 200 and r.json()["strategy"] == "consume-first"
      and "accounts" in r.json())
check("status auth via ?key=", call(path="/rotate/status?key=devkey-1", key=None,
                                    method="GET").status_code == 200)
r = call(path="/rotate/stats", method="GET")
check("stats returns rollups", r.status_code == 200 and "by_device" in r.json())
r = call(path="/rotate/panel?key=devkey-1", key=None, method="GET")
check("panel serves html", r.status_code == 200 and "text/html" in r.headers["content-type"])

print(f"\nall {PASS} checks passed")
