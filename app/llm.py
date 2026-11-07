"""One small interface over the LLM providers.

Every pipeline stage calls `complete(Schema, system, user)` and receives a
validated pydantic object, so the rest of the code never parses model text.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from .config import get_settings

T = TypeVar("T", bound=BaseModel)

# Models that accept the server-side refusal fallback.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """An LLM call failed. The message is written for the instructor."""


class LLMNotConfigured(LLMError):
    pass


class Provider(Protocol):
    def complete(self, schema: type[T], system: str, user: str, *, role: str, effort: str) -> T: ...
    def web_search(self, query: str, domains: list[str] | None = None) -> list[dict]: ...


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema, flattened into the form structured outputs require."""
    root = model.model_json_schema()
    defs = root.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
        out = {}
        for key, value in node.items():
            if key == "properties":  # keys here are field names, not keywords
                out[key] = {name: walk(prop) for name, prop in value.items()}
            elif key not in ("title", "default", "description", "maxLength", "minLength"):
                out[key] = walk(value)
        if out.get("type") == "object":
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out

    return walk(root)


class AnthropicProvider:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
        self.model = settings.anthropic_model
        self.qc_model = settings.qc_model or settings.anthropic_model

    def complete(self, schema: type[T], system: str, user: str, *, role: str, effort: str) -> T:
        model = self.qc_model if role == "qc" else self.model
        params: dict[str, Any] = dict(
            model=model,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": strict_schema(schema)},
            },
        )
        try:
            if model in _FALLBACK_MODELS:
                stream = self.client.beta.messages.stream(
                    betas=[_FALLBACK_BETA], fallbacks="default", **params)
            else:
                stream = self.client.messages.stream(**params)
            with stream as active:
                message = active.get_final_message()
        except anthropic.APIError as exc:
            raise _anthropic_error(exc) from exc

        if message.stop_reason == "refusal":
            raise LLMError("The model declined this request. Rephrase the lesson inputs and retry.")
        if message.stop_reason == "max_tokens":
            raise LLMError("The model ran out of output space before finishing. Retry this step.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError(f"The model returned an unexpected format: {exc.errors()[0]['msg']}") from exc

    def web_search(self, query: str, domains: list[str] | None = None) -> list[dict]:
        tool: dict[str, Any] = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
        if domains:
            tool["allowed_domains"] = domains
        messages: list[dict] = [{"role": "user", "content": f"Search the web for: {query}"}]
        results: dict[str, str] = {}
        try:
            for _ in range(3):  # a long search can pause and need resuming
                message = self.client.messages.create(
                    model=self.model, max_tokens=4000, tools=[tool], messages=messages,
                    output_config={"effort": "low"},
                )
                for block in message.content:
                    if block.type == "web_search_tool_result" and isinstance(block.content, list):
                        for hit in block.content:
                            results.setdefault(hit.url, hit.title)
                if message.stop_reason != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": message.content})
        except anthropic.APIError as exc:
            raise _anthropic_error(exc) from exc
        return [{"url": url, "title": title} for url, title in results.items()]


def _anthropic_error(exc: anthropic.APIError) -> LLMError:
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError("Anthropic rate limit reached. Wait a minute, then retry this step.")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError("Could not reach Anthropic. Check the network, then retry this step.")
    if isinstance(exc, anthropic.APIStatusError):
        return LLMError(f"Anthropic returned an error ({exc.status_code}): {exc.message}")
    return LLMError(f"Anthropic call failed: {exc}")


class OpenAICompatibleProvider:
    """OpenAI, or anything speaking its API (Gemini, Groq, OpenRouter, Ollama)."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_model:
            raise LLMNotConfigured("Set OPENAI_MODEL in .env, restart, then retry this step.")
        self.client = openai.OpenAI(
            api_key=settings.openai_api_key or "not-needed",
            base_url=settings.openai_base_url or None,
            max_retries=5,  # free tiers return 429 and 503 often
        )
        self.model = settings.openai_model
        self.qc_model = settings.qc_model or settings.openai_model

    def complete(self, schema: type[T], system: str, user: str, *, role: str, effort: str) -> T:
        contract = (
            "\n\nReturn only one JSON object that matches this JSON schema:\n"
            + json.dumps(strict_schema(schema))
        )
        messages = [{"role": "system", "content": system + contract},
                    {"role": "user", "content": user}]
        for attempt in (1, 2):
            try:
                response = self._create(
                    effort,
                    model=self.qc_model if role == "qc" else self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                )
            except openai.AuthenticationError as exc:
                raise LLMError("The provider rejected the API key. Check OPENAI_API_KEY in .env.") from exc
            except openai.RateLimitError as exc:
                if "PerDay" in exc.response.text:  # waiting a minute will not help
                    raise LLMError("Today's request quota for this model is used up. Set a different "
                                   "OPENAI_MODEL or QC_MODEL in .env and restart, or wait for the "
                                   "daily reset, then retry this step.") from exc
                raise LLMError("Provider rate limit reached. Wait a minute, then retry this step.") from exc
            except openai.APIConnectionError as exc:
                raise LLMError("Could not reach the provider. Check the network, then retry this step.") from exc
            except openai.InternalServerError as exc:
                raise LLMError(f"The provider is overloaded or down (error {exc.status_code}). "
                               "Wait a minute, then retry this step.") from exc
            except openai.APIError as exc:
                raise LLMError(f"The provider returned an error: {exc}") from exc
            text = response.choices[0].message.content or ""
            try:
                return schema.model_validate_json(text)
            except ValidationError as exc:
                if attempt == 2:
                    raise LLMError("The model returned an unexpected format twice. Retry this step.") from exc
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"That JSON was invalid: {exc}. Return corrected JSON only."}]
        raise LLMError("The model did not return a result.")

    def _create(self, effort: str, **params: Any) -> Any:
        """Request the reasoning effort, dropping it only if this server rejects the field."""
        try:
            return self.client.chat.completions.create(reasoning_effort=effort, **params)
        except openai.BadRequestError as exc:
            if "reasoning" not in str(exc).lower():
                raise
        return self.client.chat.completions.create(**params)

    def web_search(self, query: str, domains: list[str] | None = None) -> list[dict]:
        raise LLMError("Web search is not available through this provider. Set TAVILY_API_KEY.")


_override: Provider | None = None


def set_provider(provider: Provider | None) -> None:
    """Swap the provider. Used by the test suite."""
    global _override
    _override = provider


def get_provider() -> Provider:
    if _override is not None:
        return _override
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMNotConfigured(
                "No LLM key found. Set ANTHROPIC_API_KEY in .env, restart the server, then retry this step.")
        return AnthropicProvider()
    if settings.llm_provider == "openai":
        return OpenAICompatibleProvider()
    raise LLMNotConfigured(f"Unknown LLM_PROVIDER '{settings.llm_provider}'. Use 'anthropic' or 'openai'.")


def complete(schema: type[T], system: str, user: str, *, role: str = "generate", effort: str = "high") -> T:
    return get_provider().complete(schema, system, user, role=role, effort=effort)


def llm_ready() -> tuple[bool, str]:
    try:
        get_provider()
        return True, ""
    except LLMError as exc:
        return False, str(exc)
