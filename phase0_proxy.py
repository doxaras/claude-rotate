"""Phase 0 harness: transparent logging reverse proxy in front of api.anthropic.com.

Goal: verify two undocumented assumptions before building the real rotator:
  1. Claude Code + subscription setup-token works through ANTHROPIC_BASE_URL.
  2. Max-subscription responses carry rate-limit / utilization headers we can
     read for the "switch at 80%" rule.

Run:  python3 phase0_proxy.py            (listens on 127.0.0.1:8484)
Logs: logs/phase0.jsonl  — one record per request with method, path, model,
      status, every anthropic-ratelimit*/x-ratelimit*/retry-after header,
      and any usage blocks seen in the response (JSON or SSE).
"""

import json
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

UPSTREAM = "https://api.anthropic.com"
LOG_PATH = Path(__file__).parent / "logs" / "phase0.jsonl"

# Hop-by-hop headers that must not be forwarded either direction.
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    # force identity encoding so we can inspect bodies
    "accept-encoding",
}

INTERESTING = ("anthropic-ratelimit", "x-ratelimit", "retry-after", "request-id", "anthropic-organization-id")

app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=15.0))


def log_record(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def extract_usage_from_sse(chunk_text: str, sink: list) -> None:
    """Pull usage objects out of SSE data lines (message_start / message_delta)."""
    for line in chunk_text.splitlines():
        if line.startswith("data:") and '"usage"' in line:
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage") or obj.get("message", {}).get("usage")
            if usage:
                sink.append({"event_type": obj.get("type"), "usage": usage})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    # httpx would otherwise inject its own accept-encoding and auto-decompress,
    # while we relay upstream's content-encoding header — force identity end-to-end.
    fwd_headers["accept-encoding"] = "identity"

    # Request-side metadata worth keeping
    model, stream_req = None, None
    if body:
        try:
            payload = json.loads(body)
            model = payload.get("model")
            stream_req = payload.get("stream")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            pass

    rec = {
        "method": request.method,
        "path": "/" + path,
        "query": str(request.url.query) or None,
        "model": model,
        "stream": stream_req,
        "auth_kind": ("bearer" if request.headers.get("authorization", "").lower().startswith("bearer")
                      else "x-api-key" if "x-api-key" in request.headers else "none"),
        "req_beta": request.headers.get("anthropic-beta"),
        "user_agent": request.headers.get("user-agent"),
    }

    upstream_req = client.build_request(
        request.method, "/" + path + ("?" + str(request.url.query) if request.url.query else ""),
        headers=fwd_headers, content=body,
    )
    upstream = await client.send(upstream_req, stream=True)

    rec["status"] = upstream.status_code
    rec["resp_headers"] = {
        k: v for k, v in upstream.headers.items()
        if any(k.lower().startswith(p) or k.lower() == p for p in INTERESTING)
    }
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP and k.lower() != "content-encoding"}
    content_type = upstream.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        usage_events: list = []

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    extract_usage_from_sse(chunk.decode("utf-8", errors="replace"), usage_events)
                    yield chunk
            finally:
                await upstream.aclose()
                rec["usage_events"] = usage_events
                log_record(rec)

        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 headers=resp_headers, media_type=content_type)

    content = await upstream.aread()
    await upstream.aclose()
    try:
        parsed = json.loads(content)
        rec["usage"] = parsed.get("usage")
        if upstream.status_code >= 400:
            rec["error_body"] = parsed
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    log_record(rec)
    return Response(content=content, status_code=upstream.status_code, headers=resp_headers)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8484, log_level="warning")
