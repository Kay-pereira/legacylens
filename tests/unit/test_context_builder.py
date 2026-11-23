"""Context builder: deterministic facts, source excerpts and budget enforcement."""

from __future__ import annotations

from pathlib import Path

from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import FactStore
from legacylens.llm import build_context
from legacylens.parsers import parse_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_web"
FUNCTION = "fn:public/inc/helpers.php#search"


def _store_from(root: Path, db: Path) -> FactStore:
    scan = scan_repository(root, ScanOptions())
    store = FactStore(db)
    for scanned in scan.files:
        store.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
    return store


def test_function_context_includes_source_facts_and_citations(tmp_path: Path) -> None:
    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        context = build_context(store, FUNCTION, source_root=FIXTURE)

    assert context.target.kind == "function"
    assert "Target function: fn:public/inc/helpers.php#search" in context.text
    assert "Source: public/inc/helpers.php:8-" in context.text
    assert "SELECT id, title FROM articles" in context.text
    assert "metrics: loc=" in context.text
    assert FUNCTION in context.citations
    assert "public/inc/helpers.php:10" in context.citations
    assert context.truncated is False
    assert context.estimated_tokens == len(context.text) // 4


def test_context_is_deterministic(tmp_path: Path) -> None:
    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        first = build_context(store, FUNCTION, source_root=FIXTURE)
        second = build_context(store, FUNCTION, source_root=FIXTURE)

    assert first.text == second.text
    assert first.citations == second.citations


def test_project_context_summarises_the_application(tmp_path: Path) -> None:
    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        context = build_context(store, "project", source_root=FIXTURE)

    assert context.target.kind == "project"
    assert "Project inventory" in context.text
    assert "files=18" in context.text
    assert "Entry points" in context.text
    assert "CRUD coverage" in context.text
    assert "table:users" in context.citations


def test_file_context_lists_functions_and_source(tmp_path: Path) -> None:
    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        context = build_context(store, "public/inc/helpers.php", source_root=FIXTURE)

    assert context.target.kind == "file"
    assert "Target file: public/inc/helpers.php" in context.text
    assert "search" in context.text
    assert "Source: public/inc/helpers.php:1-" in context.text


def test_budget_marks_truncation_and_never_overflows(tmp_path: Path) -> None:
    with _store_from(FIXTURE, tmp_path / "facts.db") as store:
        context = build_context(
            store,
            FUNCTION,
            source_root=FIXTURE,
            budget_chars=240,
        )

    assert context.truncated is True
    assert len(context.text) <= 240
