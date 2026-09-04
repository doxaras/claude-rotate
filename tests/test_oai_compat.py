"""Offline tests for the OpenAI <-> Anthropic translation layer. No config, no
network — oai_compat.py imports clean by design.

  python3 -m pytest tests/ -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from oai_compat import SseToOai, anthropic_to_oai, oai_to_anthropic


def test_request_translation_full_shape():
    body, err = oai_to_anthropic({
        "model": "claude-sonnet-5",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "system", "content": "in greek"},
            {"role": "user", "content": [{"type": "text", "text": "γεια"},
                                         {"type": "text", "text": " σου"}]},
            {"role": "assistant", "content": "γεια!"},
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},        # consecutive same-role: merged
        ],
        "max_tokens": 128, "temperature": 0.5, "top_p": 0.9, "stop": "END",
        "stream": True,
    })
    assert err is None
    assert body["system"] == "be terse\n\nin greek"
    assert body["messages"] == [
        {"role": "user", "content": "γεια σου"},
        {"role": "assistant", "content": "γεια!"},
        {"role": "user", "content": "a\n\nb"},
    ]
    assert body["max_tokens"] == 128 and body["temperature"] == 0.5
    assert body["top_p"] == 0.9 and body["stop_sequences"] == ["END"]
    assert body["stream"] is True


def test_request_defaults_and_max_completion_tokens():
    body, err = oai_to_anthropic({"model": "m", "max_completion_tokens": 64,
                                  "messages": [{"role": "user", "content": "hi"}]})
    assert err is None
    assert body["max_tokens"] == 64 and body["stream"] is False
    assert "temperature" not in body and "system" not in body


def test_tools_and_unknown_roles_are_refused_not_mistranslated():
    _, err = oai_to_anthropic({"messages": [{"role": "user", "content": "x"}],
                               "tools": [{"type": "function"}]})
    assert err and "tool" in err
    _, err = oai_to_anthropic({"messages": [{"role": "tool", "content": "x"}]})
    assert err and "role" in err
    _, err = oai_to_anthropic({"messages": []})
    assert err


def test_response_translation_and_finish_reasons():
    msg = {"id": "msg_01", "model": "claude-sonnet-5", "stop_reason": "max_tokens",
           "content": [{"type": "text", "text": "hello "},
                       {"type": "text", "text": "world"}],
           "usage": {"input_tokens": 10, "output_tokens": 5,
                     "cache_read_input_tokens": 100,
                     "cache_creation_input_tokens": 20}}
    out = anthropic_to_oai(msg)
    assert out["object"] == "chat.completion" and out["id"] == "msg_01"
    assert out["choices"][0]["message"]["content"] == "hello world"
    assert out["choices"][0]["finish_reason"] == "length"
    # cache reads/writes fold into prompt_tokens so caller cost math stays sane
    assert out["usage"] == {"prompt_tokens": 130, "completion_tokens": 5,
                            "total_tokens": 135}
    assert anthropic_to_oai({"stop_reason": "end_turn"}
                            )["choices"][0]["finish_reason"] == "stop"


def _events():
    return [
        'event: message_start',
        'data: {"type":"message_start","message":{"id":"msg_9","model":"claude-sonnet-5",'
        '"usage":{"input_tokens":12,"cache_read_input_tokens":3}}}',
        '',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"He"}}',
        '',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"llo"}}',
        '',
        'event: message_delta',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":7}}',
        '',
        'data: {"type":"message_stop"}',
        '',
    ]


def _parse_chunks(s: str) -> list:
    out = []
    for line in s.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:]))
    return out


def test_sse_translation_survives_arbitrary_chunk_boundaries():
    stream = "\n".join(_events()) + "\n"
    for size in (1, 3, 7, len(stream)):          # split mid-line, mid-utf8-safe
        tr = SseToOai()
        got = ""
        for i in range(0, len(stream), size):
            got += tr.feed(stream[i:i + size])
        got += tr.tail()
        chunks = _parse_chunks(got)
        # role chunk, two text deltas, final finish chunk, usage chunk
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        text = "".join(c["choices"][0]["delta"].get("content", "")
                       for c in chunks if c["choices"])
        assert text == "Hello"
        assert chunks[0]["id"] == "msg_9" and chunks[0]["model"] == "claude-sonnet-5"
        final = [c for c in chunks if c["choices"]
                 and c["choices"][0]["finish_reason"]][-1]
        assert final["choices"][0]["finish_reason"] == "stop"
        usage = [c for c in chunks if not c["choices"]][-1]["usage"]
        assert usage == {"prompt_tokens": 15, "completion_tokens": 7,
                         "total_tokens": 22}
        assert got.rstrip().endswith("data: [DONE]")
        # the accumulated Anthropic-style usage feeds the audit record
        assert tr.usage["output_tokens"] == 7 and tr.usage["input_tokens"] == 12
