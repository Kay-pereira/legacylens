"""Q&A retrieval: corpus construction, BM25 ranking, filters and citations."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacylenscli.ingestion import ScanOptions, scan_repository
from legacylenscli.ir import (
    EntryKind,
    EntryPointRecord,
    FactStore,
    FileFacts,
    FileRecord,
    Language,
)
from legacylenscli.parsers import parse_source
from legacylenscli.qa import PassageKind, RetrievalIndex
from legacylenscli.verification import resolve_citation

USER = "src/User.php"
FUNCTION_KEY = f"fn:{USER}#User::find"
LEGACY_WEB = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_web"


@pytest.fixture
def index(qa_store: FactStore) -> RetrievalIndex:
    return RetrievalIndex.build(qa_store)


def test_index_contains_both_corpora_and_every_page(index: RetrievalIndex) -> None:
    stats = index.stats()

    assert stats.passages > len(index.docs_pages)
    assert stats.docs_source == "generated"
    assert stats.docs_pages[0] == "index.md"
    assert set(stats.docs_pages) == {
        "index.md",
        "claims.md",
        "architecture.md",
        "entry-points.md",
        "database.md",
        "side-effects.md",
        "hotspots.md",
        "files.md",
    }
    assert {"doc", "function", "class", "file", "table", "entry", "claim"} <= set(
        stats.by_kind
    )


def test_ir_passage_citations_resolve_against_the_store(
    index: RetrievalIndex, qa_store: FactStore
) -> None:
    ir_kinds = {
        PassageKind.FUNCTION,
        PassageKind.CLASS,
        PassageKind.FILE,
        PassageKind.TABLE,
        PassageKind.ENTRY,
        PassageKind.CLAIM,
    }
    for passage in index.passages:
        if passage.kind not in ir_kinds:
            continue
        resolution = resolve_citation(qa_store, passage.citation)
        assert resolution.resolved, (passage.id, resolution.reason)


def test_search_ranks_an_exact_function_citation_first(index: RetrievalIndex) -> None:
    hits = index.search(FUNCTION_KEY)

    assert hits
    assert hits[0].passage.citation == FUNCTION_KEY
    assert hits[0].score > 0


def test_search_matches_identifiers_and_camel_case(index: RetrievalIndex) -> None:
    hits = index.search("user find id")

    assert any(hit.passage.citation == FUNCTION_KEY for hit in hits[:5])


def test_table_passage_reports_operations_and_crud(index: RetrievalIndex) -> None:
    table = index.best_for_citation("table:users")

    assert table is not None
    assert table.kind is PassageKind.TABLE
    assert "CRUD: RCU" in table.text or "CRUD:" in table.text
    assert "SELECT" in table.text
    assert "INSERT" in table.text


def test_kind_filters_keep_only_the_requested_corpus(index: RetrievalIndex) -> None:
    hits = index.search("users", limit=50, kinds=["table"])

    assert hits
    assert {hit.passage.kind for hit in hits} == {PassageKind.TABLE}


def test_page_filter_keeps_only_generated_documentation(index: RetrievalIndex) -> None:
    hits = index.search("table", limit=50, page="database.md")

    assert hits
    assert {hit.passage.page for hit in hits} == {"database.md"}
    assert {hit.passage.kind for hit in hits} == {PassageKind.DOC}


def test_unsupported_claims_are_excluded_and_supported_claims_indexed(
    index: RetrievalIndex,
) -> None:
    claims = [passage for passage in index.passages if passage.kind is PassageKind.CLAIM]

    assert claims
    assert all("unsupported claim" not in passage.text.lower() for passage in claims)


def test_unknown_kind_is_rejected(index: RetrievalIndex) -> None:
    with pytest.raises(ValueError, match="unknown passage kind"):
        index.search("users", kinds=["nonsense"])


def test_empty_and_stopword_queries_return_no_hits(index: RetrievalIndex) -> None:
    assert index.search("   ") == ()
    assert index.search("the and of") == ()


def test_docs_directory_becomes_section_passages(qa_store: FactStore, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Deployment guide\n\n## Overview\n\nDeploy with the release script.\n\n"
        "## Overview\n\nRepeat headings get a unique slug.\n",
        encoding="utf-8",
    )
    (docs / "ops.md").write_text("# Operations\n\n## Runbook\n\nRestart the worker.\n", encoding="utf-8")

    index = RetrievalIndex.build(qa_store, docs_dir=docs)

    assert index.docs_source == "directory"
    assert index.docs_pages == ("guide.md", "ops.md")
    citations = {
        passage.citation for passage in index.passages if passage.kind is PassageKind.DOC
    }
    assert "docs/guide.md#overview" in citations
    assert "docs/guide.md#overview-2" in citations
    assert "docs/ops.md#runbook" in citations
    assert index.search("release script", kinds=["doc"])[0].passage.page == "guide.md"


def test_docs_and_docs_dir_are_mutually_exclusive(qa_store: FactStore, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "page.md").write_text("# Page\n\n## Body\n\nText.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="docs or docs_dir"):
        RetrievalIndex.build(qa_store, docs_dir=docs, docs=object())  # type: ignore[arg-type]


def test_empty_docs_directory_is_rejected(qa_store: FactStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        RetrievalIndex.build(qa_store, docs_dir=tmp_path / "missing")


def test_ir_only_and_docs_only_indexes_are_supported(qa_store: FactStore) -> None:
    ir_only = RetrievalIndex.build(qa_store, include_docs=False)
    docs_only = RetrievalIndex.build(qa_store, include_ir=False)

    assert ir_only.stats().docs_pages == ()
    assert {passage.kind for passage in ir_only.passages} == {
        PassageKind.FUNCTION,
        PassageKind.CLASS,
        PassageKind.FILE,
        PassageKind.TABLE,
        PassageKind.ENTRY,
        PassageKind.CLAIM,
    }
    assert {passage.kind for passage in docs_only.passages} == {PassageKind.DOC}


def test_no_corpus_is_rejected(qa_store: FactStore) -> None:
    with pytest.raises(ValueError, match="at least one corpus"):
        RetrievalIndex.build(qa_store, include_docs=False, include_ir=False)


def test_duplicate_entry_citations_get_distinct_passage_ids(tmp_path: Path) -> None:
    scan = scan_repository(LEGACY_WEB, ScanOptions())
    with FactStore(tmp_path / "facts.db") as store:
        for scanned in scan.files:
            store.replace_file(
                parse_source(scanned.absolute_path.read_bytes(), scanned.record)
            )
        index = RetrievalIndex.build(store)

    entries = [item for item in index.passages if item.kind is PassageKind.ENTRY]
    secure = [
        item for item in entries if item.citation == "entry:filter:-:/secure/*"
    ]

    assert len(entries) == 10
    assert len({item.id for item in entries}) == len(entries)
    assert len(secure) == 2
    assert len({item.id for item in secure}) == 2


def test_same_line_duplicate_entries_get_distinct_passage_ids(tmp_path: Path) -> None:
    entry = EntryPointRecord(
        file_path="src/main/webapp/WEB-INF/web.xml",
        kind=EntryKind.SERVLET,
        path="/duplicate/*",
        line=7,
        handler_ref="com.example.DuplicateServlet",
    )
    facts = FileFacts(
        file=FileRecord(
            path="src/main/webapp/WEB-INF/web.xml",
            language=Language.XML,
            content_hash="sha256:duplicate-entries",
            size_bytes=100,
            loc=10,
        ),
        entry_points=(entry, entry),
    )
    with FactStore(tmp_path / "duplicate.db") as store:
        store.replace_file(facts)
        index = RetrievalIndex.build(store, include_docs=False)

    entries = [item for item in index.passages if item.kind is PassageKind.ENTRY]

    assert len(entries) == 2
    assert len({item.id for item in entries}) == 2
