"""Provider adapters: request shape, response parsing, retries and configuration."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from legacylenscli.llm import (
    HostedProvider,
    LLMConfigurationError,
    LLMSettings,
    LocalProvider,
    NullProvider,
    ProviderHTTPError,
    build_provider,
)


def _hosted_settings(**overrides: Any) -> LLMSettings:
    values = {
        "provider": "openai",
        "model": "test-model",
        "base_url": "https://llm.example/v1",
        "api_key": "secret-key",
        "retries": 0,
        "retry_backoff": 0.0,
        "prompt_cost_per_1k": 0.01,
        "completion_cost_per_1k": 0.02,
    }
    values.update(overrides)
    return LLMSettings(**values)


def _ollama_settings(**overrides: Any) -> LLMSettings:
    values = {
        "provider": "ollama",
        "model": "llama-test",
        "base_url": "http://localhost:11434",
        "retries": 0,
        "retry_backoff": 0.0,
    }
    values.update(overrides)
    return LLMSettings(**values)


def test_hosted_provider_sends_chat_completion_and_parses_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "test-model-2026",
                "choices": [
                    {
                        "message": {"content": '{"claims": []}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    provider = HostedProvider(
        _hosted_settings(),
        transport=httpx.MockTransport(handler),
    )
    completion = provider.complete(
        "system",
        "prompt",
        temperature=0.2,
        max_tokens=100,
        top_p=0.9,
        seed=7,
        response_format="json",
    )

    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["payload"]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "prompt"},
    ]
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["max_tokens"] == 100
    assert seen["payload"]["seed"] == 7
    assert completion.text == '{"claims": []}'
    assert completion.provider == "openai"
    assert completion.model == "test-model-2026"
    assert completion.prompt_tokens == 10
    assert completion.completion_tokens == 5
    assert completion.cost_usd == pytest.approx(0.0002)
    assert completion.finish_reason == "stop"


def test_hosted_provider_retries_transient_failures() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="try later")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    provider = HostedProvider(
        _hosted_settings(retries=1),
        transport=httpx.MockTransport(handler),
    )
    completion = provider.complete("s", "p")

    assert completion.text == "ok"
    assert calls["count"] == 2


def test_hosted_provider_raises_typed_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    provider = HostedProvider(
        _hosted_settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderHTTPError) as error:
        provider.complete("s", "p")
    assert error.value.status_code == 401


def test_ollama_provider_uses_chat_api_and_zero_cost() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "llama-test",
                "message": {"content": '{"claims": []}'},
                "prompt_eval_count": 7,
                "eval_count": 3,
                "done_reason": "stop",
            },
        )

    provider = LocalProvider(
        _ollama_settings(),
        transport=httpx.MockTransport(handler),
    )
    completion = provider.complete(
        "system",
        "prompt",
        temperature=0.1,
        max_tokens=64,
        top_p=0.8,
        seed=3,
        response_format="json",
    )

    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["payload"]["format"] == "json"
    assert seen["payload"]["stream"] is False
    assert seen["payload"]["options"] == {
        "temperature": 0.1,
        "num_predict": 64,
        "top_p": 0.8,
        "seed": 3,
    }
    assert completion.text == '{"claims": []}'
    assert completion.prompt_tokens == 7
    assert completion.completion_tokens == 3
    assert completion.cost_usd == 0.0


def test_null_provider_and_factory() -> None:
    settings = LLMSettings()
    provider = build_provider(settings)

    assert isinstance(provider, NullProvider)
    completion = provider.complete("s", "p")
    assert completion.text == ""
    assert completion.finish_reason == "disabled"


def test_provider_requires_a_model() -> None:
    with pytest.raises(LLMConfigurationError, match="model"):
        LLMSettings(provider="openai")
