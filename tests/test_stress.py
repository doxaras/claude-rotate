"""Stress tests: the proxy under high concurrent traffic (mocked upstream).

What high load must not break:
- every request answered, audit complete, sane throughput;
- a quota-429 landing while 100 requests are in flight causes exactly ONE
  switch (state_lock race check), everyone retries onto the new account;
- a burst-429 storm paces, never rotates;
- concurrent held requests all wake and finish;
- parallel SSE streams relay byte-identical;
- the events list stays bounded; aggregate_audit stays fast on a big file.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

PASS = 0


def check(name, cond, extra=""):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        sys.exit(1)
    PASS += 1


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
                                 base_url=R.UPSTREAM, timeout=60.0)


def blast(n, path="/v1/messages"):
    """Fire n concurrent POSTs through the ASGI app; return (responses, seconds)."""
    async def _go():
        limits = httpx.Limits(max_connections=n + 10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app),
                                     base_url="http://proxy", timeout=60.0,
                                     limits=limits) as ac:
            body = json.dumps({"model": "claude-opus-5", "messages": []})
            hdrs = {"authorization": "Bearer devkey-1"}
            t0 = time.monotonic()
            resps = await asyncio.gather(
                *(ac.post(path, content=body, headers=hdrs) for _ in range(n)))
            return resps, time.monotonic() - t0
    return asyncio.run(_go())


def audit_recs():
    return [json.loads(l) for l in R.AUDIT_PATH.read_text().splitlines() if l.strip()]


OK_JSON = {"usage": {"input_tokens": 5, "output_tokens": 2}}

# --- 1. fan-out: 300 concurrent requests, all served, audit complete ---------
fresh_state()
use_upstream(lambda req: httpx.Response(200, json=OK_JSON, headers=unified()))
resps, secs = blast(300)
codes = [r.status_code for r in resps]
check("300 concurrent all 200", codes.count(200) == 300)
check("audit has all 300 records", len(audit_recs()) == 300)
check("throughput sane (>50 req/s via ASGI+mock)", 300 / secs > 50,
      f"{300 / secs:.0f} req/s")

# --- 2. rotation race: quota 429 mid-storm → exactly ONE switch --------------
# The upstream has realistic latency (50 ms RTT), so the WHOLE herd is on the
# wire before the first 429 lands. The lock cannot un-send those — they hit
# the dying account once each, and that is asserted rather than hidden (GitHub
# issue #1). What the lock DOES guarantee, and what these checks pin: one
# switch decision (no switch stampede), and every request retries exactly once
# onto the survivor — no retry thrash, no second herd.
fresh_state("a", {"b": {"util_5h": 0.1, "util_7d": 0.1}})
upstream_calls = {"a": 0, "b": 0}


async def race_handler(req):
    await asyncio.sleep(0.05)              # herd is fully in flight together
    if req.headers["authorization"] == "Bearer tok-a":
        upstream_calls["a"] += 1
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(u5="1.0", status="rejected"))
    upstream_calls["b"] += 1
    return httpx.Response(200, json=OK_JSON, headers=unified(u5="0.1"))


use_upstream(race_handler)
resps, secs = blast(100)
switches = [e for e in R.STATE["events"] if e["type"] == "switch"]
check("100 in-flight during quota hit: all recover to 200",
      all(r.status_code == 200 for r in resps))
check("exactly one switch event despite 100 racers", len(switches) == 1,
      f"{len(switches)} switches")
check("in-flight herd hits the dying account at most once each",
      1 <= upstream_calls["a"] <= 100, f"{upstream_calls['a']} hits on a")
check("survivor serves every request exactly once (no retry thrash)",
      upstream_calls["b"] == 100, f"{upstream_calls['b']} hits on b")
check("active landed on b", R.STATE["active_account"] == "b")
check("audit final records all on b",
      all(rec["account"] == "b" for rec in audit_recs()))

# --- 3. burst storm: 429 bursts pace, never rotate ---------------------------
fresh_state("a", {"a": {"util_5h": 0.3, "util_7d": 0.1}, "b": {"util_5h": 0.0}})
burst_count = {"n": 0}


def burst_handler(req):
    burst_count["n"] += 1
    if burst_count["n"] <= 50:
        return httpx.Response(429, json={"error": "burst"},
                              headers={**unified(u5="0.3"), "retry-after": "0.1"})
    return httpx.Response(200, json=OK_JSON, headers=unified(u5="0.3"))


use_upstream(burst_handler)
resps, secs = blast(100)
check("burst storm: all requests recover to 200",
      all(r.status_code == 200 for r in resps))
check("burst storm: zero rotations",
      not any(e["type"] == "switch" for e in R.STATE["events"])
      and R.STATE["active_account"] == "a")

# --- 4. concurrent holds: everyone parks, everyone finishes ------------------
now = time.time()
fresh_state("a", {"a": {"util_5h": 1.0, "reset_5h": now + 100},
                  "b": {"util_5h": 1.0, "reset_5h": now + 2000}})
R.HOLD_MAX_S = 2
hold_count = {"n": 0}


def hold_handler(req):
    hold_count["n"] += 1
    if hold_count["n"] <= 20:
        # pin active's reset soonest so the verdict is "exhausted", not a bounce
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(
                                  u5="1.0", status="rejected",
                                  **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}))
    return httpx.Response(200, json=OK_JSON, headers=unified(u5="0.0"))


use_upstream(hold_handler)
resps, secs = blast(20)
check("20 concurrent held requests all finish 200",
      all(r.status_code == 200 for r in resps))
check("held ~deadline then released together", 1.5 <= secs < 20, f"{secs:.1f}s")
check("hold events recorded", any(e["type"] == "hold" for e in R.STATE["events"]))
R.HOLD_MAX_S = 0

# --- 5. parallel SSE streams relay byte-identical ----------------------------
fresh_state()
SSE = ('event: message_start\n'
       'data: {"message": {"usage": {"input_tokens": 3, "output_tokens": 1}}}\n\n'
       + "data: " + "x" * 2000 + "\n\n" +
       'event: message_delta\n'
       'data: {"usage": {"output_tokens": 9}}\n\n')
use_upstream(lambda req: httpx.Response(
    200, content=SSE.encode(),
    headers={**unified(), "content-type": "text/event-stream"}))
resps, secs = blast(50)
check("50 parallel SSE streams byte-identical",
      all(r.status_code == 200 and r.text == SSE for r in resps))
check("SSE audit complete under concurrency", len(audit_recs()) == 50)

# --- 6. events list stays bounded under switch churn -------------------------
fresh_state("a", {"a": {"util_5h": 0.1}, "b": {"util_5h": 0.1}})
R.COOLDOWN_S = 0


async def churn():
    for i in range(300):
        active = R.STATE["active_account"]
        h = httpx.Headers({**unified(u5="1.0", status="rejected"),
                           "anthropic-ratelimit-unified-5h-reset": str(time.time() + 1000 + i)})
        await R.note_response(active, h, 429)
asyncio.run(churn())
check("events capped at 200 under churn", len(R.STATE["events"]) <= 200,
      f"{len(R.STATE['events'])} events")
R.COOLDOWN_S = 300

# --- 7. aggregate_audit stays fast on a big log ------------------------------
fresh_state()
FMT = "%Y-%m-%dT%H:%M:%S%z"
ts = time.strftime(FMT, time.localtime(time.time() - 60))
line = json.dumps({"ts": ts, "device": "laptop", "model": "claude-opus-5",
                   "account": "a", "status": 200,
                   "usage": {"input_tokens": 100, "output_tokens": 50,
                             "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}}) + "\n"
with R.AUDIT_PATH.open("w") as f:
    f.write(line * 50_000)
t0 = time.monotonic()
agg = R.aggregate_audit()
secs = time.monotonic() - t0
check("aggregate_audit correct on 50k rows",
      agg["by_device"]["laptop"]["requests"] == 50_000)
check("aggregate_audit under 5s on 50k rows", secs < 5.0, f"{secs:.2f}s")

print(f"\nall {PASS} checks passed")
