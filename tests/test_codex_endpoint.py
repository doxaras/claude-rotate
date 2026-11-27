"""HTTP-layer tests for POST /v1/images/generations through the real ASGI app,
with `codex exec` faked (codex_image.run_codex patched): auth, validation,
success shape, rotation on spent quota, 503 + Retry-After when all are spent,
and the codex block in /rotate/status.

  python3 tests/test_codex_endpoint.py
"""
import asyncio
import base64
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_image
import rotator as R

PASS = 0
PNG = b"\x89PNG\r\n\x1a\nfake"


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.save_state = lambda: None
R.DEVICE_BY_KEY = {"devkey-1": "aimedia"}
R.CFG.devices = {"aimedia": "devkey-1"}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
for n in ("p1", "p2"):
    (tmp / n).mkdir()
R.CODEX_ACCOUNTS = {n: R.codex_image.Codex_account_config(name=n, codex_home=str(tmp / n), priority=p)
                    for n, p in (("p1", 1), ("p2", 2))}
MODE = {}


async def fake_run(_bin, home, _model, _effort, prompt, _timeout):
    tid = f"t-{home.name}-{time.time_ns()}"
    out = json.dumps({"type": "thread.started", "thread_id": tid})
    if MODE[home.name] == "quota":
        return 1, out + "\nusage_limit_reached"
    (home / "generated_images" / tid).mkdir(parents=True)
    (home / "generated_images" / tid / "x.png").write_bytes(PNG)
    return 0, out


codex_image.run_codex = fake_run


def call(path, key="devkey-1", body=None, method="POST"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app),
                                     base_url="http://t") as c:
            return await c.request(method, path, json=body,
                                   headers={"authorization": f"Bearer {key}"})
    return asyncio.run(go())


def reset():
    R.STATE = {"active_account": None, "accounts": {}, "events": [], "codex": {}}
    R.AUDIT_PATH.write_text("")


reset()
r = call("/v1/images/generations", key="nope", body={"prompt": "x"})
check("unknown device key -> 401", r.status_code == 401)
r = call("/v1/images/generations", body={"prompt": "  "})
check("empty prompt -> 400", r.status_code == 400)
r = call("/v1/images/generations", body={"prompt": "x", "response_format": "url"})
check("url response_format -> 400", r.status_code == 400)

MODE.update(p1="ok", p2="ok")
r = call("/v1/images/generations", body={"prompt": "hero", "size": "1536x1024"})
check("success -> 200 b64 png", r.status_code == 200
      and base64.b64decode(r.json()["data"][0]["b64_json"]) == PNG)
check("success served by preferred account", r.headers["x-rotate-account"] == "p1")
rec = json.loads(R.AUDIT_PATH.read_text().splitlines()[-1])
check("audit records image call", rec["kind"] == "image" and rec["status"] == 200
      and rec["device"] == "aimedia")

reset()
MODE.update(p1="quota", p2="ok")
r = call("/v1/images/generations", body={"prompt": "hero"})
check("spent quota rotates to next account", r.status_code == 200
      and r.headers["x-rotate-account"] == "p2")
check("audit counts the retry",
      json.loads(R.AUDIT_PATH.read_text().splitlines()[-1]).get("retries") == 1)

reset()
MODE.update(p1="quota", p2="quota")
r = call("/v1/images/generations", body={"prompt": "hero"})
check("all spent -> 503 + retry-after", r.status_code == 503
      and int(r.headers["retry-after"]) >= 1)

s = call("/rotate/status", method="GET").json()
check("status lists codex accounts", set(s["codex_accounts"]) == {"p1", "p2"}
      and s["codex_accounts"]["p1"]["usable"] is False)

R.ACCOUNTS = {}
r = call("/v1/messages", body={"model": "m"})
check("codex-only instance: claude proxy -> 503", r.status_code == 503)

print(f"{PASS} passed")
