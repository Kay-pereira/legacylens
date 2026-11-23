"""End-to-end: scan the legacy fixture, publish the site and check the pages."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import FactStore
from legacylens.parsers import parse_source
from legacylens.publisher import MANIFEST_NAME, publish_site

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WEB_APP = FIXTURES / "legacy_web"
PAGES = (
    "index.md",
    "claims.md",
    "architecture.md",
    "entry-points.md",
    "database.md",
    "side-effects.md",
    "hotspots.md",
    "files.md",
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FactStore]:
    scan = scan_repository(WEB_APP, ScanOptions())
    assert len(scan.files) == 18
    with FactStore(tmp_path / "facts.db") as opened:
        for scanned in scan.files:
            opened.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
        yield opened


def test_publish_site_from_the_legacy_fixture(store: FactStore, tmp_path: Path) -> None:
    result = publish_site(store, tmp_path / "out")

    assert result.built is False
    assert result.skipped_reason is None
    assert result.pages == tuple(sorted(PAGES))

    index = (result.docs_dir / "index.md").read_text(encoding="utf-8")
    architecture = (result.docs_dir / "architecture.md").read_text(encoding="utf-8")
    entry_points = (result.docs_dir / "entry-points.md").read_text(encoding="utf-8")
    database = (result.docs_dir / "database.md").read_text(encoding="utf-8")
    side_effects = (result.docs_dir / "side-effects.md").read_text(encoding="utf-8")

    assert "| Files | 18 |" in index
    assert "| Entry points | 10 |" in index
    assert "| Tables named in SQL | 2 |" in index

    assert "```mermaid" in architecture
    assert "flowchart LR" in architecture
    assert "Unresolved references" in architecture

    assert "```mermaid" in entry_points
    assert "pie showData" in entry_points
    assert "/api/users/*" in entry_points

    assert "| `users` | RC | U, D |" in database
    assert "| `articles` | R | C, U, D |" in database

    assert "| filesystem | 3 |" in side_effects
    assert "| session | 6 |" in side_effects
    assert "https://example.com/notify" in side_effects
    assert "archiveUsers" in side_effects

    manifest = json.loads((result.docs_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["pages"] == sorted(PAGES)


def test_publishing_twice_keeps_the_output_stable(store: FactStore, tmp_path: Path) -> None:
    root = tmp_path / "out"
    first = publish_site(store, root)
    snapshot = {filename: (first.docs_dir / filename).read_bytes() for filename in first.pages}
    config = first.config_path.read_bytes()

    second = publish_site(store, root)

    assert second.removed == ()
    assert second.pages == first.pages
    assert second.config_path.read_bytes() == config
    for filename in first.pages:
        assert (second.docs_dir / filename).read_bytes() == snapshot[filename]


def test_mkdocs_builds_the_generated_site(store: FactStore, tmp_path: Path) -> None:
    pytest.importorskip("mkdocs")

    result = publish_site(store, tmp_path / "out", build=True, strict=True)

    assert result.built is True
    assert result.skipped_reason is None
    assert (result.site_dir / "index.html").is_file()
    assert (result.site_dir / "database" / "index.html").is_file()
