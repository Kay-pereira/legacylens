"""Explanation service: prompt -> provider -> accepted claims -> store, with caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import ClaimOrigin, FactStore
from legacylens.llm import (
    Completion,
    LLMSettings,
    NullProvider,
    explain_function,
)
from legacylens.parsers import parse_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_web"
FUNCTION = "fn:public/inc/helpers.php#search"


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
        assert "Source: public/inc/helpers.php:8-" in prompt
        return Completion(
            text=json.dumps(
                {
                    "summary": "Searches articles.",
                    "claims": [
                        {
                            "kind": "behaviour",
                            "text": "Searches the articles table by title.",
                            "citations": ["public/inc/helpers.php:10"],
                        }
                    ],
                }
            ),
            provider=self.name,
            model="test-model",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001,
        )


def _store_from(root: Path, db: Path) -> FactStore:
    scan = scan_repository(root, ScanOptions())
    store = FactStore(db)
    for scanned in scan.files:
        store.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
    return store


def test_explain_function_stores_claims_and_reuses_cache(tmp_path: Path) -> None:
    settings = LLMSettings(provider="openai", model="test-model", retries=0)
    provider = FakeProvider(settings)

    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        first = explain_function(store, FUNCTION, provider, source_root=FIXTURE)
        second = explain_function(store, FUNCTION, provider, source_root=FIXTURE)
        claims = store.list_claims(subject=FUNCTION)
        stats = store.cache_stats()

    assert first.stored is True
    assert len(first.claims) == 1
    assert first.claims[0].origin is ClaimOrigin.LLM
    assert first.claims[0].citations == ("public/inc/helpers.php:10",)
    assert second.completion.cached is True
    assert provider.calls == 1
    assert len(claims) == 1
    assert stats.entries == 1
    assert stats.hits == 1


def test_null_provider_does_not_store_or_replace_claims(tmp_path: Path) -> None:
    provider = NullProvider(LLMSettings())

    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        result = explain_function(store, FUNCTION, provider, source_root=FIXTURE)
        claims = store.list_claims(subject=FUNCTION)

    assert result.claims == ()
    assert result.stored is False
    assert claims == []
