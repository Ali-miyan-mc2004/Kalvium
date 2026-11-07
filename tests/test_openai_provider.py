"""Runs the OpenAI-compatible provider against a stub transport. No network."""
from __future__ import annotations

import json

import httpx
import openai
import pytest

from app import llm
from app.schemas import QueryPlan

PLAN = {"subtopics": ["a"], "wikipedia_queries": ["b"], "web_queries": ["c"],
        "book_queries": ["d"], "video_queries": ["e"]}


def completion(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "1", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
    })


def provider(handler, monkeypatch) -> llm.OpenAICompatibleProvider:
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.delenv("QC_MODEL", raising=False)
    built = llm.OpenAICompatibleProvider()
    built.client = openai.OpenAI(api_key="k", base_url="http://stub.test", max_retries=0,
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return built


def test_schema_is_sent_and_the_result_validated(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return completion(json.dumps(PLAN))

    result = provider(handler, monkeypatch).complete(QueryPlan, "system", "user", role="generate", effort="high")
    assert result.subtopics == ["a"]
    assert seen["response_format"] == {"type": "json_object"}
    assert "wikipedia_queries" in seen["messages"][0]["content"]


def test_effort_reaches_the_provider(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return completion(json.dumps(PLAN))

    provider(handler, monkeypatch).complete(QueryPlan, "s", "u", role="qc", effort="high")
    assert seen["reasoning_effort"] == "high"


def test_server_without_reasoning_support_still_works(monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if "reasoning_effort" in bodies[-1]:
            return httpx.Response(400, json={"error": {"message": "unknown field reasoning_effort"}})
        return completion(json.dumps(PLAN))

    result = provider(handler, monkeypatch).complete(QueryPlan, "s", "u", role="qc", effort="high")
    assert result.subtopics == ["a"] and len(bodies) == 2


def test_exhausted_daily_quota_does_not_say_wait_a_minute(monkeypatch):
    body = {"error": {"message": "quota", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    built = provider(lambda r: httpx.Response(429, json=body), monkeypatch)
    with pytest.raises(llm.LLMError, match="quota for this model is used up"):
        built.complete(QueryPlan, "s", "u", role="generate", effort="high")


def test_overload_gets_a_message_that_says_to_retry(monkeypatch):
    built = provider(lambda r: httpx.Response(503, json={"error": {"message": "busy"}}), monkeypatch)
    with pytest.raises(llm.LLMError, match="overloaded"):
        built.complete(QueryPlan, "s", "u", role="generate", effort="high")


def test_invalid_json_gets_one_repair_attempt(monkeypatch):
    replies = iter(["not json", json.dumps(PLAN)])
    built = provider(lambda r: completion(next(replies)), monkeypatch)
    assert built.complete(QueryPlan, "s", "u", role="generate", effort="high").subtopics == ["a"]


def test_invalid_json_twice_is_a_readable_error(monkeypatch):
    built = provider(lambda r: completion("not json"), monkeypatch)
    with pytest.raises(llm.LLMError, match="unexpected format twice"):
        built.complete(QueryPlan, "s", "u", role="generate", effort="high")


@pytest.mark.parametrize("status, expected", [(401, "rejected the API key"), (429, "rate limit")])
def test_http_errors_become_readable_messages(monkeypatch, status, expected):
    built = provider(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}), monkeypatch)
    with pytest.raises(llm.LLMError, match=expected):
        built.complete(QueryPlan, "s", "u", role="generate", effort="high")


def test_missing_model_gives_setup_instructions(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "")
    with pytest.raises(llm.LLMNotConfigured, match="OPENAI_MODEL"):
        llm.OpenAICompatibleProvider()
