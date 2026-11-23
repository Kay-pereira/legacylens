"""Parse structured LLM output into conservative, citation-bounded claims.

The parser treats model output as untrusted data. Claims without text or without a citation
that actually appeared in the prompt context are dropped. Everything accepted starts as
``inferred`` with ``origin=llm``; verification may promote or reject it later.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from typing import Any

from legacylens.ir import ClaimKind, ClaimOrigin, ClaimRecord, Confidence

__all__ = ["parse_claims"]

_DEFAULT_KIND = ClaimKind.OTHER
_MAX_CLAIMS = 20


def parse_claims(
    response: str,
    *,
    subject: str,
    model: str | None,
    prompt_hash: str | None,
    allowed_citations: Collection[str],
    max_claims: int = _MAX_CLAIMS,
) -> tuple[ClaimRecord, ...]:
    """Return accepted claims from a JSON response, dropping unusable entries."""

    if max_claims < 0:
        raise ValueError("max_claims must be >= 0")
    if not response.strip() or max_claims == 0:
        return ()

    payload = _decode(response)
    if isinstance(payload, dict):
        raw_claims = payload.get("claims")
    else:
        raw_claims = payload
    if not isinstance(raw_claims, list):
        return ()

    allowed = {item for item in allowed_citations if item}
    claims: list[ClaimRecord] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for raw in raw_claims:
        if len(claims) >= max_claims:
            break
        claim = _claim(raw, allowed)
        if claim is None:
            continue
        key = (claim.kind.value, claim.text, claim.citations)
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            ClaimRecord(
                subject=subject,
                kind=claim.kind,
                text=claim.text,
                status=Confidence.INFERRED,
                citations=claim.citations,
                origin=ClaimOrigin.LLM,
                model=model,
                prompt_hash=prompt_hash,
            )
        )
    return tuple(claims)


def _decode(response: str) -> object:
    text = response.strip()
    if text.startswith("```"):
        text = _strip_fence(text)
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                return None
        return None


def _strip_fence(text: str) -> str:
    first_newline = text.find("\n")
    if first_newline < 0:
        return text
    body = text[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def _claim(raw: object, allowed: set[str]) -> _ParsedClaim | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("text") or raw.get("claim")
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None

    citations = _citations(raw)
    if not citations:
        return None
    kept = tuple(item for item in citations if item in allowed)
    if not kept:
        return None

    kind_value = raw.get("kind")
    try:
        kind = ClaimKind(kind_value) if isinstance(kind_value, str) else _DEFAULT_KIND
    except ValueError:
        kind = _DEFAULT_KIND
    return _ParsedClaim(kind=kind, text=text, citations=kept)


def _citations(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("citations")
    if value is None:
        value = raw.get("citation")
    if isinstance(value, str):
        values: Iterable[object] = (value,)
    elif isinstance(value, list):
        values = value
    else:
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        citation = item.strip()
        if citation and citation not in seen:
            seen.add(citation)
            result.append(citation)
    return tuple(result)


class _ParsedClaim:
    __slots__ = ("kind", "text", "citations")

    def __init__(self, *, kind: ClaimKind, text: str, citations: tuple[str, ...]) -> None:
        self.kind = kind
        self.text = text
        self.citations = citations
