"""Cited answers over the retrieval index.

Two modes share one evidence contract:

* **Extractive** answers are assembled from the top-ranked passages. They require no model
  and always cite passages that were retrieved.
* **LLM** answers use the cached completion layer and the ``qa`` prompt, then validate every
  returned citation against the citations supplied to the model. A citation outside that set
  is reported as unresolved; it never silently becomes evidence.

Status labels mirror the verifier: ``verified`` when every citation resolves to supplied
evidence, ``inferred`` when some do, and ``unsupported`` when none do or the question cannot
be answered from the index.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from legacylenscli.ir import Confidence, FactStore
from legacylenscli.llm import (
    QA_SYSTEM_PROMPT,
    NullProvider,
    Provider,
    complete_cached,
    render_prompt,
)

from .retrieval import PassageKind, RetrievalIndex, SearchHit

__all__ = [
    "Answer",
    "AnswerCitation",
    "AnswerEngine",
]

AnswerMode = Literal["extractive", "llm"]
_MAX_LIMIT = 20
_TABLE_RULE = re.compile(r"^[\s:|-]+$")


@dataclass(frozen=True, slots=True)
class AnswerCitation:
    """A citation returned with an answer, linked to its best passage when available."""

    citation: str
    passage_id: str | None = None
    kind: PassageKind | None = None
    title: str | None = None
    section: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "citation": self.citation,
            "passage_id": self.passage_id,
            "kind": self.kind.value if self.kind else None,
            "title": self.title,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """A cited answer plus the evidence that produced it."""

    question: str
    answer: str
    mode: AnswerMode
    status: Confidence
    citations: tuple[AnswerCitation, ...]
    passages: tuple[SearchHit, ...]
    insufficient_evidence: bool
    unresolved_citations: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    cached: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "answer": self.answer,
            "mode": self.mode,
            "status": self.status.value,
            "insufficient_evidence": self.insufficient_evidence,
            "citations": [item.as_dict() for item in self.citations],
            "unresolved_citations": list(self.unresolved_citations),
            "provider": self.provider,
            "model": self.model,
            "cached": self.cached,
            "passages": [hit.as_dict() for hit in self.passages],
        }


@dataclass(frozen=True, slots=True)
class AnswerEngine:
    """Answer assembly over one retrieval index and one provider."""

    store: FactStore
    index: RetrievalIndex
    provider: Provider = field(default_factory=NullProvider)
    system_prompt: str = QA_SYSTEM_PROMPT
    min_score: float = 0.0
    max_context_chars: int = 12_000
    max_evidence: int = 8
    max_extract_citations: int = 4

    def __post_init__(self) -> None:
        if self.min_score < 0:
            raise ValueError("min_score must be >= 0")
        if self.max_context_chars < 500:
            raise ValueError("max_context_chars must be >= 500")
        if not 1 <= self.max_evidence <= 50:
            raise ValueError("max_evidence must be between 1 and 50")
        if not 1 <= self.max_extract_citations <= 10:
            raise ValueError("max_extract_citations must be between 1 and 10")

    def ask(
        self,
        question: str,
        *,
        mode: AnswerMode = "extractive",
        limit: int | None = None,
        kinds: list[PassageKind | str] | None = None,
        page: str | None = None,
    ) -> Answer:
        """Answer a question with citations or an explicit insufficient-evidence result."""

        if mode not in ("extractive", "llm"):
            raise ValueError("mode must be 'extractive' or 'llm'")
        if mode == "llm" and self.provider.name == "none":
            raise ValueError("LLM mode requires a configured provider")

        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")

        selected_limit = self.max_evidence if limit is None else limit
        if not 1 <= selected_limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")

        hits = self.index.search(
            question,
            limit=selected_limit,
            kinds=kinds,
            page=page,
            min_score=self.min_score,
        )
        if not hits:
            return self._no_evidence(question, mode)

        if mode == "extractive":
            return self._extractive(question, hits)
        return self._llm(question, hits)

    # ------------------------------------------------------------------
    # Extractive answers
    # ------------------------------------------------------------------

    def _extractive(self, question: str, hits: tuple[SearchHit, ...]) -> Answer:
        selected: list[SearchHit] = []
        seen: set[str] = set()
        for hit in hits:
            if hit.passage.citation in seen:
                continue
            seen.add(hit.passage.citation)
            selected.append(hit)
            if len(selected) >= self.max_extract_citations:
                break

        blocks = []
        for hit in selected:
            digest = _digest(
                hit.passage.text,
                hit.passage.title,
                hit.matched_terms,
            )
            blocks.append(f"{hit.passage.title}\n{digest} [{hit.passage.citation}]")
        answer = "According to the indexed evidence:\n\n" + "\n\n".join(blocks)
        return Answer(
            question=question,
            answer=answer,
            mode="extractive",
            status=Confidence.VERIFIED,
            citations=tuple(self._citation(hit.passage.citation) for hit in selected),
            passages=hits,
            insufficient_evidence=False,
        )

    # ------------------------------------------------------------------
    # LLM answers
    # ------------------------------------------------------------------

    def _llm(self, question: str, hits: tuple[SearchHit, ...]) -> Answer:
        context, supplied_hits = self._render_evidence(hits)
        prompt = render_prompt("qa", question=question, context=context)
        completion = complete_cached(
            self.store,
            self.provider,
            self.system_prompt,
            prompt,
            response_format="json",
        )
        payload = _parse_payload(completion.text)
        if payload is None:
            return Answer(
                question=question,
                answer="The model returned a response that was not valid JSON.",
                mode="llm",
                status=Confidence.UNSUPPORTED,
                citations=(),
                passages=hits,
                insufficient_evidence=True,
                provider=completion.provider,
                model=completion.model or self.provider.settings.model,
                cached=completion.cached,
            )

        raw_answer = payload.get("answer")
        if not isinstance(raw_answer, str) or not raw_answer.strip():
            return Answer(
                question=question,
                answer="The model did not return an answer.",
                mode="llm",
                status=Confidence.UNSUPPORTED,
                citations=(),
                passages=hits,
                insufficient_evidence=True,
                provider=completion.provider,
                model=completion.model or self.provider.settings.model,
                cached=completion.cached,
            )

        supplied = _supplied_citations(supplied_hits)
        valid: list[str] = []
        unresolved: list[str] = []
        for raw in _citation_values(payload.get("citations")):
            citation = _normalise_citation(raw)
            if not citation:
                continue
            if citation not in supplied:
                mapped = _map_numeric(citation, supplied_hits)
                if mapped is not None and mapped in supplied:
                    citation = mapped
                else:
                    if citation not in unresolved:
                        unresolved.append(citation)
                    continue
            if citation not in valid:
                valid.append(citation)

        insufficient = payload.get("insufficient_evidence") is True
        if not valid:
            status = Confidence.UNSUPPORTED
            insufficient = True
        elif unresolved:
            status = Confidence.INFERRED
        else:
            status = Confidence.VERIFIED
        if insufficient and status is Confidence.VERIFIED:
            status = Confidence.INFERRED

        return Answer(
            question=question,
            answer=raw_answer.strip(),
            mode="llm",
            status=status,
            citations=tuple(self._citation(citation) for citation in valid),
            passages=hits,
            insufficient_evidence=insufficient,
            unresolved_citations=tuple(unresolved),
            provider=completion.provider or self.provider.name,
            model=completion.model or self.provider.settings.model,
            cached=completion.cached,
        )

    def _render_evidence(
        self, hits: tuple[SearchHit, ...]
    ) -> tuple[str, tuple[SearchHit, ...]]:
        blocks: list[str] = []
        included: list[SearchHit] = []
        used = 0
        for hit in hits:
            passage = hit.passage
            lines = [
                f"[{len(included) + 1}] citation: {passage.citation}",
                f"kind: {passage.kind.value}",
                f"title: {passage.title}",
            ]
            if len(passage.citations) > 1:
                lines.append("additional citations: " + ", ".join(passage.citations[1:]))
            lines.extend(["", passage.text.strip()])
            block = "\n".join(lines)
            if included and used + len(block) + 2 > self.max_context_chars:
                break
            included.append(hit)
            blocks.append(block)
            used += len(block) + 2
            if used >= self.max_context_chars:
                break
        return "\n\n".join(blocks), tuple(included)

    def _citation(self, citation: str) -> AnswerCitation:
        passage = self.index.best_for_citation(citation)
        return AnswerCitation(
            citation=citation,
            passage_id=passage.id if passage else None,
            kind=passage.kind if passage else None,
            title=passage.title if passage else None,
            section=passage.section if passage else None,
        )

    def _no_evidence(self, question: str, mode: AnswerMode) -> Answer:
        return Answer(
            question=question,
            answer=(
                "I could not find evidence for that question in the indexed IR and "
                "documentation. Try naming a file, function, route, table or claim."
            ),
            mode=mode,
            status=Confidence.UNSUPPORTED,
            citations=(),
            passages=(),
            insufficient_evidence=True,
            provider=self.provider.name if mode == "llm" else None,
            model=self.provider.settings.model if mode == "llm" else None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _supplied_citations(hits: tuple[SearchHit, ...]) -> set[str]:
    supplied: set[str] = set()
    for hit in hits:
        supplied.update(hit.passage.citations)
    return supplied


def _citation_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _normalise_citation(value: str) -> str:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text.strip("`").strip()


def _map_numeric(citation: str, hits: tuple[SearchHit, ...]) -> str | None:
    if not citation.isdigit():
        return None
    index = int(citation) - 1
    if 0 <= index < len(hits):
        return hits[index].passage.citation
    return None


def _parse_payload(response: str) -> dict[str, object] | None:
    text = response.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = _strip_fence(text)
    try:
        payload = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def _strip_fence(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _digest(text: str, title: str, terms: tuple[str, ...], *, limit: int = 360) -> str:
    """Select a few citation-bearing lines rather than dumping a whole passage."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title_parts = {part.strip() for part in title.split("/")}
    title_parts.add(title)
    cleaned = [
        line
        for line in lines
        if line not in title_parts
        and not line.lower().startswith("citation:")
        and not line.startswith("|")
        and not _TABLE_RULE.fullmatch(line)
    ]
    if not cleaned:
        cleaned = [
            _table_row(line)
            for line in lines
            if line.startswith("|") and not _TABLE_RULE.fullmatch(line)
        ][:2]
    lowered = [term.lower() for term in terms]
    matched = [
        line for line in cleaned if any(term in line.lower() for term in lowered)
    ]
    chosen = (matched or cleaned)[:3]
    digest = " · ".join(_clip(line, 170) for line in chosen)
    return digest or _excerpt(text, limit)


def _table_row(value: str) -> str:
    cells = (cell.strip() for cell in value.strip().strip("|").split("|"))
    return " | ".join(cell for cell in cells if cell)


def _clip(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _excerpt(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip() + "…"
