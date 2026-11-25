"""LLM provider implementations and the configuration factory."""

from __future__ import annotations

from ..settings import LLMConfigurationError, LLMSettings
from .base import (
    Completion,
    NullProvider,
    Provider,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from .hosted import HostedProvider
from .local import LocalProvider

__all__ = [
    "Completion",
    "HostedProvider",
    "LLMConfigurationError",
    "LLMSettings",
    "LocalProvider",
    "NullProvider",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "build_provider",
]


def build_provider(settings: LLMSettings) -> Provider:
    """Build the adapter selected by ``settings.provider``."""

    if settings.provider == "none":
        return NullProvider(settings)
    if settings.provider == "openai":
        if not settings.model:
            raise LLMConfigurationError("model is required for the hosted provider")
        return HostedProvider(settings)
    if settings.provider == "ollama":
        if not settings.model:
            raise LLMConfigurationError("model is required for the Ollama provider")
        return LocalProvider(settings)
    raise LLMConfigurationError(f"unknown llm provider {settings.provider!r}")
