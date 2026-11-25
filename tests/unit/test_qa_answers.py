"""Q&A answer assembly: extractive answers, LLM validation and status policy."""

from __future__ import annotations

import json
from typing import Literal

import pytest

from legacylens.ir import Confidence, FactStore
from legacylens.llm import Completion, LLMSettings, NullProvider
from legacylens.qa import AnswerEngine, RetrievalIndex


class FakeProvider:
    """A deterministic provider that returns one canned JSON response."""

    name = "openai"

    def __init__(self, payload: object) -> None:
        self.settings = LLMSettings(provider="openai", model="fake-qa-model")
        self.payload = payload
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
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return Completion(
            text=text,
            provider=self.name,
            model=self.settings.model or "",
            prompt_tokens=100,
            completion_tokens=20,
        )


@pytest.fixture
def index(qa_store: FactStore) -> RetrievalIndex:
    return RetrievalIndex.build(qa_store)


def test_extractive_answer_cites_retrieved_evidence(
    qa_store: FactStore, index: RetrievalIndex
) -> None:
    engine = AnswerEngine(store=qa_store, index=index)

    answer = engine.ask("Which table does User::find read?")

    assert answer.mode == "extractive"
    assert answer.status is Confidence.VERIFIED
    assert answer.citations
    assert answer.passages
    assert "According to the indexed evidence" in answer.answer
    assert not answer.insufficient_evidence
    assert all(citation.citation for citation in answer.citations)


def test_extractive_answer_handles_no_evidence(
    qa_store: FactStore, index: RetrievalIndex
) -> None:
    engine = AnswerEngine(store=qa_store, index=index)

    answer = engine.ask("zzzzqqqq")

    assert answer.status is Confidence.UNSUPPORTED
    assert answer.insufficient_evidence is True
    assert answer.citations == ()
    assert answer.passages == ()


def test_extractive_answer_respects_kind_filters(
    qa_store: FactStore, index: RetrievalIndex
) -> None:
    engine = AnswerEngine(store=qa_store, index=index)

    answer = engine.ask("users table", kinds=["table"])

    assert answer.passages
    assert {hit.passage.kind.value for hit in answer.passages} == {"table"}


def test_blank_question_and_bad_limit_are_rejected(
    qa_store: FactStore, index: RetrievalIndex
) -> None:
    engine = AnswerEngine(store=qa_store, index=index)

    with pytest.raises(ValueError, match="question"):
        engine.ask("   ")
    with pytest.raises(ValueError, match="limit"):
        engine.ask("users", limit=0)


def test_llm_mode_requires_a_provider(qa_store: FactStore, index: RetrievalIndex) -> None:
    engine = AnswerEngine(store=qa_store, index=index, provider=NullProvider())

    with pytest.raises(ValueError, match="configured provider"):
        engine.ask("users table", mode="llm")


def test_llm_answer_with_supplied_citations_is_verified(qa_store: FactStore) -> None:
    provider = FakeProvider(
        {"answer": "User::find reads and writes the users table.", "citations": ["table:users"]}
    )
    engine = AnswerEngine(store=qa_store, index=RetrievalIndex.build(qa_store), provider=provider)

    answer = engine.ask("users table", mode="llm")

    assert answer.status is Confidence.VERIFIED
    assert answer.mode == "llm"
    assert [item.citation for item in answer.citations] == ["table:users"]
    assert answer.unresolved_citations == ()
    assert answer.provider == "openai"
    assert answer.model == "fake-qa-model"


def test_llm_answer_can_map_numeric_citation_markers(qa_store: FactStore) -> None:
    provider = FakeProvider({"answer": "Cited by number.", "citations": ["1"]})
    index = RetrievalIndex.build(qa_store)
    engine = AnswerEngine(store=qa_store, index=index, provider=provider)

    answer = engine.ask("users table", mode="llm", limit=1)

    assert answer.status is Confidence.VERIFIED
    assert answer.citations[0].citation == answer.passages[0].passage.citation


def test_llm_answer_with_unknown_citation_is_inferred(qa_store: FactStore) -> None:
    provider = FakeProvider(
        {
            "answer": "Mix of real and invented evidence.",
            "citations": ["table:users", "fn:src/Nowhere.php#missing"],
        }
    )
    engine = AnswerEngine(store=qa_store, index=RetrievalIndex.build(qa_store), provider=provider)

    answer = engine.ask("users table", mode="llm")

    assert answer.status is Confidence.INFERRED
    assert answer.unresolved_citations == ("fn:src/Nowhere.php#missing",)


def test_llm_answer_with_no_valid_citation_is_unsupported(qa_store: FactStore) -> None:
    provider = FakeProvider(
        {"answer": "No real evidence.", "citations": ["fn:src/Nowhere.php#missing"]}
    )
    engine = AnswerEngine(store=qa_store, index=RetrievalIndex.build(qa_store), provider=provider)

    answer = engine.ask("users table", mode="llm")

    assert answer.status is Confidence.UNSUPPORTED
    assert answer.insufficient_evidence is True


def test_llm_answer_marked_insufficient_is_inferred(qa_store: FactStore) -> None:
    provider = FakeProvider(
        {
            "answer": "Only part of the question is covered.",
            "citations": ["table:users"],
            "insufficient_evidence": True,
        }
    )
    engine = AnswerEngine(store=qa_store, index=RetrievalIndex.build(qa_store), provider=provider)

    answer = engine.ask("users table", mode="llm")

    assert answer.status is Confidence.INFERRED
    assert answer.insufficient_evidence is True


def test_invalid_json_and_fenced_json_are_handled(qa_store: FactStore) -> None:
    index = RetrievalIndex.build(qa_store)
    invalid = AnswerEngine(
        store=qa_store,
        index=index,
        provider=FakeProvider("this is not json"),
    )
    fenced = AnswerEngine(
        store=qa_store,
        index=index,
        provider=FakeProvider(
            '```json\n{"answer": "Fenced.", "citations": ["table:audit"]}\n```'
        ),
    )

    assert invalid.ask("users table", mode="llm").status is Confidence.UNSUPPORTED
    assert fenced.ask("audit table", mode="llm").status is Confidence.VERIFIED


def test_llm_completions_are_cached(qa_store: FactStore) -> None:
    provider = FakeProvider({"answer": "Cached.", "citations": ["table:users"]})
    engine = AnswerEngine(store=qa_store, index=RetrievalIndex.build(qa_store), provider=provider)

    first = engine.ask("users table", mode="llm")
    second = engine.ask("users table", mode="llm")

    assert first.cached is False
    assert second.cached is True
    assert provider.calls == 1


def test_answer_payload_is_json_ready(qa_store: FactStore, index: RetrievalIndex) -> None:
    payload = AnswerEngine(store=qa_store, index=index).ask("users table").as_dict()

    assert json.loads(json.dumps(payload))["status"] == "verified"
    assert payload["citations"]
    assert payload["passages"]


def test_engine_rejects_unsafe_configuration(qa_store: FactStore, index: RetrievalIndex) -> None:
    with pytest.raises(ValueError, match="max_context_chars"):
        AnswerEngine(store=qa_store, index=index, max_context_chars=10)
    with pytest.raises(ValueError, match="max_evidence"):
        AnswerEngine(store=qa_store, index=index, max_evidence=0)
