"""Runs the real Anthropic SDK code path against a stub transport.

No network and no key. It verifies what we send and how we read the stream, not
how the live API behaves.
"""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from app import llm
from app.schemas import QueryPlan

PLAN = {"subtopics": ["a"], "wikipedia_queries": ["b"], "web_queries": ["c"],
        "book_queries": ["d"], "video_queries": ["e"]}


def sse(text: str, stop_reason: str = "end_turn") -> bytes:
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 0}}
    events = [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def provider(handler, monkeypatch) -> llm.AnthropicProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.delenv("QC_MODEL", raising=False)
    built = llm.AnthropicProvider()
    built.client = anthropic.Anthropic(
        api_key="test-key", base_url="http://stub.test", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return built


def stream_response(body: bytes) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def test_request_shape_and_parsed_result(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return stream_response(sse(json.dumps(PLAN)))

    result = provider(handler, monkeypatch).complete(QueryPlan, "system", "user", role="generate", effort="high")
    assert isinstance(result, QueryPlan) and result.subtopics == ["a"]

    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["stream"] is True
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert "temperature" not in body and "thinking" not in body


def test_qc_role_uses_the_qc_model(monkeypatch):
    seen = {}

    def handler(request):
        seen["model"] = json.loads(request.content)["model"]
        return stream_response(sse(json.dumps(PLAN)))

    built = provider(handler, monkeypatch)
    built.qc_model = "claude-sonnet-5-5"
    built.complete(QueryPlan, "s", "u", role="qc", effort="high")
    assert seen["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("stop_reason, expected", [("refusal", "declined"), ("max_tokens", "out of output space")])
def test_incomplete_responses_become_readable_errors(monkeypatch, stop_reason, expected):
    built = provider(lambda r: stream_response(sse("", stop_reason)), monkeypatch)
    with pytest.raises(llm.LLMError, match=expected):
        built.complete(QueryPlan, "s", "u", role="generate", effort="high")


def test_malformed_output_is_an_error_not_a_crash(monkeypatch):
    built = provider(lambda r: stream_response(sse("not json")), monkeypatch)
    with pytest.raises(llm.LLMError, match="unexpected format"):
        built.complete(QueryPlan, "s", "u", role="generate", effort="high")


@pytest.mark.parametrize("status, expected", [(401, "rejected the API key"), (429, "rate limit"), (500, "error \\(500\\)")])
def test_http_errors_become_readable_messages(monkeypatch, status, expected):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "nope"}})

    with pytest.raises(llm.LLMError, match=expected):
        provider(handler, monkeypatch).complete(QueryPlan, "s", "u", role="generate", effort="high")


def test_missing_key_gives_setup_instructions(monkeypatch):
    llm.set_provider(None)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(llm.LLMNotConfigured, match="ANTHROPIC_API_KEY"):
        llm.get_provider()
