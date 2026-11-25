"""Deterministic retrieval over generated documentation and stored IR facts.

The index has two corpora:

* **Documentation** — the publisher's Markdown pages, split into section passages. By
  default the pages are rebuilt in memory from the IR, so retrieval never depends on stale
  files. A published ``docs/`` directory can be indexed instead.
* **IR facts** — one passage per file, class, function, table, entry point and publishable
  claim. These passages carry the same citations the verifier understands, so a cited
  answer can always be traced back to a stored fact.

Ranking is lexical BM25 with weighted fields. Exact citation, symbol and path matches get a
fixed boost. The implementation deliberately has no embedding dependency: it is deterministic,
inspectable and fast enough to rebuild at local-server startup.
"""

from __future__ import annotations

import html
import math
import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from legacylens.analysis import EntryPointDetail, build_crud, build_entrypoints
from legacylens.ir import (
    ClaimRecord,
    ClassRecord,
    Confidence,
    DataAccessRecord,
    EdgeKind,
    EntryPointRecord,
    FactStore,
    FileRecord,
    FunctionRecord,
    RequestInputRecord,
    SideEffectRecord,
)
from legacylens.publisher import Documentation, MarkdownPage, build_documentation
from legacylens.verification import (
    CitationError,
    CitationKind,
    parse_citation,
    resolve_citation,
)

__all__ = [
    "IndexStats",
    "Passage",
    "PassageKind",
    "RetrievalIndex",
    "SearchHit",
]

_BM25_K1 = 1.5
_BM25_B = 0.75
_EXACT_BOOST = 8.0
_EXACT_PRIMARY_BOOST = 18.0
_MAX_CITATIONS_PER_PASSAGE = 80

_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "each",
        "either",
        "else",
        "for",
        "from",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "might",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "per",
        "please",
        "same",
        "she",
        "should",
        "show",
        "so",
        "some",
        "such",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "use",
        "used",
        "using",
        "via",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
    }
)

_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT_IDENTIFIER = re.compile(r"[_.]+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_PREFIXED_CITATION = re.compile(r"(?<![\w:/.#-])((?:fn|class|table|entry):[^\s`|,;]+)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_NEWLINES = re.compile(r"\n{3,}")


class PassageKind(StrEnum):
    """The kind of evidence a passage holds."""

    DOC = "doc"
    FUNCTION = "function"
    CLASS = "class"
    FILE = "file"
    TABLE = "table"
    ENTRY = "entry"
    CLAIM = "claim"


@dataclass(frozen=True, slots=True)
class Passage:
    """One retrievable, citation-bearing passage."""

    id: str
    kind: PassageKind
    title: str
    text: str
    citation: str
    citations: tuple[str, ...] = ()
    page: str | None = None
    section: str | None = None
    subject: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", PassageKind(self.kind))
        if not self.id.strip():
            raise ValueError("passage id must not be empty")
        if not self.title.strip():
            raise ValueError("passage title must not be empty")
        if not self.citation.strip():
            raise ValueError("passage citation must not be empty")
        if not self.text.strip():
            raise ValueError("passage text must not be empty")

        citations: list[str] = []
        for raw in (self.citation, *self.citations):
            value = raw.strip()
            if value and value not in citations:
                citations.append(value)
        object.__setattr__(self, "citations", tuple(citations[:_MAX_CITATIONS_PER_PASSAGE]))
        object.__setattr__(self, "citation", citations[0])

    @property
    def evidence(self) -> tuple[str, ...]:
        """Every citation a caller may use for this passage."""

        return self.citations

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "citation": self.citation,
            "citations": list(self.citations),
            "page": self.page,
            "section": self.section,
            "subject": self.subject,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A passage plus its BM25 score and the query terms it matched."""

    passage: Passage
    score: float
    matched_terms: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload = self.passage.as_dict()
        payload["score"] = round(self.score, 6)
        payload["matched_terms"] = list(self.matched_terms)
        return payload


@dataclass(frozen=True, slots=True)
class IndexStats:
    """Small, JSON-ready description of an index."""

    passages: int
    by_kind: dict[str, int]
    terms: int
    docs_pages: tuple[str, ...]
    docs_source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "passages": self.passages,
            "by_kind": dict(sorted(self.by_kind.items())),
            "terms": self.terms,
            "docs_pages": list(self.docs_pages),
            "docs_source": self.docs_source,
        }


@dataclass(frozen=True, slots=True)
class RetrievalIndex:
    """An immutable BM25 index over documentation and IR passages."""

    passages: tuple[Passage, ...]
    docs_pages: tuple[str, ...]
    docs_source: str
    _terms: dict[str, tuple[tuple[int, float], ...]] = field(repr=False)
    _lengths: tuple[float, ...] = field(repr=False)
    _avg_length: float = field(repr=False)
    _doc_freq: dict[str, int] = field(repr=False)
    _exact: dict[str, tuple[int, ...]] = field(repr=False)
    _by_id: dict[str, Passage] = field(repr=False)
    _by_citation: dict[str, tuple[str, ...]] = field(repr=False)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        store: FactStore,
        *,
        docs: Documentation | None = None,
        docs_dir: str | Path | None = None,
        include_docs: bool = True,
        include_ir: bool = True,
        project: str = "LegacyLens",
        max_section_chars: int = 1_200,
        max_rows: int = 200,
    ) -> RetrievalIndex:
        """Build an index from the store, generated docs or a Markdown directory."""

        if docs is not None and docs_dir is not None:
            raise ValueError("pass docs or docs_dir, not both")
        if not include_docs and not include_ir:
            raise ValueError("the index needs at least one corpus")
        if max_section_chars < 200:
            raise ValueError("max_section_chars must be >= 200")

        pages: tuple[MarkdownPage, ...] = ()
        docs_source = "none"
        if include_docs:
            if docs_dir is not None:
                pages = _load_docs_dir(docs_dir)
                docs_source = "directory"
            elif docs is not None:
                pages = tuple(docs.pages)
                docs_source = "provided"
            else:
                documentation = build_documentation(store, project=project, max_rows=max_rows)
                pages = tuple(documentation.pages)
                docs_source = "generated"

        passages: list[Passage] = []
        if include_docs:
            passages.extend(_documentation_passages(pages, store, max_section_chars))
        if include_ir:
            passages.extend(_ir_passages(store))
        if not passages:
            raise ValueError("the retrieval index has no passages")

        term_postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        lengths: list[float] = []
        for position, passage in enumerate(passages):
            weighted = _weighted_terms(passage)
            lengths.append(sum(weighted.values()))
            for term, weight in weighted.items():
                term_postings[term].append((position, weight))

        doc_freq = {term: len(posting) for term, posting in term_postings.items()}
        average_length = sum(lengths) / len(lengths) if lengths else 0.0

        exact: dict[str, list[int]] = {}
        for position, passage in enumerate(passages):
            for key in _exact_keys(passage):
                normalized = _normalise_query(key)
                if normalized:
                    exact.setdefault(normalized, []).append(position)

        by_id: dict[str, Passage] = {}
        for passage in passages:
            if passage.id in by_id:
                raise ValueError(f"duplicate passage id: {passage.id!r}")
            by_id[passage.id] = passage

        by_citation: dict[str, list[str]] = {}
        for passage in passages:
            by_citation.setdefault(passage.citation, []).append(passage.id)
        for passage in passages:
            for citation in passage.citations:
                ids = by_citation.setdefault(citation, [])
                if passage.id not in ids:
                    ids.append(passage.id)

        return cls(
            passages=tuple(passages),
            docs_pages=tuple(page.filename for page in pages),
            docs_source=docs_source,
            _terms={term: tuple(posting) for term, posting in term_postings.items()},
            _lengths=tuple(lengths),
            _avg_length=average_length,
            _doc_freq=doc_freq,
            _exact={key: tuple(value) for key, value in exact.items()},
            _by_id=by_id,
            _by_citation={key: tuple(value) for key, value in by_citation.items()},
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def stats(self) -> IndexStats:
        counts: dict[str, int] = {}
        for passage in self.passages:
            counts[passage.kind.value] = counts.get(passage.kind.value, 0) + 1
        return IndexStats(
            passages=len(self.passages),
            by_kind=counts,
            terms=len(self._terms),
            docs_pages=self.docs_pages,
            docs_source=self.docs_source,
        )

    def get(self, passage_id: str) -> Passage | None:
        """Return a passage by its internal id."""

        return self._by_id.get(passage_id)

    def find_by_citation(self, citation: str) -> tuple[Passage, ...]:
        """Return every passage that carries ``citation`` as primary or embedded evidence."""

        return tuple(self._by_id[passage_id] for passage_id in self._by_citation.get(citation, ()))

    def best_for_citation(self, citation: str) -> Passage | None:
        """Return the most directly relevant passage for a citation."""

        ids = self._by_citation.get(citation)
        if not ids:
            return None
        return self._by_id[ids[0]]

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        kinds: Collection[PassageKind | str] | None = None,
        page: str | None = None,
        min_score: float = 0.0,
    ) -> tuple[SearchHit, ...]:
        """Rank passages for a question, with optional kind and page filters."""

        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if min_score < 0:
            raise ValueError("min_score must be >= 0")

        terms = tuple(dict.fromkeys(_tokenize(query)))
        if not terms:
            return ()

        kind_filter = _normalise_kinds(kinds)
        scores: dict[int, float] = {}
        matched: dict[int, set[str]] = defaultdict(set)
        total = len(self.passages)

        for term in terms:
            posting = self._terms.get(term)
            if not posting:
                continue
            document_frequency = self._doc_freq[term]
            inverse_frequency = math.log(
                1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for position, term_frequency in posting:
                passage = self.passages[position]
                if not _matches_filters(passage, kind_filter, page):
                    continue
                length = self._lengths[position]
                denominator = term_frequency + _BM25_K1 * (
                    1.0 - _BM25_B + _BM25_B * (length / self._avg_length)
                )
                contribution = inverse_frequency * (
                    term_frequency * (_BM25_K1 + 1.0)
                ) / denominator
                scores[position] = scores.get(position, 0.0) + contribution
                matched[position].add(term)

        normalized_query = _normalise_query(query)
        boosted: set[int] = set()
        for key, positions in self._exact.items():
            if key == normalized_query:
                boosted.update(positions)
            elif len(key) >= 8 and _is_strong_key(key) and key in normalized_query:
                boosted.update(positions)
        for position in boosted:
            passage = self.passages[position]
            if not _matches_filters(passage, kind_filter, page):
                continue
            primary = normalized_query in {
                _normalise_query(passage.citation),
                _normalise_query(passage.id),
                _normalise_query(passage.title),
            }
            scores[position] = scores.get(position, 0.0) + (
                _EXACT_PRIMARY_BOOST if primary else _EXACT_BOOST
            )
            matched[position].update(terms)

        ranked = sorted(
            ((position, score) for position, score in scores.items() if score >= min_score),
            key=lambda item: (-item[1], self.passages[item[0]].id),
        )
        return tuple(
            SearchHit(
                passage=self.passages[position],
                score=score,
                matched_terms=tuple(sorted(matched[position])),
            )
            for position, score in ranked[:limit]
        )


# ---------------------------------------------------------------------------
# Tokenisation and scoring
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> tuple[str, ...]:
    """Split prose, identifiers, paths and citations into stable lowercase terms."""

    tokens: list[str] = []
    for raw in _WORD.findall(text):
        candidates = {raw.lower()}
        for piece in _SPLIT_IDENTIFIER.split(raw):
            for part in _CAMEL.split(piece):
                if len(part) >= 2:
                    candidates.add(part.lower())
        for token in candidates:
            if len(token) >= 2 and token not in _STOPWORDS:
                tokens.append(token)
    return tuple(tokens)


def _weighted_terms(passage: Passage) -> dict[str, float]:
    terms: dict[str, float] = {}

    def add(text: str | None, weight: float) -> None:
        if not text:
            return
        for token in _tokenize(text):
            terms[token] = terms.get(token, 0.0) + weight

    add(passage.title, 3.0)
    add(passage.section, 2.0)
    add(" ".join(passage.citations), 2.0)
    add(passage.text, 1.0)
    return terms


def _exact_keys(passage: Passage) -> tuple[str, ...]:
    keys = [passage.id, passage.citation, passage.title]
    if passage.section:
        keys.append(passage.section)
    if passage.subject:
        keys.append(passage.subject)
    keys.extend(passage.citations)
    return tuple(dict.fromkeys(keys))


def _normalise_query(value: str) -> str:
    return " ".join(value.strip().strip("[]`").lower().split())


def _is_strong_key(value: str) -> bool:
    return (
        value.startswith(("fn:", "class:", "table:", "entry:", "docs/"))
        or "::" in value
        or "/" in value
    )


def _normalise_kinds(
    kinds: Collection[PassageKind | str] | None,
) -> frozenset[PassageKind]:
    if not kinds:
        return frozenset()
    resolved: set[PassageKind] = set()
    for kind in kinds:
        try:
            resolved.add(PassageKind(kind))
        except ValueError as exc:
            raise ValueError(f"unknown passage kind: {kind!r}") from exc
    return frozenset(resolved)


def _matches_filters(
    passage: Passage,
    kinds: frozenset[PassageKind],
    page: str | None,
) -> bool:
    if kinds and passage.kind not in kinds:
        return False
    return page is None or passage.page == page


# ---------------------------------------------------------------------------
# Documentation corpus
# ---------------------------------------------------------------------------


def _documentation_passages(
    pages: Sequence[MarkdownPage],
    store: FactStore,
    max_section_chars: int,
) -> list[Passage]:
    passages: list[Passage] = []
    for page in pages:
        for heading, slug, body in _sections(page.markdown):
            plain = _plain_markdown(body)
            if not plain:
                continue
            citation = f"docs/{page.filename}"
            if slug:
                citation += f"#{slug}"
            parts = _split_text(plain, max_section_chars)
            for index, part in enumerate(parts):
                passage_id = f"docs:{page.filename}"
                if slug:
                    passage_id += f"#{slug}"
                if len(parts) > 1:
                    passage_id += f"~{index + 1}"
                if heading and heading.lower() == page.title.lower():
                    title = page.title
                    text = f"{page.title}\n\n{part}"
                elif heading:
                    title = f"{page.title} / {heading}"
                    text = f"{page.title}\n{heading}\n\n{part}"
                else:
                    title = page.title
                    text = f"{page.title}\n\n{part}"
                passages.append(
                    Passage(
                        id=passage_id,
                        kind=PassageKind.DOC,
                        title=title,
                        text=text,
                        citation=citation,
                        citations=_extract_citations(part, store),
                        page=page.filename,
                        section=heading,
                    )
                )
    return passages


def _sections(markdown: str) -> list[tuple[str | None, str | None, str]]:
    """Split a page into preamble and ``##`` sections with unique slugs."""

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[tuple[str | None, str | None, str]] = []
    used: set[str] = set()
    heading: str | None = None
    slug: str | None = None
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append((heading, slug, text))

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            continue
        if line.startswith("## ") and not line.startswith("### "):
            flush()
            heading = line[3:].strip()
            slug = _unique_slug(_slugify(heading), used)
            body = []
        else:
            body.append(line)
    flush()
    return sections


def _slugify(value: str) -> str:
    slug = _NON_SLUG.sub("-", value.lower()).strip("-")
    return slug or "section"


def _unique_slug(slug: str, used: set[str]) -> str:
    candidate = slug
    suffix = 2
    while candidate in used:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _plain_markdown(text: str) -> str:
    text = _HTML_TAG.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _NEWLINES.sub("\n\n", text).strip()


def _split_text(text: str, limit: int) -> tuple[str, ...]:
    if len(text) <= limit:
        return (text,)

    parts: list[str] = []
    current: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for piece in _split_block(block, limit):
            candidate_length = sum(len(item) for item in current) + 2 * len(current) + len(
                piece
            )
            if current and candidate_length > limit:
                parts.append("\n\n".join(current))
                current = []
            current.append(piece)
    if current:
        parts.append("\n\n".join(current))
    return tuple(parts) or (text,)


def _split_block(block: str, limit: int) -> tuple[str, ...]:
    if len(block) <= limit:
        return (block,)

    lines = block.splitlines()
    header: list[str] = []
    body = lines
    if (
        len(lines) >= 2
        and lines[0].lstrip().startswith("|")
        and _TABLE_SEPARATOR.match(lines[1])
    ):
        header = lines[:2]
        body = lines[2:]

    parts: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in body:
        extra = len(line) + 1
        if current and current_length + extra > limit:
            parts.append("\n".join(header + current))
            current = []
            current_length = 0
        current.append(line)
        current_length += extra
    if current:
        parts.append("\n".join(header + current))
    return tuple(parts) or (block,)


def _extract_citations(text: str, store: FactStore) -> tuple[str, ...]:
    """Collect explicit IR citations from code spans and prefixed references."""

    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in _CODE_SPAN.finditer(text))
    candidates.extend(match.group(1).strip() for match in _PREFIXED_CITATION.finditer(text))

    result: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip("` ")
        if not candidate or candidate in result:
            continue
        try:
            parsed = parse_citation(candidate)
        except CitationError:
            continue
        if parsed.kind in (CitationKind.FILE, CitationKind.FILE_LINE):
            if store.get_file(parsed.path or "") is None:
                continue
        elif not resolve_citation(store, candidate).resolved:
            continue
        result.append(candidate)
        if len(result) >= _MAX_CITATIONS_PER_PASSAGE:
            break
    return tuple(result)


def _load_docs_dir(value: str | Path) -> tuple[MarkdownPage, ...]:
    root = Path(value)
    if not root.is_dir():
        raise ValueError(f"docs directory does not exist: {root}")
    files = sorted(root.glob("*.md"), key=lambda item: item.name.lower())
    if not files:
        raise ValueError(f"no Markdown pages found in {root}")

    pages: list[MarkdownPage] = []
    for path in files:
        markdown = path.read_text(encoding="utf-8", errors="replace")
        title = _first_heading(markdown) or path.stem.replace("-", " ").replace("_", " ").title()
        pages.append(
            MarkdownPage(
                key=path.stem,
                filename=path.name,
                title=title,
                nav_title=title,
                description="",
                markdown=markdown,
            )
        )
    return tuple(pages)


def _first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


# ---------------------------------------------------------------------------
# IR corpus
# ---------------------------------------------------------------------------


def _ir_passages(store: FactStore) -> list[Passage]:
    files = store.list_files()
    classes = store.list_classes()
    functions = store.list_functions()
    data_access = store.list_data_access()
    entries = store.list_entry_points()
    inputs = store.list_request_inputs()
    effects = store.list_side_effects()
    claims = store.list_claims()

    crud = build_crud(store)
    entry_report = build_entrypoints(store)
    crud_by_table = {table.table_name: table for table in crud.tables}
    detail_by_citation = {detail.entry.citation: detail for detail in entry_report.entries}

    classes_by_file = _group(classes, lambda item: item.file_path)
    functions_by_file = _group(functions, lambda item: item.file_path)
    functions_by_class = _group(functions, lambda item: item.class_id or "")
    data_by_function = _group(data_access, lambda item: item.function_id or "")
    data_by_file = _group(data_access, lambda item: item.file_path)
    data_by_table = _group(
        (item for item in data_access if item.table_name),
        lambda item: item.table_name or "",
    )
    entries_by_function = _group(entries, lambda item: item.function_id or "")
    entries_by_file = _group(entries, lambda item: item.file_path)
    inputs_by_file = _group(inputs, lambda item: item.file_path)
    effects_by_function = _group(effects, lambda item: item.function_id or "")
    effects_by_file = _group(effects, lambda item: item.file_path)
    claims_by_subject = _group(claims, lambda item: item.subject)

    passages: list[Passage] = []
    for file in files:
        passages.append(
            _file_passage(
                file,
                classes_by_file.get(file.path, ()),
                functions_by_file.get(file.path, ()),
                entries_by_file.get(file.path, ()),
                data_by_file.get(file.path, ()),
                effects_by_file.get(file.path, ()),
                claims_by_subject.get(file.path, ()),
                store,
            )
        )
    for class_record in classes:
        passages.append(
            _class_passage(
                class_record,
                functions_by_class.get(class_record.id, ()),
                claims_by_subject.get(class_record.id, ()),
                store,
            )
        )
    for function in functions:
        passages.append(
            _function_passage(
                function,
                store,
                data_by_function.get(function.id, ()),
                entries_by_function.get(function.id, ()),
                effects_by_function.get(function.id, ()),
                claims_by_subject.get(function.id, ()),
            )
        )
    for table_name in sorted(data_by_table):
        passages.append(
            _table_passage(
                table_name,
                data_by_table[table_name],
                crud_by_table.get(table_name),
                claims_by_subject.get(f"table:{table_name}", ()),
                store,
            )
        )
    for entry in entries:
        passages.append(
            _entry_passage(
                entry,
                detail_by_citation.get(entry.citation),
                inputs_by_file.get(entry.file_path, ()),
                claims_by_subject.get(entry.citation, ()),
                store,
            )
        )
    for index, claim in enumerate(claims):
        passage = _claim_passage(claim, store, index)
        if passage is not None:
            passages.append(passage)
    return passages


def _group(items: Iterable[object], key: object) -> dict[str, tuple[object, ...]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for item in items:
        grouped[key(item)].append(item)  # type: ignore[operator]
    return {name: tuple(values) for name, values in grouped.items()}


def _file_passage(
    file: FileRecord,
    classes: Sequence[ClassRecord],
    functions: Sequence[FunctionRecord],
    entries: Sequence[EntryPointRecord],
    data: Sequence[DataAccessRecord],
    effects: Sequence[SideEffectRecord],
    claims: Sequence[ClaimRecord],
    store: FactStore,
) -> Passage:
    lines = [
        f"File: {file.path}",
        f"Language: {file.language.value}",
        f"Lines: {file.loc}",
        f"Size: {file.size_bytes} bytes",
    ]
    if file.package:
        lines.append(f"Package: {file.package}")
    if file.module:
        lines.append(f"Module: {file.module}")
    if file.is_generated:
        lines.append("Generated: yes")
    if file.is_vendored:
        lines.append("Vendored: yes")
    if file.parse_error:
        lines.append(f"Parse error: {_one_line(file.parse_error)}")
    if classes:
        lines.append("Classes: " + ", ".join(item.qualified_name for item in classes))
    if functions:
        lines.append("Functions: " + ", ".join(item.qualified_name for item in functions))
    if entries:
        lines.append("Entry points: " + _entry_labels(entries))
    tables = _tables(data)
    if tables:
        lines.append("Tables: " + ", ".join(tables))
    if effects:
        lines.append("Side effects:")
        lines.extend(_effect_lines(effects))
    claim_lines = _claim_lines(claims, store)
    if claim_lines:
        lines.append("Claims:")
        lines.extend(claim_lines)

    citations = [file.path]
    citations.extend(item.id for item in classes)
    citations.extend(item.id for item in functions)
    citations.extend(item.citation for item in entries)
    citations.extend(f"table:{table}" for table in tables)
    citations.extend(f"{item.file_path}:{item.line}" for item in effects if item.line > 0)
    return Passage(
        id=file.path,
        kind=PassageKind.FILE,
        title=file.path,
        text="\n".join(lines),
        citation=file.path,
        citations=tuple(citations),
        subject=file.path,
    )


def _class_passage(
    record: ClassRecord,
    methods: Sequence[FunctionRecord],
    claims: Sequence[ClaimRecord],
    store: FactStore,
) -> Passage:
    lines = [
        f"Class: {record.qualified_name}",
        f"Citation: {record.id}",
        f"Kind: {record.kind.value}",
        f"Span: {record.file_path}:{record.span.start_line}-{record.span.end_line}",
        f"File: {record.file_path}",
    ]
    if record.extends:
        lines.append(f"Extends: {record.extends}")
    if record.implements:
        lines.append("Implements: " + ", ".join(record.implements))
    if record.modifiers:
        lines.append("Modifiers: " + ", ".join(record.modifiers))
    if methods:
        lines.append("Methods: " + ", ".join(item.qualified_name for item in methods))
    claim_lines = _claim_lines(claims, store)
    if claim_lines:
        lines.append("Claims:")
        lines.extend(claim_lines)

    citations = [
        record.id,
        record.file_path,
        f"{record.file_path}:{record.span.start_line}",
    ]
    citations.extend(item.id for item in methods)
    return Passage(
        id=record.id,
        kind=PassageKind.CLASS,
        title=record.qualified_name,
        text="\n".join(lines),
        citation=record.id,
        citations=tuple(citations),
        subject=record.id,
    )


def _function_passage(
    record: FunctionRecord,
    store: FactStore,
    data: Sequence[DataAccessRecord],
    entries: Sequence[EntryPointRecord],
    effects: Sequence[SideEffectRecord],
    claims: Sequence[ClaimRecord],
) -> Passage:
    lines = [
        f"Function: {record.qualified_name}",
        f"Citation: {record.id}",
        f"Signature: {record.signature}",
        f"Span: {record.file_path}:{record.span.start_line}-{record.span.end_line}",
        f"Complexity: {record.cyclomatic_complexity}",
        f"Nesting: {record.max_nesting}",
    ]
    if record.class_id:
        lines.append(f"Class: {record.class_id}")
    if record.return_type:
        lines.append(f"Returns: {record.return_type}")
    if record.visibility:
        lines.append(f"Visibility: {record.visibility.value}")
    if record.is_static:
        lines.append("Static: yes")
    if record.is_abstract:
        lines.append("Abstract: yes")
    if record.parameters:
        parameters = []
        for parameter in record.parameters:
            text = parameter.name
            if parameter.type:
                text += f": {parameter.type}"
            parameters.append(text)
        lines.append("Parameters: " + ", ".join(parameters))

    calls = store.list_edges(kind=EdgeKind.CALL, src=record.id)
    calls += store.list_edges(kind=EdgeKind.INSTANTIATE, src=record.id)
    if calls:
        rendered = []
        for edge in calls:
            suffix = "" if edge.resolved else " (unresolved)"
            rendered.append(f"{edge.dst}{suffix}")
        lines.append("Calls: " + ", ".join(dict.fromkeys(rendered)))

    if data:
        lines.append("Data access:")
        lines.extend(_data_lines(data))
    if effects:
        lines.append("Side effects:")
        lines.extend(_effect_lines(effects))
    if entries:
        lines.append("Entry points:")
        lines.extend(f"- {item.kind.value} {item.path}" for item in entries)
    claim_lines = _claim_lines(claims, store)
    if claim_lines:
        lines.append("Claims:")
        lines.extend(claim_lines)

    citations = [
        record.id,
        f"{record.file_path}:{record.span.start_line}",
        record.file_path,
    ]
    if record.class_id:
        citations.append(record.class_id)
    citations.extend(f"table:{table}" for table in _tables(data))
    citations.extend(item.citation for item in entries)
    citations.extend(f"{item.file_path}:{item.line}" for item in effects if item.line > 0)
    return Passage(
        id=record.id,
        kind=PassageKind.FUNCTION,
        title=record.qualified_name,
        text="\n".join(lines),
        citation=record.id,
        citations=tuple(citations),
        subject=record.id,
    )


def _table_passage(
    table_name: str,
    records: Sequence[DataAccessRecord],
    crud: object | None,
    claims: Sequence[ClaimRecord],
    store: FactStore,
) -> Passage:
    lines = [
        f"Table: {table_name}",
        f"Citation: table:{table_name}",
    ]
    if crud is not None:
        crud_value = getattr(crud, "crud", None)
        missing = getattr(crud, "missing", ())
        functions = getattr(crud, "functions", ())
        files = getattr(crud, "files", ())
        lines.append(f"CRUD: {crud_value or 'unknown'}")
        lines.append(f"Missing operations: {', '.join(missing) if missing else 'none'}")
        if functions:
            lines.append("Functions: " + ", ".join(functions))
        if files:
            lines.append("Files: " + ", ".join(files))
    lines.append("Statements:")
    for record in records:
        lines.append(
            f"- {record.operation.value.upper()} at {record.file_path}:{record.line} "
            f"[{record.confidence.value}]: {_one_line(record.sql, 180)}"
        )
    claim_lines = _claim_lines(claims, store)
    if claim_lines:
        lines.append("Claims:")
        lines.extend(claim_lines)

    citations = [f"table:{table_name}"]
    citations.extend(f"{record.file_path}:{record.line}" for record in records)
    citations.extend(record.file_path for record in records)
    return Passage(
        id=f"table:{table_name}",
        kind=PassageKind.TABLE,
        title=table_name,
        text="\n".join(lines),
        citation=f"table:{table_name}",
        citations=tuple(citations),
        subject=f"table:{table_name}",
    )


def _entry_passage(
    record: EntryPointRecord,
    detail: EntryPointDetail | None,
    inputs: Sequence[RequestInputRecord],
    claims: Sequence[ClaimRecord],
    store: FactStore,
) -> Passage:
    label = f"{record.http_method.value} {record.path}" if record.http_method else record.path
    lines = [
        f"Entry point: {label}",
        f"Citation: {record.citation}",
        f"Kind: {record.kind.value}",
        f"File: {record.file_path}:{record.line}",
    ]
    if record.framework:
        lines.append(f"Framework: {record.framework}")
    if record.requires_auth is not None:
        lines.append(f"Auth required: {'yes' if record.requires_auth else 'no'}")
    if record.auth_hint:
        lines.append(f"Auth hint: {record.auth_hint}")

    handlers = detail.handlers if detail is not None else ()
    if handlers:
        lines.append("Handlers: " + ", ".join(handlers))
    selected_inputs = detail.inputs if detail is not None else inputs
    if selected_inputs:
        lines.append("Request inputs:")
        lines.extend(
            f"- {item.kind.value} {item.name} at {item.file_path}:{item.line}"
            for item in selected_inputs
        )
    claim_lines = _claim_lines(claims, store)
    if claim_lines:
        lines.append("Claims:")
        lines.extend(claim_lines)

    citations = [record.citation, f"{record.file_path}:{record.line}", record.file_path]
    citations.extend(handler for handler in handlers if handler.startswith(("fn:", "class:")))
    citations.extend(f"{item.file_path}:{item.line}" for item in selected_inputs)
    return Passage(
        id=record.citation,
        kind=PassageKind.ENTRY,
        title=label,
        text="\n".join(lines),
        citation=record.citation,
        citations=tuple(citations),
        subject=record.citation,
    )


def _claim_passage(
    claim: ClaimRecord,
    store: FactStore,
    index: int,
) -> Passage | None:
    if claim.status is Confidence.UNSUPPORTED:
        return None

    subject = claim.subject.strip()
    evidence = _claim_citations(claim, store)
    citation: str | None = None
    if subject and subject not in {"project", "*"} and _resolves(store, subject):
        citation = subject
    if citation is None and evidence:
        citation = evidence[0]
    if citation is None:
        return None

    lines = [
        f"Claim: {claim.text}",
        f"Kind: {claim.kind.value}",
        f"Status: {claim.status.value}",
        f"Subject: {claim.subject}",
    ]
    if evidence:
        lines.append("Evidence: " + ", ".join(evidence))
    title = claim.subject if subject and subject not in {"project", "*"} else claim.text
    return Passage(
        id=f"claim:{claim.id if claim.id is not None else index}",
        kind=PassageKind.CLAIM,
        title=_one_line(title, 120),
        text="\n".join(lines),
        citation=citation,
        citations=evidence,
        subject=claim.subject,
    )


def _claim_citations(claim: ClaimRecord, store: FactStore) -> tuple[str, ...]:
    result: list[str] = []
    for value in claim.citations:
        if value and value not in result and _resolves(store, value):
            result.append(value)
    return tuple(result)


def _claim_lines(claims: Sequence[ClaimRecord], store: FactStore) -> list[str]:
    lines: list[str] = []
    for claim in claims:
        if claim.status is Confidence.UNSUPPORTED:
            continue
        evidence = ", ".join(_claim_citations(claim, store)) or "no resolvable citation"
        lines.append(f"- [{claim.status.value}] {_one_line(claim.text, 300)} ({evidence})")
    return lines


def _resolves(store: FactStore, citation: str) -> bool:
    return resolve_citation(store, citation).resolved


def _tables(records: Sequence[DataAccessRecord]) -> tuple[str, ...]:
    return tuple(sorted({record.table_name for record in records if record.table_name}))


def _data_lines(records: Sequence[DataAccessRecord]) -> list[str]:
    return [
        f"- {record.operation.value.upper()} {record.table_name or 'unknown table'} "
        f"at {record.file_path}:{record.line} [{record.confidence.value}]"
        for record in records
    ]


def _effect_lines(records: Sequence[SideEffectRecord]) -> list[str]:
    return [
        f"- {record.kind.value}: {_one_line(record.detail, 160)} at "
        f"{record.file_path}:{record.line} [{record.confidence.value}]"
        for record in records
    ]


def _entry_labels(records: Sequence[EntryPointRecord]) -> str:
    labels = []
    for record in records:
        method = f"{record.http_method.value} " if record.http_method else ""
        labels.append(f"{method}{record.path} ({record.kind.value})")
    return ", ".join(labels)


def _one_line(value: object, limit: int = 200) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
