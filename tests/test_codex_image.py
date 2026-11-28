"""Offline tests for Codex image generation (codex_image.py). No config, no
network, no codex binary — the runner is faked and writes the same files a
real `codex exec` would (generated_images/<thread>/*.png + a session log).

  python3 tests/test_codex_image.py
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import codex_image as ci

PNG = b"\x89PNG\r\n\x1a\nfake"


def _session(primary: float, secondary: float, reset_5h: float, reset_7d: float) -> str:
    return "\n".join([
        json.dumps({"type": "session_meta", "payload": {}}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": reset_5h},
            "secondary": {"used_percent": secondary, "window_minutes": 10080,
                          "resets_at": reset_7d}}}}),
    ])


def _fake_runner(script: dict):
    """script: account-home-name -> "ok" | "quota" | "auth" | "refuse"."""
    calls = []

    async def runner(_bin, home, _model, _effort, prompt, _timeout):
        calls.append((home.name, prompt))
        mode = script[home.name]
        tid = f"tid-{home.name}-{len(calls)}"
        started = json.dumps({"type": "thread.started", "thread_id": tid})
        if mode == "quota":
            return 1, started + "\nerror: usage_limit_reached: You've hit your usage limit"
        if mode == "auth":
            return 1, "Your access token could not be refreshed. Please log in again."
        sess = home / "sessions" / "2026" / "11" / "17"
        sess.mkdir(parents=True, exist_ok=True)
        (sess / f"rollout-2026-11-17T10-00-00-{tid}.jsonl").write_text(
            _session(12.0, 3.0, time.time() + 9000, time.time() + 500000))
        if mode == "ok":
            img = home / "generated_images" / tid
            img.mkdir(parents=True)
            (img / "exec-1.png").write_bytes(PNG)
        return 0, started + '\n{"type":"item.completed","item":{"text":"DONE"}}'

    return runner, calls


def _accounts(tmp: Path, *names: str) -> dict:
    out = {}
    for n in names:
        (tmp / n).mkdir()
        out[n] = ci.Codex_account_config(name=n, codex_home=str(tmp / n))
    return out


def test_aspect_and_prompt():
    assert ci.aspect_for("1536x1024") == "16:9 landscape"
    assert ci.aspect_for("1024x1536") == "9:16 portrait"
    assert ci.aspect_for("1024x1024") == "1:1 square"
    assert ci.aspect_for(None) == "16:9 landscape"
    assert ci.aspect_for("auto") == "16:9 landscape"
    p = ci.build_prompt("  a data centre at dusk ", "1024x1024")
    assert "exactly once" in p and p.endswith("Image (1:1 square): a data centre at dusk")


def test_parse_rate_limits_takes_last_snapshot():
    text = _session(2.0, 1.0, 100, 200) + "\n" + _session(40.0, 9.0, 300, 400)
    assert ci.parse_rate_limits(text) == {"util_5h": 0.4, "reset_5h": 300.0,
                                          "util_7d": 0.09, "reset_7d": 400.0}
    assert ci.parse_rate_limits("not json\n") == {}


def test_classify_failure():
    assert ci.classify_failure("error: usage_limit_reached") == "quota"
    assert ci.classify_failure("You've hit your usage limit.") == "quota"
    assert ci.classify_failure("access token could not be refreshed") == "auth"
    assert ci.classify_failure("Not logged in") == "auth"
    # a hex thread id containing 401 must not read as an auth failure
    assert ci.classify_failure('{"thread_id":"01a1e401-ffff"} refused') == "other"


def test_pick_priority_then_consume_first():
    now = time.time()
    accts = {n: ci.Codex_account_config(name=n, codex_home="") for n in "abcd"}
    accts["c"].priority = 1
    accts["d"].disabled = True
    state = {"a": ci.Codex_account(reset_7d=now + 900_000),
             "b": ci.Codex_account(reset_7d=now + 100_000),
             "c": ci.Codex_account(util_5h=0.95, reset_5h=now + 60)}
    # c is preferred but over threshold, d disabled -> b (weekly resets sooner)
    assert ci.pick_codex_account(accts, state, 0.8, 0.98, now) == "b"
    # c's 5h window has passed its reset -> counts as 0 again, preferred tier wins
    state["c"].reset_5h = now - 1
    assert ci.pick_codex_account(accts, state, 0.8, 0.98, now) == "c"
    state["a"].benched_until = now + 60
    assert ci.pick_codex_account(accts, state, 0.8, 0.98, now, exclude={"b", "c"}) is None


def test_per_account_thresholds():
    now = time.time()
    accts = {n: ci.Codex_account_config(name=n, codex_home="") for n in "abc"}
    accts["a"].switch_threshold = 0.5
    accts["b"].switch_threshold_7d = 0.7
    accts["c"].switch_threshold = 0.9
    state = {"a": ci.Codex_account(util_5h=0.6),
             "b": ci.Codex_account(util_7d=0.75),
             "c": ci.Codex_account(util_5h=0.85)}
    # a and b are past their own thresholds; c is past the global 0.8 but not its own 0.9
    assert [n for n in accts if ci.usable(n, accts, state, 0.8, 0.98, now)] == ["c"]
    assert ci.pick_codex_account(accts, state, 0.8, 0.98, now) == "c"
    assert ci.account_threshold(accts["b"], 0.8) == 0.8
    assert ci.account_threshold_7d(accts["b"], 0.98) == 0.7


def test_generate_success_records_telemetry():
    with tempfile.TemporaryDirectory() as d:
        accts = _accounts(Path(d), "plus-1")
        runner, calls = _fake_runner({"plus-1": "ok"})
        state: dict = {}
        res = asyncio.run(ci.generate("hero", "1536x1024", accounts=accts,
                                      state=state, runner=runner))
        assert res["ok"] and res["png"] == PNG and res["account"] == "plus-1"
        assert state["plus-1"].util_5h == 0.12 and state["plus-1"].util_7d == 0.03
        assert "16:9 landscape" in calls[0][1]
        assert ci.to_openai(res)["data"][0]["b64_json"]


def test_generate_rotates_on_quota_and_auth():
    with tempfile.TemporaryDirectory() as d:
        accts = _accounts(Path(d), "a", "b", "c")
        accts["a"].priority = 1
        accts["b"].priority = 2
        runner, calls = _fake_runner({"a": "quota", "b": "auth", "c": "ok"})
        state: dict = {}
        res = asyncio.run(ci.generate("hero", None, accounts=accts,
                                      state=state, runner=runner))
        assert res["ok"] and res["account"] == "c" and res["tried"] == ["a", "b", "c"]
        assert state["a"].util_5h >= 1.0            # spent: skipped next time
        assert state["b"].benched_until > time.time()  # signed out: benched
        # next call goes straight to c
        res2 = asyncio.run(ci.generate("hero", None, accounts=accts,
                                       state=state, runner=runner))
        assert res2["tried"] == ["c"]


def test_generate_all_spent_reports_retry_after():
    with tempfile.TemporaryDirectory() as d:
        accts = _accounts(Path(d), "a")
        runner, _ = _fake_runner({"a": "quota"})
        res = asyncio.run(ci.generate("hero", None, accounts=accts, state={},
                                      runner=runner))
        assert not res["ok"] and res["account"] is None and res["retry_after"] >= 1


def test_generate_refusal_does_not_rotate():
    with tempfile.TemporaryDirectory() as d:
        accts = _accounts(Path(d), "a", "b")
        runner, calls = _fake_runner({"a": "refuse", "b": "ok"})
        res = asyncio.run(ci.generate("hero", None, accounts=accts, state={},
                                      runner=runner))
        assert not res["ok"] and res["account"] == "a" and len(calls) == 1


if __name__ == "__main__":
    import sys as _sys
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"ok   {n}")
    print(f"{len(fns)} passed")
    _sys.exit(0)
