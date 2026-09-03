"""Offline tests for claude-rotate analytics: SSE usage capture, pricing,
cost math, and aggregate_audit rollups/alerts/roster."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# --- usage_from_sse: max-accumulate across events, ignore junk ---
sink = {}
sse = (
    'event: message_start\n'
    'data: {"message": {"usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 200}}}\n\n'
    'event: message_delta\n'
    'data: {"usage": {"output_tokens": 42}}\n\n'
    'data: {broken json with "usage" inside}\n'
    'data: [DONE]\n'
)
R.usage_from_sse(sse, sink)
check("sse captures input_tokens", sink.get("input_tokens") == 10)
check("sse max-accumulates output_tokens", sink.get("output_tokens") == 42)
check("sse captures cache reads", sink.get("cache_read_input_tokens") == 200)

# --- summarize_usage ---
check("summarize None → None", R.summarize_usage(None) is None)
check("summarize fills zeros", R.summarize_usage({"input_tokens": 5}) == {
    "input_tokens": 5, "output_tokens": 0,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

# --- price_for: substring match + default ---
check("opus priced", R.price_for("claude-opus-5") == [5.0, 25.0])
check("fable priced", R.price_for("claude-fable-5") == [10.0, 50.0])
check("unknown model → default", R.price_for("weird-model-9") == R.DEFAULT_PRICE)
check("None model → default", R.price_for(None) == R.DEFAULT_PRICE)

# --- api_cost: in + out + cache read 0.1x + cache write 1.25x ---
u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
     "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
# opus: 5 + 25 + 0.5 + 6.25
check("api_cost math (opus)", abs(R.api_cost("claude-opus-5", u) - 36.75) < 1e-9)
check("api_cost empty usage is 0", R.api_cost("claude-opus-5", {}) == 0.0)

# --- aggregate_audit: rollups, window cut-off, alerts, roster ---
tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.CFG["devices"] = {"laptop": "key-laptop-abcd", "ci-box": "key-ci-efgh", "idle-dev": "key-idle-ijkl"}
R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
R.STATE = {"active_account": "a", "events": [],
           "accounts": {"a": {"util_5h": 0.85, "reset_5h": time.time() + 3600}, "b": {}}}

FMT = "%Y-%m-%dT%H:%M:%S%z"
now = time.time()


def rec(ts, device, model, account, inp=0, out=0, cr=0, cw=0):
    return {"ts": time.strftime(FMT, time.localtime(ts)), "device": device,
            "model": model, "account": account, "status": 200,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}


records = [
    rec(now - 60, "laptop", "claude-opus-5", "a", inp=1000, out=500),        # last hour, expensive
    rec(now - 7200, "laptop", "claude-haiku-4-5", "a", inp=2000, out=100),   # today, outside hour
    rec(now - 90000, "laptop", "claude-opus-5", "a", inp=9999, out=9999),    # >24h → excluded
    rec(now - 120, "ci-box", "claude-sonnet-5", "b", inp=6_000_000, out=10), # token/hour alert
]
with R.AUDIT_PATH.open("w") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")
    f.write("not json\n")  # must be skipped, not crash

agg = R.aggregate_audit()

check("old record excluded from device rollup",
      agg["by_device"]["laptop"]["requests"] == 2)
check("device rollup sums tokens",
      agg["by_device"]["laptop"]["input_tokens"] == 3000)
check("model rollup present", agg["by_model"]["claude-opus-5"]["requests"] == 1)
check("account rollup present", agg["by_account"]["b"]["requests"] == 1)
check("cost accumulated", agg["by_device"]["laptop"]["cost_usd"] > 0)

alert_texts = " | ".join(a["text"] for a in agg["alerts"])
check("token/hour alert fires", "ci-box" in alert_texts and "6,000,010" in alert_texts)
check("expensive-model alert fires", "laptop / claude-opus-5" in alert_texts)
check("util warn alert fires", "a 5h window at 85%" in alert_texts)

devs = agg["devices"]
check("roster covers idle device too", "idle-dev" in devs)
check("recent device online", devs["laptop"]["online"] is True)
check("idle device offline, never seen",
      devs["idle-dev"]["online"] is False and devs["idle-dev"]["last_seen_min"] is None)
check("roster last-hour counters", devs["ci-box"]["req_1h"] == 1
      and devs["ci-box"]["tokens_1h"] == 6_000_010)
check("roster shows key suffix", devs["laptop"]["key_suffix"] == "…abcd")
check("roster last model/account", devs["laptop"]["last_model"] == "claude-opus-5"
      and devs["laptop"]["last_account"] == "a")

print(f"\nall {PASS} checks passed")
