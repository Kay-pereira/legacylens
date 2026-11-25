"""Hosted LLM adapters speaking the OpenAI-compatible chat completions API.

The adapter is intentionally vendor-neutral: any endpoint that accepts the standard
``/chat/completions`` request shape works by changing ``base_url`` and ``model``. The API
key is sent as a bearer token when present and is never logged or stored in cache keys.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import httpx

from ..settings import LLMConfigurationError, LLMSettings
from .base import (
    Completion,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
)

__all__ = ["HostedProvider"]

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class HostedProvider:
    """OpenAI-compatible provider over HTTPS."""

    name = "openai"

    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.resolved_base_url:
            raise LLMConfigurationError("base_url is required for the hosted provider")
        self.settings = settings
        self._transport = transport

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
        settings = self.settings
        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if top_p is not None:
            payload["top_p"] = top_p
        if seed is not None:
            payload["seed"] = seed
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"

        url = f"{settings.resolved_base_url}/chat/completions"
        response = self._post(
            url,
            headers=headers,
            payload=payload,
            timeout=timeout if timeout is not None else settings.timeout,
        )
        return self._parse(response)

    def _post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        settings = self.settings
        last_error: ProviderTimeoutError | None = None
        for attempt in range(settings.retries + 1):
            try:
                with httpx.Client(
                    timeout=timeout,
                    transport=self._transport,
                ) as client:
                    response = client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeoutError(
                    f"hosted provider timed out after {timeout:g}s"
                )
                if attempt < settings.retries:
                    self._backoff(attempt)
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                if attempt < settings.retries:
                    self._backoff(attempt)
                    continue
                raise ProviderResponseError(f"hosted provider request failed: {exc}") from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < settings.retries:
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise ProviderHTTPError(
                    f"hosted provider returned HTTP {response.status_code}: "
                    f"{response.text[:500]}",
                    status_code=response.status_code,
                )
            return response

        raise last_error or ProviderResponseError("hosted provider request failed")

    def _backoff(self, attempt: int) -> None:
        delay = self.settings.retry_backoff * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _parse(self, response: httpx.Response) -> Completion:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderResponseError("hosted provider returned invalid JSON") from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("hosted provider response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError("hosted provider response has no message")
        text = _content_text(message.get("content"))
        if text is None:
            raise ProviderResponseError("hosted provider response has no text content")

        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = _int(usage.get("prompt_tokens"))
        completion_tokens = _int(usage.get("completion_tokens"))
        settings = self.settings
        cost = (
            prompt_tokens / 1000 * settings.prompt_cost_per_1k
            + completion_tokens / 1000 * settings.completion_cost_per_1k
        )
        finish_reason = choices[0].get("finish_reason")
        return Completion(
            text=text,
            provider=self.name,
            model=str(data.get("model") or settings.model or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=round(cost, 8),
            finish_reason=str(finish_reason) if finish_reason is not None else None,
        )


def _content_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "".join(parts)
    return None


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
