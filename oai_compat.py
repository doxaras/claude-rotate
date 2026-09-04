"""OpenAI chat.completions <-> Anthropic /v1/messages translation (Phase 3).

Pure functions + one incremental SSE translator, deliberately import-safe with no
config dependency so they can be unit-tested offline (rotator.py needs config.json
at import; this module needs nothing).

Scope: text conversations. Tool use is REFUSED loudly (400) rather than
mistranslated silently — the schemas differ and a wrong translation would look
like a model failure to the caller.
"""
from __future__ import annotations

import json
import time

_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


def _oai_usage(u: dict) -> dict:
    """OpenAI counts the whole prompt; Anthropic splits cache reads/writes out.
    Folding them into prompt_tokens keeps any caller-side cost math sane."""
    prompt = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
              + u.get("cache_creation_input_tokens", 0))
    out = u.get("output_tokens", 0)
    return {"prompt_tokens": prompt, "completion_tokens": out,
            "total_tokens": prompt + out}


def oai_to_anthropic(oai: dict) -> tuple[dict | None, str | None]:
    """OpenAI body -> Anthropic body, or (None, error). System/developer messages
    become the `system` param; consecutive same-role messages are merged because
    Anthropic requires alternation."""
    if oai.get("tools") or oai.get("functions") or oai.get("tool_choice"):
        return None, "tool use is not supported by this endpoint yet"
    system_parts: list[str] = []
    msgs: list[dict] = []
    for m in oai.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, list):        # OpenAI content-part arrays
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict) and p.get("type") == "text")
        content = "" if content is None else str(content)
        if role in ("system", "developer"):
            system_parts.append(content)
        elif role in ("user", "assistant"):
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + content
            else:
                msgs.append({"role": role, "content": content})
        else:
            return None, f"unsupported message role: {role!r}"
    if not msgs:
        return None, "messages[] must contain at least one user/assistant message"
    out: dict = {
        "model": oai.get("model"),
        "max_tokens": int(oai.get("max_tokens")
                          or oai.get("max_completion_tokens") or 4096),
        "messages": msgs,
        "stream": bool(oai.get("stream")),
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if (t := oai.get("temperature")) is not None:
        out["temperature"] = t
    if (p := oai.get("top_p")) is not None:
        out["top_p"] = p
    if stop := oai.get("stop"):
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    return out, None


def anthropic_to_oai(msg: dict) -> dict:
    """Anthropic message response -> OpenAI chat.completion response."""
    text = "".join(b.get("text", "") for b in msg.get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text")
    return {
        "id": msg.get("id") or "chatcmpl-rotate",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": msg.get("model"),
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": _FINISH.get(msg.get("stop_reason"), "stop")}],
        "usage": _oai_usage(msg.get("usage") or {}),
    }


class SseToOai:
    """Incremental Anthropic SSE -> OpenAI chunk translator.

    feed() takes decoded upstream text (any chunking — lines may split anywhere)
    and returns whatever OpenAI `data:` lines are ready; tail() emits the final
    chunk, a usage chunk, and [DONE]. `usage` accumulates the Anthropic-style
    usage dict across message_start/message_delta for the caller's audit record.
    """

    def __init__(self):
        self._buf = ""
        self.id = "chatcmpl-rotate"
        self.model: str | None = None
        self.created = int(time.time())
        self.usage: dict = {}
        self.finish_reason: str | None = None

    def _chunk(self, delta: dict, finish: str | None = None) -> str:
        return "data: " + json.dumps({
            "id": self.id, "object": "chat.completion.chunk",
            "created": self.created, "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    def _absorb_usage(self, u: dict | None) -> None:
        for k, v in (u or {}).items():
            if isinstance(v, (int, float)):
                self.usage[k] = v

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "message_start":
                m = obj.get("message") or {}
                self.id = m.get("id") or self.id
                self.model = m.get("model") or self.model
                self._absorb_usage(m.get("usage"))
                out.append(self._chunk({"role": "assistant", "content": ""}))
            elif t == "content_block_delta":
                d = obj.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    out.append(self._chunk({"content": d["text"]}))
            elif t == "message_delta":
                d = obj.get("delta") or {}
                if d.get("stop_reason"):
                    self.finish_reason = _FINISH.get(d["stop_reason"], "stop")
                self._absorb_usage(obj.get("usage"))
        return "".join(out)

    def tail(self) -> str:
        usage_line = "data: " + json.dumps({
            "id": self.id, "object": "chat.completion.chunk",
            "created": self.created, "model": self.model,
            "choices": [], "usage": _oai_usage(self.usage),
        }) + "\n\n"
        return (self._chunk({}, self.finish_reason or "stop")
                + usage_line + "data: [DONE]\n\n")
