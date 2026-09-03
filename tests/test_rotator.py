"""Offline tests for claude-rotate switch logic (pick_account / note_response)."""
import asyncio
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

R.save_state = lambda: None  # don't touch the real state.json


class H(dict):
    """httpx.Headers stand-in (case-insensitive get)."""
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def headers(u5=None, u7=None, r5=None, r7=None, status=None, retry_after=None):
    h = H()
    if u5 is not None: h["anthropic-ratelimit-unified-5h-utilization"] = str(u5)
    if u7 is not None: h["anthropic-ratelimit-unified-7d-utilization"] = str(u7)
    if r5 is not None: h["anthropic-ratelimit-unified-5h-reset"] = str(r5)
    if r7 is not None: h["anthropic-ratelimit-unified-7d-reset"] = str(r7)
    if status is not None: h["anthropic-ratelimit-unified-status"] = status
    if retry_after is not None: h["retry-after"] = str(retry_after)
    return h


def fresh_state(active="a"):
    R.STATE = {"active_account": active, "accounts": {}, "events": [], "last_switch_ts": 0}


R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}, "c": {"name": "c"}}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
R.STRATEGY, R.COOLDOWN_S, R.SWITCH_MARGIN, R.CF_MARGIN_S = "consume-first", 300, 0.05, 3600
run = asyncio.run
now = time.time()
PASS = 0

def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# 1. consume-first: pick usable account with soonest weekly reset
fresh_state()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.3, "reset_7d": now + 500000},
    "b": {"util_5h": 0.5, "util_7d": 0.6, "reset_7d": now + 100000},  # soonest weekly reset
    "c": {"util_5h": 0.05, "util_7d": 0.1, "reset_7d": now + 300000},
}
check("consume-first picks soonest weekly reset", R.pick_account() == "b")

# 1b. ...but skips unusable (5h over threshold) accounts
R.STATE["accounts"]["b"]["util_5h"] = 0.9
check("consume-first skips 5h-exhausted account", R.pick_account() == "c")

# 1c. least-used strategy picks lowest 5h util
R.STRATEGY = "least-used"
check("least-used picks lowest 5h util", R.pick_account() == "c")
R.STRATEGY = "consume-first"

# 2. burst 429 (util well under 1.0, no rejected status) → pace, no rotation
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.4, "util_7d": 0.2}}
v = run(R.note_response("a", headers(u5=0.4, u7=0.2, retry_after=7), 429))
check("burst 429 verdict is 'burst'", v == "burst")
check("burst 429 does not rotate", R.STATE["active_account"] == "a")
check("burst 429 sets pace_until ~retry-after",
      6 < R.STATE["accounts"]["a"]["pace_until"] - time.time() <= 7.5)

# 3. quota 429 (unified-status rejected) → rotate despite cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()  # cooldown active — must not block a hard stop
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "util_7d": 0.5, "reset_7d": now + 500000},
    "b": {"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 100000},
    "c": {"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 300000},
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
check("quota 429 switches", v == "switched")
check("quota 429 lands on soonest weekly reset", R.STATE["active_account"] == "b")
check("switch event reason", R.STATE["events"][-1]["reason"] == "quota_exhausted")

# 4. all spent → exhausted
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "reset_5h": now + 1000},
    "b": {"util_5h": 0.95, "reset_5h": now + 2000},
    "c": {"util_5h": 0.9, "reset_5h": now + 500},
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
# fallback pick = earliest 5h reset = c; c != a so it switches (bounce toward relief)
check("all-spent quota hit still moves toward earliest reset", v == "switched" and R.STATE["active_account"] == "c")
v = run(R.note_response("c", headers(u5=0.9, status="rejected"), 429))
check("exhausted when fallback is itself", v == "exhausted")
check("earliest_relief is soonest reset", abs(R.earliest_relief() - (now + 500)) < 5)

# 5. threshold switch respects cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()
R.STATE["accounts"] = {"a": {"util_5h": 0.85}, "b": {"util_5h": 0.1}, "c": {"util_5h": 0.2}}
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch blocked by cooldown", v is None and R.STATE["active_account"] == "a")
R.STATE["last_switch_ts"] = 0
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch after cooldown", v == "switched" and R.STATE["active_account"] == "b")

# 6. hysteresis: candidate must beat active by margin
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.81}, "b": {"util_5h": 0.79}, "c": {"util_5h": 0.80}}
v = run(R.note_response("a", headers(u5=0.81), 200))
check("hysteresis blocks ping-pong at the line", v is None and R.STATE["active_account"] == "a")

# 7. proactive consume-first: jump to clearly-sooner weekly reset even below threshold
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 500000},
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100000},
    "c": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 300000},
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("proactive consume-first switch", v == "switched" and R.STATE["active_account"] == "b")
check("event reason consume_first", R.STATE["events"][-1]["reason"] == "consume_first")
# 7b. margin: near-equal weekly resets don't churn
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 101000},
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100000},
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("consume-first margin prevents churn", v is None and R.STATE["active_account"] == "a")

# 8. window that passed its reset counts as 0
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 1.0, "reset_5h": now - 10, "util_7d": 0.9, "reset_7d": now - 10}}
check("passed 5h reset → util 0", R.utilization("a") == 0.0)
check("passed 7d reset → util 0", R.util_7d("a") == 0.0)

# 9. priority tiers: preferred tier always beats a sooner weekly reset
R.ACCOUNTS = {"a": {"name": "a", "priority": 2},
              "b": {"name": "b", "priority": 1}, "c": {"name": "c", "priority": 1}}
fresh_state()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 1000},    # soonest, but backup tier
    "b": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 500000},
    "c": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 100000},
}
check("priority tier beats sooner weekly reset", R.pick_account() == "c")

# 9b. backup tier only used once the preferred tier is spent
R.STATE["accounts"]["b"]["util_5h"] = 0.9
R.STATE["accounts"]["c"]["util_5h"] = 0.9
check("backup tier used when preferred spent", R.pick_account() == "a")

# 10. priority recovery: traffic pulled back once a preferred account has room
fresh_state("a")  # active on the backup
R.STATE["accounts"] = {
    "a": {"util_5h": 0.2, "util_7d": 0.1},
    "b": {"util_5h": 0.1, "util_7d": 0.1},
    "c": {"util_5h": 0.9, "util_7d": 0.1},
}
v = run(R.note_response("a", headers(u5=0.2, u7=0.1), 200))
check("priority recovery switches back", v == "switched" and R.STATE["active_account"] == "b")
check("recovery event reason", R.STATE["events"][-1]["reason"] == "priority_recovery")

# 10b. recovery waits out the cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()
R.STATE["accounts"] = {"a": {"util_5h": 0.2}, "b": {"util_5h": 0.1}, "c": {"util_5h": 0.9}}
v = run(R.note_response("a", headers(u5=0.2), 200))
check("priority recovery respects cooldown", v is None and R.STATE["active_account"] == "a")

# 11. disabled accounts: never picked, active one abandoned immediately
R.ACCOUNTS = {"a": {"name": "a", "disabled": True}, "b": {"name": "b"}, "c": {"name": "c"}}
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.0}, "b": {"util_5h": 0.2}, "c": {"util_5h": 0.1}}
check("disabled excluded from pick", R.pick_account() == "c")
R.STATE["last_switch_ts"] = time.time()  # even inside the cooldown
v = run(R.note_response("a", headers(u5=0.0), 200))
check("disabled active abandoned", v == "switched" and R.STATE["active_account"] == "c")
check("disabled event reason", R.STATE["events"][-1]["reason"] == "account_disabled")

# 11b. all-spent fallback skips disabled accounts too
fresh_state("b")
R.STATE["accounts"] = {"a": {"util_5h": 1.0, "reset_5h": now + 10},
                       "b": {"util_5h": 1.0, "reset_5h": now + 100},
                       "c": {"util_5h": 1.0, "reset_5h": now + 50}}
check("fallback skips disabled", R.pick_account() == "c")

print(f"\nall {PASS} checks passed")
