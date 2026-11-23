"""Provider interface shared by hosted and local LLM adapters.

Adapters never parse claims or touch the store: they accept a system prompt and a user
prompt, perform one completion, and return text plus usage metadata. Caching, context
assembly and claim parsing live above this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..settings import LLMSettings

__all__ = [
    "Completion",
    "NullProvider",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderResponseError",
    "ProviderTimeoutError",
]


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderTimeoutError(ProviderError):
    """The provider did not respond before the configured timeout."""


class ProviderHTTPError(ProviderError):
    """The provider returned a non-success HTTP status."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderResponseError(ProviderError):
    """The provider responded successfully but the payload was unusable."""


@dataclass(frozen=True, slots=True)
class Completion:
    """One provider completion with usage and cost metadata."""

    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str | None = None
    cached: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "finish_reason": self.finish_reason,
            "cached": self.cached,
        }


class Provider(Protocol):
    """Protocol implemented by every adapter."""

    name: str
    settings: LLMSettings

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
        """Return one completion for ``system`` + ``prompt``."""
        ...


class NullProvider:
    """No-op adapter used when the LLM stage is disabled."""

    name = "none"

    def __init__(self, settings: LLMSettings | None = None) -> None:
        self.settings = settings or LLMSettings(provider="none")

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
        return Completion(
            text="",
            provider=self.name,
            model="none",
            finish_reason="disabled",
        )
