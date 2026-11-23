"""Deterministic completion cache over the project's SQLite store.

The key covers everything that can change the response: provider, model, endpoint,
sampling settings and the full system/user prompt. API keys are never part of the key.
Cache hits preserve the original usage and cost so evaluation totals stay honest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from legacylens.ir import CachedCompletion, FactStore

from .providers.base import Completion, Provider
from .settings import LLMSettings

__all__ = [
    "cache_key",
    "complete_cached",
    "prompt_hash",
]


def prompt_hash(system: str, prompt: str) -> str:
    """Return the prompt-only hash recorded on generated claims."""

    digest = hashlib.sha256()
    digest.update(system.encode("utf-8"))
    digest.update(b"\n---\n")
    digest.update(prompt.encode("utf-8"))
    return digest.hexdigest()


def cache_key(
    system: str,
    prompt: str,
    settings: LLMSettings,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    seed: int | None = None,
    response_format: str | None = None,
) -> str:
    """Return the SHA-256 cache key for one completion request."""

    payload: dict[str, Any] = {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.resolved_base_url,
        "system": system,
        "prompt": prompt,
        "temperature": settings.temperature if temperature is None else temperature,
        "max_tokens": settings.max_tokens if max_tokens is None else max_tokens,
        "top_p": settings.top_p if top_p is None else top_p,
        "seed": settings.seed if seed is None else seed,
        "response_format": (
            settings.response_format if response_format is None else response_format
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def complete_cached(
    store: FactStore,
    provider: Provider,
    system: str,
    prompt: str,
    *,
    settings: LLMSettings | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    seed: int | None = None,
    response_format: str | None = None,
) -> Completion:
    """Return a completion, reusing the stored response when the key matches."""

    resolved = settings or provider.settings
    request = {
        "temperature": resolved.temperature if temperature is None else temperature,
        "max_tokens": resolved.max_tokens if max_tokens is None else max_tokens,
        "top_p": resolved.top_p if top_p is None else top_p,
        "seed": resolved.seed if seed is None else seed,
        "response_format": (
            resolved.response_format if response_format is None else response_format
        ),
    }
    key = cache_key(system, prompt, resolved, **request)
    cached = store.get_cached_completion(key)
    if cached is not None:
        return Completion(
            text=cached.response,
            provider=cached.provider or provider.name,
            model=cached.model or resolved.model or "",
            prompt_tokens=cached.prompt_tokens,
            completion_tokens=cached.completion_tokens,
            cost_usd=cached.cost_usd,
            finish_reason="cached",
            cached=True,
        )

    completion = provider.complete(
        system,
        prompt,
        temperature=request["temperature"],  # type: ignore[arg-type]
        max_tokens=request["max_tokens"],  # type: ignore[arg-type]
        top_p=request["top_p"],  # type: ignore[arg-type]
        seed=request["seed"],  # type: ignore[arg-type]
        response_format=request["response_format"],  # type: ignore[arg-type]
    )
    if provider.name != "none":
        store.put_cached_completion(
            CachedCompletion(
                cache_key=key,
                response=completion.text,
                provider=completion.provider,
                model=completion.model,
                prompt_hash=prompt_hash(system, prompt),
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                cost_usd=completion.cost_usd,
            )
        )
    return completion
