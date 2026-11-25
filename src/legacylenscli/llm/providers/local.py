"""Local LLM adapter using Ollama's ``/api/chat`` endpoint.

No API key is required for a local Ollama daemon. If one is configured it is forwarded as
a bearer token, which keeps the adapter usable behind an authenticated reverse proxy.
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

__all__ = ["LocalProvider"]

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LocalProvider:
    """Ollama provider with zero cost accounting."""

    name = "ollama"

    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.resolved_base_url:
            raise LLMConfigurationError("base_url is required for the Ollama provider")
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
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if top_p is not None:
            options["top_p"] = top_p
        if seed is not None:
            options["seed"] = seed

        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": options,
        }
        if response_format == "json":
            payload["format"] = "json"

        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"

        url = f"{settings.resolved_base_url}/api/chat"
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
                    f"Ollama provider timed out after {timeout:g}s"
                )
                if attempt < settings.retries:
                    self._backoff(attempt)
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                if attempt < settings.retries:
                    self._backoff(attempt)
                    continue
                raise ProviderResponseError(f"Ollama provider request failed: {exc}") from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < settings.retries:
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise ProviderHTTPError(
                    f"Ollama provider returned HTTP {response.status_code}: "
                    f"{response.text[:500]}",
                    status_code=response.status_code,
                )
            return response

        raise last_error or ProviderResponseError("Ollama provider request failed")

    def _backoff(self, attempt: int) -> None:
        delay = self.settings.retry_backoff * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _parse(self, response: httpx.Response) -> Completion:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderResponseError("Ollama provider returned invalid JSON") from exc

        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ProviderResponseError("Ollama provider response has no text content")

        prompt_tokens = _int(data.get("prompt_eval_count"))
        completion_tokens = _int(data.get("eval_count"))
        done_reason = data.get("done_reason")
        return Completion(
            text=message["content"],
            provider=self.name,
            model=str(data.get("model") or self.settings.model or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=0.0,
            finish_reason=str(done_reason) if done_reason is not None else None,
        )


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
