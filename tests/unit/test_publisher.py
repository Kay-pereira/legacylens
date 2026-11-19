"""Publisher: page content, diagram syntax, determinism and site writing."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from legacylens.ir import (
    ClassRecord,
    DataAccessRecord,
    EdgeKind,
    EdgeRecord,
    EntryKind,
    EntryPointRecord,
    FactStore,
    FileFacts,
    FileRecord,
    FunctionRecord,
    HttpMethod,
    Language,
    RequestInputKind,
    RequestInputRecord,
    SideEffectKind,
    SideEffectRecord,
    Span,
    SqlOperation,
)
from legacylens.publisher import (
    MANIFEST_NAME,
    SiteBuildError,
    build_documentation,
    mkdocs_config,
    publish_site,
    write_documentation,
)

PAGE_ORDER = (
    "index.md",
    "architecture.md",
    "entry-points.md",
    "database.md",
    "side-effects.md",
    "hotspots.md",
    "files.md",
)


def _user_facts() -> FileFacts:
    path = "src/User.php"
    user = ClassRecord(
        file_path=path,
        name="User",
        qualified_name="User",
        span=Span(3, 20),
    )
    admin = ClassRecord(
        file_path=path,
        name="Admin",
        qualified_name="Admin",
        span=Span(22, 30),
        extends="User",
    )
    find = FunctionRecord(
        file_path=path,
        name="find",
        qualified_name="User::find",
        signature="public static function find(int $id): ?array",
        span=Span(5, 12),
        class_id=user.id,
        cyclomatic_complexity=3,
        max_nesting=2,
    )
    return FileFacts(
        file=FileRecord(
            path=path,
            language=Language.PHP,
            content_hash="sha256:user",
            size_bytes=512,
            loc=20,
        ),
        classes=(user, admin),
        functions=(find,),
        edges=(
            EdgeRecord(
                src=path,
                dst="src/db.php",
                kind=EdgeKind.REQUIRE,
                file_path=path,
                line=2,
            ),
            EdgeRecord(
                src=admin.id,
                dst=user.id,
                kind=EdgeKind.EXTENDS,
                file_path=path,
                line=22,
            ),
            EdgeRecord(
                src=find.id,
                dst="fn:src/db.php#db_connect",
                kind=EdgeKind.CALL,
                file_path=path,
                line=8,
            ),
            EdgeRecord(
                src=find.id,
                dst="\\Legacy\\Db::connect",
                kind=EdgeKind.CALL,
                file_path=path,
                line=9,
                resolved=False,
                raw="\\Legacy\\Db::connect($id)",
            ),
        ),
        data_access=(
            DataAccessRecord(
                file_path=path,
                function_id=find.id,
                operation=SqlOperation.SELECT,
                table_name="users",
                sql="SELECT id, email FROM users WHERE id = :id",
                line=6,
                driver="PDO",
                parameters=(":id",),
            ),
            DataAccessRecord(
                file_path=path,
                function_id=find.id,
                operation=SqlOperation.INSERT,
                table_name="users",
                sql="INSERT INTO users (email) VALUES (:email)",
                line=7,
                driver="PDO",
            ),
            DataAccessRecord(
                file_path=path,
                function_id=find.id,
                operation=SqlOperation.UPDATE,
                table_name="users",
                sql="UPDATE users SET seen = 1 WHERE id = :id",
                line=8,
                driver="PDO",
            ),
            DataAccessRecord(
                file_path=path,
                operation=SqlOperation.SELECT,
                table_name="articles",
                sql="SELECT title FROM articles",
                line=11,
                driver="PDO",
            ),
        ),
        entry_points=(
            EntryPointRecord(
                file_path=path,
                kind=EntryKind.ROUTE,
                path="/users/{id}",
                line=5,
                function_id=find.id,
                http_method=HttpMethod.GET,
                framework="legacy-router",
                requires_auth=True,
                auth_hint="session guard",
            ),
        ),
        request_inputs=(
            RequestInputRecord(
                file_path=path,
                kind=RequestInputKind.QUERY,
                name="id",
                line=6,
                function_id=find.id,
            ),
        ),
        side_effects=(
            SideEffectRecord(
                file_path=path,
                kind=SideEffectKind.FILESYSTEM,
                detail="file write report.csv",
                line=11,
                function_id=find.id,
                sink="file_put_contents",
            ),
            SideEffectRecord(
                file_path=path,
                kind=SideEffectKind.SESSION,
                detail="session write user",
                line=12,
                sink="$_SESSION",
            ),
        ),
    )


def _db_facts() -> FileFacts:
    path = "src/db.php"
    return FileFacts(
        file=FileRecord(
            path=path,
            language=Language.PHP,
            content_hash="sha256:db",
            size_bytes=128,
            loc=10,
        ),
        functions=(
            FunctionRecord(
                file_path=path,
                name="db_connect",
                qualified_name="db_connect",
                signature="function db_connect(): PDO",
                span=Span(1, 10),
            ),
        ),
        request_inputs=(
            RequestInputRecord(
                file_path=path,
                kind=RequestInputKind.COOKIE,
                name="theme",
                line=4,
            ),
        ),
    )


@pytest.fixture
def store() -> Iterator[FactStore]:
    with FactStore() as opened:
        opened.replace_file(_user_facts())
        opened.replace_file(_db_facts())
        yield opened


def _markdown(store: FactStore) -> dict[str, str]:
    documentation = build_documentation(store)
    return {page.filename: page.markdown for page in documentation.pages}


def test_page_set_titles_and_fact_totals(store: FactStore) -> None:
    documentation = build_documentation(store)

    assert tuple(page.filename for page in documentation.pages) == PAGE_ORDER
    assert documentation.page("database").title == "Database usage"
    assert documentation.facts == {
        "classes": 2,
        "data_access": 4,
        "entry_points": 1,
        "files": 2,
        "functions": 2,
        "loc": 30,
        "request_inputs": 2,
        "side_effects": 2,
        "tables": 2,
    }


def test_index_links_every_page_and_summarises_the_inventory(store: FactStore) -> None:
    index = build_documentation(store).page("index").markdown

    assert "# LegacyLens documentation" in index
    assert "| Files | 2 |" in index
    assert "| Recorded side effects | 2 |" in index
    for filename in PAGE_ORDER[1:]:
        assert f"]({filename})" in index


def test_architecture_page_draws_resolved_relationships(store: FactStore) -> None:
    page = _markdown(store)["architecture.md"]

    assert "```mermaid" in page
    assert "flowchart LR" in page
    assert "db_connect" in page
    assert "Admin" in page
    assert "Unresolved references" in page
    assert "Legacy" in page


def test_database_page_reports_crud_coverage_without_inventing_schema(store: FactStore) -> None:
    page = _markdown(store)["database.md"]

    assert "| `users` | RCU | D |" in page
    assert "| `articles` | R | C, U, D |" in page
    assert "```mermaid" in page
    assert "No foreign keys or schema relationships are inferred" in page


def test_entry_points_page_links_handlers_and_inputs(store: FactStore) -> None:
    page = _markdown(store)["entry-points.md"]

    assert "/users/{id}" in page
    assert "required (session guard)" in page
    assert "Request inputs by kind" in page
    assert "| query | 1 |" in page
    assert "Inputs outside any entry point" in page
    assert "| cookie | `theme` |" in page


def test_side_effects_page_groups_kinds_and_unattributed_sites(store: FactStore) -> None:
    page = _markdown(store)["side-effects.md"]

    assert "| filesystem | 1 |" in page
    assert "file write report.csv" in page
    assert "Effects outside any function" in page
    assert "session write user" in page


def test_hotspots_page_ranks_functions_and_files_page_lists_paths(store: FactStore) -> None:
    pages = _markdown(store)

    assert "User::find" in pages["hotspots.md"]
    assert "| Length (LOC) | 2 |" in pages["hotspots.md"]
    assert "`src/User.php`" in pages["files.md"]
    assert "`src/db.php`" in pages["files.md"]


def test_publishing_is_deterministic_and_contains_no_environment_paths(store: FactStore) -> None:
    first = _markdown(store)
    second = _markdown(store)

    assert first == second
    assert str(Path.cwd()) not in "\n".join(first.values())


def test_empty_store_still_produces_the_full_page_set() -> None:
    with FactStore() as store:
        documentation = build_documentation(store)

    assert tuple(page.filename for page in documentation.pages) == PAGE_ORDER
    assert "| Files | 0 |" in documentation.page("index").markdown
    for page in documentation.pages:
        assert page.markdown.endswith("\n")
        assert "```mermaid" not in page.markdown


def test_row_budgets_are_validated_and_truncation_is_stated(store: FactStore) -> None:
    with pytest.raises(ValueError, match="max_rows"):
        build_documentation(store, max_rows=0)

    documentation = build_documentation(store, max_rows=1)

    assert "Only the first 1 of 2 files are shown." in documentation.page("files").markdown
    assert "Only the first 1 of 2 tables are shown." in documentation.page("database").markdown
    assert (
        "Only the first 1 of 2 effect sites are shown."
        in documentation.page("side-effects").markdown
    )

    cramped = build_documentation(store, max_nodes=1, max_edges=1, max_tables=1)
    architecture = cramped.page("architecture").markdown
    database = cramped.page("database").markdown
    assert "The diagram was truncated to fit that budget." in architecture
    assert "The diagram was truncated to fit that budget." in database


def test_write_documentation_removes_stale_manifest_pages(store: FactStore, tmp_path: Path) -> None:
    documentation = build_documentation(store)
    docs_dir = tmp_path / "docs"
    first_pages, removed = write_documentation(documentation, docs_dir)

    assert removed == ()
    assert first_pages == tuple(sorted(PAGE_ORDER))

    stale = docs_dir / "old-page.md"
    stale.write_text("# Old\n", encoding="utf-8")
    manifest_path = docs_dir / MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pages"].append("old-page.md")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    pages, removed = write_documentation(documentation, docs_dir)

    assert removed == ("old-page.md",)
    assert not stale.exists()
    assert pages == tuple(sorted(PAGE_ORDER))


def test_write_documentation_ignores_manifest_paths_outside_the_docs_tree(
    store: FactStore, tmp_path: Path
) -> None:
    documentation = build_documentation(store)
    docs_dir = tmp_path / "docs"
    write_documentation(documentation, docs_dir)
    outside = tmp_path / "escape.md"
    outside.write_text("# Keep me\n", encoding="utf-8")

    manifest_path = docs_dir / MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pages"].append("../escape.md")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    _, removed = write_documentation(documentation, docs_dir)

    assert removed == ()
    assert outside.is_file()


def test_publish_site_writes_pages_config_javascript_and_manifest(
    store: FactStore, tmp_path: Path
) -> None:
    root = tmp_path / "out"
    result = publish_site(store, root)

    assert result.built is False
    assert result.skipped_reason is None
    assert result.pages == tuple(sorted(PAGE_ORDER))
    assert not result.site_dir.exists()

    config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))
    assert config == mkdocs_config(build_documentation(store))
    assert config["site_name"] == "LegacyLens"
    assert config["nav"][0] == {"Overview": "index.md"}

    for filename in PAGE_ORDER:
        assert (result.docs_dir / filename).is_file()
    assert (result.docs_dir / "javascripts" / "mermaid.js").is_file()
    manifest = json.loads((result.docs_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest == {"version": 1, "pages": sorted(PAGE_ORDER)}


def test_build_is_skipped_with_a_reason_when_mkdocs_is_missing(
    store: FactStore, tmp_path: Path
) -> None:
    if importlib.util.find_spec("mkdocs") is not None:
        pytest.skip("MkDocs is installed")

    result = publish_site(store, tmp_path / "out", build=True)

    assert result.built is False
    assert result.skipped_reason is not None
    assert "MkDocs is not installed" in result.skipped_reason


def test_mkdocs_failure_raises_site_build_error(
    store: FactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from legacylens.publisher import site as site_module

    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(site_module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(site_module.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(SiteBuildError, match="boom"):
        publish_site(store, tmp_path / "out", build=True)
