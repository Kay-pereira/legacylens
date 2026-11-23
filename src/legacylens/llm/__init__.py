"""LLM stage: settings, providers, context construction, prompting and caching."""

from .cache import cache_key, complete_cached, prompt_hash
from .claims import parse_claims
from .context_builder import ContextBundle, ContextTarget, build_context
from .prompts import DEFAULT_SYSTEM_PROMPT, load_prompt, render_prompt
from .providers import (
    Completion,
    HostedProvider,
    LLMConfigurationError,
    LLMSettings,
    LocalProvider,
    NullProvider,
    Provider,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
    build_provider,
)
from .service import ExplanationResult, explain_function, explain_target

__all__ = [
    "Completion",
    "ContextBundle",
    "ContextTarget",
    "DEFAULT_SYSTEM_PROMPT",
    "ExplanationResult",
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
    "build_context",
    "build_provider",
    "cache_key",
    "complete_cached",
    "explain_function",
    "explain_target",
    "load_prompt",
    "parse_claims",
    "prompt_hash",
    "render_prompt",
]
