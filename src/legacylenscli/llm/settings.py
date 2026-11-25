"""Provider-agnostic LLM settings and environment resolution.

The LLM stage is optional. Settings are deliberately explicit so the same configuration
always produces the same prompts, cache keys and cost accounting:

* ``provider`` selects the adapter (``none``, ``openai`` or ``ollama``);
* ``base_url`` supports any OpenAI-compatible endpoint, not just the hosted default;
* ``api_key`` is optional and is never included in ``repr``, ``as_dict`` or cache keys.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "LLMConfigurationError",
    "LLMSettings",
    "ProviderName",
]


class LLMConfigurationError(ValueError):
    """Raised when LLM settings are incomplete or invalid."""


ProviderName = Literal["none", "openai", "ollama"]
_PROVIDERS = frozenset({"none", "openai", "ollama"})
_RESPONSE_FORMATS = frozenset({"text", "json"})
_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434",
}


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Configuration shared by every provider.

    Sampling values are passed through to the adapter. Prices are optional and expressed
    in USD per 1,000 tokens; unknown prices stay at zero rather than being guessed.
    """

    provider: ProviderName = "none"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 60.0
    retries: int = 2
    retry_backoff: float = 0.5
    temperature: float = 0.0
    max_tokens: int | None = None
    top_p: float | None = None
    seed: int | None = None
    response_format: str = "text"
    prompt_cost_per_1k: float = 0.0
    completion_cost_per_1k: float = 0.0

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise LLMConfigurationError(
                f"unknown llm provider {self.provider!r}; expected one of "
                f"{sorted(_PROVIDERS)}"
            )
        if self.timeout <= 0:
            raise LLMConfigurationError("timeout must be > 0")
        if self.retries < 0:
            raise LLMConfigurationError("retries must be >= 0")
        if self.retry_backoff < 0:
            raise LLMConfigurationError("retry_backoff must be >= 0")
        if not 0.0 <= self.temperature <= 2.0:
            raise LLMConfigurationError("temperature must be between 0 and 2")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise LLMConfigurationError("max_tokens must be >= 1")
        if self.top_p is not None and not 0.0 <= self.top_p <= 1.0:
            raise LLMConfigurationError("top_p must be between 0 and 1")
        if self.response_format not in _RESPONSE_FORMATS:
            raise LLMConfigurationError(
                f"unknown response_format {self.response_format!r}; expected text or json"
            )
        if self.prompt_cost_per_1k < 0 or self.completion_cost_per_1k < 0:
            raise LLMConfigurationError("token prices must be >= 0")
        if self.provider != "none" and not self.model:
            raise LLMConfigurationError(
                f"model is required when provider is {self.provider!r}"
            )

    @property
    def resolved_base_url(self) -> str:
        """Return the configured base URL or the provider default."""

        if self.base_url:
            return self.base_url.rstrip("/")
        return _DEFAULT_BASE_URLS.get(self.provider, "")

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly settings with the API key redacted."""

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": "***" if self.api_key else None,
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_backoff": self.retry_backoff,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "seed": self.seed,
            "response_format": self.response_format,
            "prompt_cost_per_1k": self.prompt_cost_per_1k,
            "completion_cost_per_1k": self.completion_cost_per_1k,
        }

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        **overrides: object,
    ) -> LLMSettings:
        """Build settings from environment variables and explicit overrides.

        ``LEGACYLENS_*`` variables win over provider-specific fallbacks. Explicit keyword
        overrides win over both, which lets the CLI pass flags without mutating ``os.environ``.
        """

        values = os.environ if environ is None else environ
        provider = (
            values.get("LEGACYLENS_LLM_PROVIDER")
            or values.get("LEGACYLENS_PROVIDER")
            or "none"
        )
        api_key = (
            values.get("LEGACYLENS_API_KEY")
            or values.get("OPENAI_API_KEY")
            or values.get("OLLAMA_API_KEY")
        )
        settings = {
            "provider": provider,
            "model": values.get("LEGACYLENS_LLM_MODEL")
            or values.get("LEGACYLENS_MODEL"),
            "base_url": values.get("LEGACYLENS_LLM_BASE_URL")
            or values.get("LEGACYLENS_BASE_URL"),
            "api_key": api_key,
        }
        settings.update(overrides)
        return cls(**settings)  # type: ignore[arg-type]
