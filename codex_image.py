"""Image generation on ChatGPT-plan accounts via the official Codex CLI (Phase 4).

Each Codex account is a CODEX_HOME directory signed in once with
`codex login --device-auth`. Codex owns its own OAuth refresh, so the proxy
never touches ChatGPT credentials — it shells out to `codex exec`, which calls
the built-in image tool and drops the PNG under
CODEX_HOME/generated_images/<thread_id>/.

Quota telemetry is exact, not guessed: every Codex session log
(CODEX_HOME/sessions/**/rollout-*-<thread_id>.jsonl) records `rate_limits`
with a primary (5h) and secondary (weekly) window, which map straight onto the
util_5h / util_7d fields the Claude rotation already uses.

Import-safe like oai_compat.py: no config dependency, so the pure helpers and
the rotation loop (with an injected runner) are testable offline.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

# (returncode, combined stdout+stderr) — tests inject a fake with this shape.
Runner = Callable[..., Awaitable[tuple[int, str]]]

AUTH_BACKOFF_S = 1800  # a signed-out account sits out this long before a retry


@dataclass(slots=True)
class Codex_account_config:
    """A Codex account: its CODEX_HOME (from confidential.json) and its
    account_options (from config.json5)."""
    name: str
    codex_home: str
    priority: int = 100  # lower = preferred tier
    disabled: bool = False
    switch_threshold: float | None = None  # None = the global switch_threshold
    switch_threshold_7d: float | None = None  # None = the global switch_threshold_7d


def account_threshold(account: Codex_account_config, default: float) -> float:
    """The 5h utilization at which this account stops being used: its own
    switch_threshold, else `default` (the global one)."""
    return default if account.switch_threshold is None else account.switch_threshold


def account_threshold_7d(account: Codex_account_config, default: float) -> float:
    """The weekly utilization at which this account stops being used: its own
    switch_threshold_7d, else `default` (the global one)."""
    return default if account.switch_threshold_7d is None else account.switch_threshold_7d


@dataclass(slots=True)
class Codex_account:
    """What is known about one Codex account's quota (from its session logs) and
    login; the proxy persists these in state.json under "codex". A fresh account
    has the defaults: nothing used, no known reset, not benched."""
    util_5h: float = 0.0
    util_7d: float = 0.0
    reset_5h: float | None = None  # epoch seconds
    reset_7d: float | None = None
    last_seen: float | None = None
    benched_until: float = 0.0  # a signed-out account sits out until then
    last_error: str | None = None


def aspect_for(size: str | None) -> str:
    """OpenAI `size` ("1536x1024", "auto", ...) -> the wording Codex's image
    tool follows. Landscape is the default: these are article heroes."""
    try:
        w, h = (int(x) for x in (size or "").lower().split("x"))
    except ValueError:
        return "16:9 landscape"
    if w == h:
        return "1:1 square"
    return "16:9 landscape" if w > h else "9:16 portrait"


def build_prompt(prompt: str, size: str | None = None) -> str:
    """Pin the agent to one tool call. Without this it plans, reads its image
    skill and tries to save files: 60s instead of 40s, ~3.5x the tokens."""
    return ("Call your built-in image generation tool exactly once, then reply "
            "only DONE. Do not read files, run shell commands, load skills or "
            f"save anything. Image ({aspect_for(size)}): {prompt.strip()}")


def parse_rate_limits(session_text: str) -> dict:
    """Last `rate_limits` snapshot in a Codex session log -> state fields."""
    snap = None
    for line in session_text.splitlines():
        if '"rate_limits"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rl = (rec.get("payload") or {}).get("rate_limits")
        if isinstance(rl, dict):
            snap = rl
    out: dict = {}
    if not snap:
        return out
    for window, (ukey, rkey) in (("primary", ("util_5h", "reset_5h")),
                                 ("secondary", ("util_7d", "reset_7d"))):
        w = snap.get(window) or {}
        if w.get("used_percent") is not None:
            out[ukey] = float(w["used_percent"]) / 100.0
        if w.get("resets_at"):
            out[rkey] = float(w["resets_at"])
    return out


def thread_id_from(output: str) -> str | None:
    for line in output.splitlines():
        if '"thread.started"' in line:
            try:
                return json.loads(line).get("thread_id")
            except json.JSONDecodeError:
                pass
    return None


def classify_failure(output: str) -> str:
    """"quota" (spent window: rotate), "auth" (signed out: bench + rotate),
    or "other" (bad prompt, refusal, crash: no point trying another account)."""
    low = output.lower()
    if "usage_limit_reached" in low or "usage limit" in low:
        return "quota"
    if ("could not be refreshed" in low or "not logged in" in low
            or "unauthorized" in low):
        return "auth"
    return "other"


def find_image(codex_home: Path, thread_id: str) -> Path | None:
    pngs = sorted((codex_home / "generated_images" / thread_id).glob("*.png"),
                  key=lambda p: p.stat().st_mtime)
    return pngs[-1] if pngs else None


def read_session(codex_home: Path, thread_id: str) -> str:
    hits = list((codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"))
    return hits[0].read_text(errors="replace") if hits else ""


def usable(name: str, accounts: dict[str, Codex_account_config],
           state: dict[str, Codex_account], threshold: float,
           threshold_7d: float, now: float) -> bool:
    """Not disabled, not benched, and both windows under the account's
    thresholds; `threshold` / `threshold_7d` are the global ones, used where
    the account has none of its own."""
    account = accounts[name]
    if account.disabled:
        return False
    info = state.get(name, Codex_account())
    if info.benched_until > now:
        return False
    u5 = 0.0 if info.reset_5h and now >= info.reset_5h else info.util_5h
    u7 = 0.0 if info.reset_7d and now >= info.reset_7d else info.util_7d
    return (u5 < account_threshold(account, threshold)
            and u7 < account_threshold_7d(account, threshold_7d))


def pick_codex_account(accounts: dict[str, Codex_account_config],
                       state: dict[str, Codex_account], threshold: float,
                       threshold_7d: float, now: float,
                       exclude: set[str] = frozenset()) -> str | None:
    """Same policy as the Claude side: priority tier first, then consume-first
    (spend the account whose weekly window resets soonest)."""
    under = [n for n in accounts if n not in exclude
             and usable(n, accounts, state, threshold, threshold_7d, now)]
    if not under:
        return None
    return min(under, key=lambda n: (accounts[n].priority,
                                     state.get(n, Codex_account()).reset_7d or float("inf"),
                                     state.get(n, Codex_account()).util_5h))


async def run_codex(codex_bin: str, codex_home: Path, model: str, effort: str,
                    prompt: str, timeout_s: float) -> tuple[int, str]:
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    with tempfile.TemporaryDirectory(prefix="codex-img-") as cwd:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "exec", "--json", "-m", model,
            "-c", f"model_reasoning_effort={effort}",
            "--skip-git-repo-check", "--sandbox", "read-only", "-C", cwd, prompt,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "codex exec timed out"
    return proc.returncode or 0, out.decode(errors="replace")


async def generate(prompt: str, size: str | None, *,
                   accounts: dict[str, Codex_account_config],
                   state: dict[str, Codex_account],
                   codex_bin: str = "codex", model: str = "gpt-6.1-sol",
                   effort: str = "low", timeout_s: float = 300,
                   threshold: float = 0.8, threshold_7d: float = 0.98,
                   runner: Runner = run_codex) -> dict:
    """Generate one image, rotating across Codex accounts on spent quota or a
    lost login. Mutates `state` (per-account telemetry) in place; the caller
    persists it. Returns {"ok", "account", "png", "elapsed_s", "tried",
    "error", "retry_after"}."""
    tried: list[str] = []
    started = time.time()
    last_error = "no usable codex account"
    while True:
        now = time.time()
        name = pick_codex_account(accounts, state, threshold, threshold_7d, now,
                                  exclude=set(tried))
        if name is None:
            break
        tried.append(name)
        home = Path(accounts[name].codex_home).expanduser()
        rc, output = await runner(codex_bin, home, model, effort,
                                  build_prompt(prompt, size), timeout_s)
        info = state.setdefault(name, Codex_account())
        info.last_seen = time.time()
        tid = thread_id_from(output)
        if tid:
            for key, value in parse_rate_limits(read_session(home, tid)).items():
                setattr(info, key, value)
            png = find_image(home, tid)
            if png is not None:
                info.benched_until = 0.0
                return {"ok": True, "account": name, "png": png.read_bytes(),
                        "elapsed_s": round(time.time() - started, 1), "tried": tried}
        kind = classify_failure(output)
        last_error = output.strip().splitlines()[-1][:300] if output.strip() else f"exit {rc}"
        if kind == "quota":
            info.util_5h = max(info.util_5h, 1.0)
            if info.reset_5h is None:
                info.reset_5h = time.time() + 3600
            continue
        if kind == "auth":
            info.benched_until = time.time() + AUTH_BACKOFF_S
            info.last_error = last_error
            continue
        return {"ok": False, "account": name, "error": last_error, "tried": tried,
                "elapsed_s": round(time.time() - started, 1)}

    resets = [state.get(n, Codex_account()).reset_5h or 0 for n in accounts]
    future = [r for r in resets if r > time.time()]
    retry_after = max(1, round(min(future) - time.time())) if future else 300
    return {"ok": False, "account": None, "error": last_error, "tried": tried,
            "retry_after": retry_after, "elapsed_s": round(time.time() - started, 1)}


def to_openai(result: dict) -> dict:
    return {"created": int(time.time()),
            "data": [{"b64_json": base64.b64encode(result["png"]).decode()}]}
