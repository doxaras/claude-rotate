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
    R.STATE = R.State(active_account=active)


def acct(name, **options):
    return R.Account_config(name=name, token="", **options)


R.ACCOUNTS = {n: acct(n) for n in "abc"}
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
R.STATE.accounts = {
    "a": R.Account(util_5h=0.1, util_7d=0.3, reset_7d=now + 500000),
    "b": R.Account(util_5h=0.5, util_7d=0.6, reset_7d=now + 100000),  # soonest weekly reset
    "c": R.Account(util_5h=0.05, util_7d=0.1, reset_7d=now + 300000),
}
check("consume-first picks soonest weekly reset", R.pick_account() == "b")

# 1b. ...but skips unusable (5h over threshold) accounts
R.STATE.accounts["b"].util_5h = 0.9
check("consume-first skips 5h-exhausted account", R.pick_account() == "c")

# 1c. least-used strategy picks lowest 5h util
R.STRATEGY = "least-used"
check("least-used picks lowest 5h util", R.pick_account() == "c")
R.STRATEGY = "consume-first"

# 2. burst 429 (util well under 1.0, no rejected status) → pace, no rotation
fresh_state("a")
R.STATE.accounts = {"a": R.Account(util_5h=0.4, util_7d=0.2)}
v = run(R.note_response("a", headers(u5=0.4, u7=0.2, retry_after=7), 429))
check("burst 429 verdict is 'burst'", v == "burst")
check("burst 429 does not rotate", R.STATE.active_account == "a")
check("burst 429 sets pace_until ~retry-after",
      6 < R.STATE.accounts["a"].pace_until - time.time() <= 7.5)

# 3. quota 429 (unified-status rejected) → rotate despite cooldown
fresh_state("a")
R.STATE.last_switch_ts = time.time()  # cooldown active — must not block a hard stop
R.STATE.accounts = {
    "a": R.Account(util_5h=1.0, util_7d=0.5, reset_7d=now + 500000),
    "b": R.Account(util_5h=0.1, util_7d=0.2, reset_7d=now + 100000),
    "c": R.Account(util_5h=0.1, util_7d=0.2, reset_7d=now + 300000),
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
check("quota 429 switches", v == "switched")
check("quota 429 lands on soonest weekly reset", R.STATE.active_account == "b")
check("switch event reason", R.STATE.events[-1].reason == "quota_exhausted")

# 4. all spent → exhausted
fresh_state("a")
R.STATE.accounts = {
    "a": R.Account(util_5h=1.0, reset_5h=now + 1000),
    "b": R.Account(util_5h=0.95, reset_5h=now + 2000),
    "c": R.Account(util_5h=0.9, reset_5h=now + 500),
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
# fallback pick = earliest 5h reset = c; c != a so it switches (bounce toward relief)
check("all-spent quota hit still moves toward earliest reset", v == "switched" and R.STATE.active_account == "c")
v = run(R.note_response("c", headers(u5=0.9, status="rejected"), 429))
check("exhausted when fallback is itself", v == "exhausted")
check("earliest_relief is soonest reset", abs(R.earliest_relief() - (now + 500)) < 5)

# 5. threshold switch respects cooldown
fresh_state("a")
R.STATE.last_switch_ts = time.time()
R.STATE.accounts = {"a": R.Account(util_5h=0.85), "b": R.Account(util_5h=0.1), "c": R.Account(util_5h=0.2)}
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch blocked by cooldown", v is None and R.STATE.active_account == "a")
R.STATE.last_switch_ts = 0
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch after cooldown", v == "switched" and R.STATE.active_account == "b")

# 6. hysteresis: candidate must beat active by margin
fresh_state("a")
R.STATE.accounts = {"a": R.Account(util_5h=0.81), "b": R.Account(util_5h=0.79), "c": R.Account(util_5h=0.80)}
v = run(R.note_response("a", headers(u5=0.81), 200))
check("hysteresis blocks ping-pong at the line", v is None and R.STATE.active_account == "a")

# 7. proactive consume-first: jump to clearly-sooner weekly reset even below threshold
fresh_state("a")
R.STATE.accounts = {
    "a": R.Account(util_5h=0.3, util_7d=0.4, reset_7d=now + 500000),
    "b": R.Account(util_5h=0.3, util_7d=0.4, reset_7d=now + 100000),
    "c": R.Account(util_5h=0.3, util_7d=0.4, reset_7d=now + 300000),
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("proactive consume-first switch", v == "switched" and R.STATE.active_account == "b")
check("event reason consume_first", R.STATE.events[-1].reason == "consume_first")
# 7b. margin: near-equal weekly resets don't churn
fresh_state("a")
R.STATE.accounts = {
    "a": R.Account(util_5h=0.3, util_7d=0.4, reset_7d=now + 101000),
    "b": R.Account(util_5h=0.3, util_7d=0.4, reset_7d=now + 100000),
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("consume-first margin prevents churn", v is None and R.STATE.active_account == "a")

# 8. window that passed its reset counts as 0
fresh_state("a")
R.STATE.accounts = {"a": R.Account(util_5h=1.0, reset_5h=now - 10, util_7d=0.9, reset_7d=now - 10)}
check("passed 5h reset → util 0", R.utilization("a") == 0.0)
check("passed 7d reset → util 0", R.util_7d("a") == 0.0)
check("passed weekly reset rolls forward a week", R.weekly_reset("a") == now - 10 + R.WEEK_S)
# 8b. a passed weekly reset doesn't draw a consume-first switch: its new
# window resets a week later, not "already"
fresh_state("b")
R.STATE.accounts = {
    "a": R.Account(util_5h=0.1, util_7d=0.99, reset_7d=now - 1800),
    "b": R.Account(util_5h=0.5, util_7d=0.6, reset_7d=now + 4 * 86400),
    "c": R.Account(util_5h=0.1, util_7d=0.1, reset_7d=now + 6 * 86400),
}
v = run(R.note_response("b", headers(u5=0.5, u7=0.6), 200))
check("no consume-first onto a passed weekly reset", v is None and R.STATE.active_account == "b")

# 9. priority tiers: preferred tier always beats a sooner weekly reset
R.ACCOUNTS = {"a": acct("a", priority=2), "b": acct("b", priority=1), "c": acct("c", priority=1)}
fresh_state()
R.STATE.accounts = {
    "a": R.Account(util_5h=0.1, util_7d=0.1, reset_7d=now + 1000),    # soonest, but backup tier
    "b": R.Account(util_5h=0.1, util_7d=0.1, reset_7d=now + 500000),
    "c": R.Account(util_5h=0.1, util_7d=0.1, reset_7d=now + 100000),
}
check("priority tier beats sooner weekly reset", R.pick_account() == "c")

# 9b. backup tier only used once the preferred tier is spent
R.STATE.accounts["b"].util_5h = 0.9
R.STATE.accounts["c"].util_5h = 0.9
check("backup tier used when preferred spent", R.pick_account() == "a")

# 10. priority recovery: traffic pulled back once a preferred account has room
fresh_state("a")  # active on the backup
R.STATE.accounts = {
    "a": R.Account(util_5h=0.2, util_7d=0.1),
    "b": R.Account(util_5h=0.1, util_7d=0.1),
    "c": R.Account(util_5h=0.9, util_7d=0.1),
}
v = run(R.note_response("a", headers(u5=0.2, u7=0.1), 200))
check("priority recovery switches back", v == "switched" and R.STATE.active_account == "b")
check("recovery event reason", R.STATE.events[-1].reason == "priority_recovery")

# 10b. recovery waits out the cooldown
fresh_state("a")
R.STATE.last_switch_ts = time.time()
R.STATE.accounts = {"a": R.Account(util_5h=0.2), "b": R.Account(util_5h=0.1), "c": R.Account(util_5h=0.9)}
v = run(R.note_response("a", headers(u5=0.2), 200))
check("priority recovery respects cooldown", v is None and R.STATE.active_account == "a")

# 11. disabled accounts: never picked, active one abandoned immediately
R.ACCOUNTS = {"a": acct("a", disabled=True), "b": acct("b"), "c": acct("c")}
fresh_state("a")
R.STATE.accounts = {"a": R.Account(util_5h=0.0), "b": R.Account(util_5h=0.2), "c": R.Account(util_5h=0.1)}
check("disabled excluded from pick", R.pick_account() == "c")
R.STATE.last_switch_ts = time.time()  # even inside the cooldown
v = run(R.note_response("a", headers(u5=0.0), 200))
check("disabled active abandoned", v == "switched" and R.STATE.active_account == "c")
check("disabled event reason", R.STATE.events[-1].reason == "account_disabled")

# 11b. all-spent fallback skips disabled accounts too
fresh_state("b")
R.STATE.accounts = {"a": R.Account(util_5h=1.0, reset_5h=now + 10),
                    "b": R.Account(util_5h=1.0, reset_5h=now + 100),
                    "c": R.Account(util_5h=1.0, reset_5h=now + 50)}
check("fallback skips disabled", R.pick_account() == "c")

# 12. all-spent fallback ignores a stale 5h reset on a week-exhausted account (issue #3)
R.ACCOUNTS = {n: acct(n) for n in "ab"}
fresh_state("b")
R.STATE.accounts = {
    "a": R.Account(util_5h=0.03, reset_5h=now - 72000,    # 5h window reset long ago...
                   util_7d=1.0, reset_7d=now + 260000),   # ...but the week is gone
    "b": R.Account(util_5h=0.94, reset_5h=now + 3600, util_7d=0.26, reset_7d=now + 400000),
}
check("fallback skips week-exhausted account", R.pick_account() == "b")
v = run(R.note_response("b", headers(u5=0.94, u7=0.26), 200))
check("no threshold switch onto week-exhausted account", v is None and R.STATE.active_account == "b")
R.STATE.active_account = "a"
v = run(R.note_response("a", headers(u5=0.03, u7=1.0, status="rejected"), 429))
check("quota 429 on week-exhausted account goes back", v == "switched" and R.STATE.active_account == "b")
R.STATE.accounts["a"].reset_7d = now - 10
check("week-exhausted account eligible again after its 7d reset", R.pick_account() == "a")

# 12b. both windows blocking: the later reset decides
R.STATE.accounts = {
    "a": R.Account(util_5h=1.0, reset_5h=now + 100, util_7d=1.0, reset_7d=now + 9000),
    "b": R.Account(util_5h=1.0, reset_5h=now + 5000, util_7d=0.5, reset_7d=now + 400000),
}
check("relief is the later blocking reset", abs(R.relief_ts("a", now) - (now + 9000)) < 1)
check("fallback picks soonest relief over soonest 5h reset", R.pick_account() == "b")
check("earliest_relief ignores a 5h reset that won't free the account",
      abs(R.earliest_relief() - (now + 5000)) < 5)
R.ACCOUNTS["b"].disabled = True
check("earliest_relief skips disabled accounts", abs(R.earliest_relief() - (now + 9000)) < 5)
R.STATE.accounts["a"].reset_7d = None
check("earliest_relief falls back to 5 min with no known relief",
      abs(R.earliest_relief() - (time.time() + 300)) < 5)

# 13. state.json round trip: events keep their "from"/"to" keys, and keys from
# another version are dropped instead of stopping the proxy
saved = {
    "active_account": "a",
    "accounts": {"a": {"util_5h": 0.4, "reset_5h": now + 10, "from_newer_version": 1}},
    "events": [{"ts": "t", "type": "switch", "from": "b", "to": "a", "reason": "r", "util": 0.9},
               {"type": "switch"}],  # incomplete: dropped
    "last_switch_ts": 5.0,
    "codex": {"x": {"benched_until": now + 60}},
    "from_newer_version": True,
}
loaded = R.State.from_json(saved)
check("state.json loads into dataclasses",
      loaded.accounts["a"] == R.Account(util_5h=0.4, reset_5h=now + 10)
      and loaded.events == [R.Event(ts="t", type="switch", from_account="b", to_account="a",
                                    reason="r", util=0.9)]
      and loaded.codex["x"].benched_until == now + 60 and loaded.last_switch_ts == 5.0)
dumped = loaded.to_json()
check("events saved with from/to keys",
      dumped["events"] == [{"ts": "t", "type": "switch", "from": "b", "to": "a",
                            "reason": "r", "util": 0.9}])
check("round trip is stable", R.State.from_json(dumped) == loaded)
try:
    loaded.last_swich_ts = 1  # misspelt
    check("misspelt field rejected", False)
except AttributeError:
    check("misspelt field rejected", True)

# 14. per-account thresholds: an account's own values replace the global ones
R.ACCOUNTS = {"a": acct("a", switch_threshold=0.5), "b": acct("b", switch_threshold_7d=0.7), "c": acct("c")}
check("own thresholds, the global ones elsewhere",
      (R.threshold("a"), R.threshold_7d("a"), R.threshold("b"), R.threshold_7d("b"), R.threshold("c"))
      == (0.5, 0.98, 0.8, 0.7, 0.8))
fresh_state("a")
R.STATE.accounts = {
    "a": R.Account(util_5h=0.6, util_7d=0.1, reset_5h=now + 1000, reset_7d=now + 100000),
    "b": R.Account(util_5h=0.1, util_7d=0.75, reset_7d=now + 200000),
    "c": R.Account(util_5h=0.3, util_7d=0.1, reset_7d=now + 300000),
}
check("own 5h threshold makes an account unusable", not R.usable("a"))
check("own 7d threshold makes an account unusable", not R.usable("b"))
check("relief_ts uses the own threshold", abs(R.relief_ts("a", now) - (now + 1000)) < 1)
v = run(R.note_response("a", headers(u5=0.6, u7=0.1), 200))
check("own 5h threshold switches", v == "switched" and R.STATE.active_account == "c")
check("switch reason names the own threshold", R.STATE.events[-1].reason == "utilization>=0.5")
R.ACCOUNTS["a"].switch_threshold = 0.9
R.STATE.accounts["a"].util_5h = 0.85
check("a higher own threshold keeps an account usable past the global one", R.usable("a"))

print(f"\nall {PASS} checks passed")
