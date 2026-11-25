"""Completion cache: deterministic keys, hit accounting and null-provider bypass."""

from __future__ import annotations

from typing import Literal

from legacylenscli.ir import FactStore
from legacylenscli.llm import (
    Completion,
    LLMSettings,
    NullProvider,
    cache_key,
    complete_cached,
    prompt_hash,
)


class FakeProvider:
    name = "openai"

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.calls = 0

    def complete(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        response_format: Literal["text", "json"] = "text",
        timeout: float | None = None,
    ) -> Completion:
        self.calls += 1
        return Completion(
            text='{"claims": []}',
            provider=self.name,
            model="test",
            prompt_tokens=11,
            completion_tokens=4,
            cost_usd=0.0004,
        )


def _settings(**overrides: object) -> LLMSettings:
    values: dict[str, object] = {
        "provider": "openai",
        "model": "test",
        "api_key": "do-not-hash",
        "retries": 0,
        "temperature": 0.0,
    }
    values.update(overrides)
    return LLMSettings(**values)  # type: ignore[arg-type]


def test_prompt_hash_and_cache_key_are_deterministic() -> None:
    settings = _settings()

    assert prompt_hash("system", "prompt") == prompt_hash("system", "prompt")
    assert prompt_hash("system", "prompt") != prompt_hash("system", "other")
    assert cache_key("system", "prompt", settings) == cache_key("system", "prompt", settings)
    assert cache_key("system", "prompt", settings) != cache_key(
        "system", "prompt", _settings(temperature=0.5)
    )
    assert cache_key("system", "prompt", settings) == cache_key(
        "system", "prompt", _settings(api_key="different")
    )


def test_complete_cached_reuses_response_and_counts_hits(tmp_path) -> None:
    settings = _settings()
    provider = FakeProvider(settings)

    with FactStore(tmp_path / "facts.db") as store:
        first = complete_cached(store, provider, "system", "prompt")
        second = complete_cached(store, provider, "system", "prompt")
        stats = store.cache_stats()

    assert provider.calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.text == first.text
    assert second.prompt_tokens == 11
    assert second.completion_tokens == 4
    assert stats.entries == 1
    assert stats.hits == 1
    assert stats.prompt_tokens == 11
    assert stats.completion_tokens == 4


def test_null_provider_is_not_cached(tmp_path) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        provider = NullProvider(LLMSettings())
        completion = complete_cached(store, provider, "system", "prompt")
        stats = store.cache_stats()

    assert completion.cached is False
    assert stats.entries == 0
