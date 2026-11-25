"""End-to-end: scan and parse fixture apps, then build inventory and metrics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylenscli.analysis import build_inventory, build_metrics
from legacylenscli.ingestion import DEFAULT_EXCLUDES, ScanOptions, scan_repository
from legacylenscli.ir import FactStore, Language
from legacylenscli.parsers import parse_source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_APP_EXCLUDES = (*DEFAULT_EXCLUDES, "legacy_web")


def test_inventory_and_metrics_over_fixture_apps(tmp_path: Path) -> None:
    scan = scan_repository(
        FIXTURES,
        ScanOptions(include=("*.php", "*.java"), exclude=_APP_EXCLUDES),
    )
    assert len(scan.files) == 6

    with FactStore(tmp_path / "facts.db") as store:
        for scanned in scan.files:
            facts = parse_source(scanned.absolute_path.read_bytes(), scanned.record)
            store.replace_file(facts)

        inventory = build_inventory(store)
        metrics = build_metrics(store)

    assert inventory.files == 6
    assert inventory.size_bytes == sum(item.record.size_bytes for item in scan.files)
    assert inventory.loc == sum(item.record.loc for item in scan.files)
    assert inventory.classes == 4
    assert inventory.functions == 10
    assert inventory.methods == 9
    assert inventory.top_level_functions == 1
    assert inventory.parse_errors == 0
    assert inventory.generated_files == 0
    assert inventory.vendored_files == 0
    assert [(item.language, item.files) for item in inventory.languages] == [
        (Language.JAVA, 3),
        (Language.PHP, 3),
    ]
    assert [(item.kind, item.count) for item in inventory.class_kinds] == [("class", 4)]

    assert metrics.function_count == 10
    assert metrics.length.as_dict() == {
        "count": 10,
        "total": 56,
        "min": 3,
        "max": 13,
        "mean": 5.6,
        "median": 4.5,
        "p95": 13,
    }
    assert metrics.complexity.as_dict() == {
        "count": 10,
        "total": 14,
        "min": 1,
        "max": 3,
        "mean": 1.4,
        "median": 1.0,
        "p95": 3,
    }
    assert metrics.nesting.as_dict() == {
        "count": 10,
        "total": 3,
        "min": 0,
        "max": 2,
        "mean": 0.3,
        "median": 0.0,
        "p95": 2,
    }

    find_by_id = next(
        item for item in metrics.most_complex if item.qualified_name.endswith("::findById")
    )
    assert (find_by_id.loc, find_by_id.cyclomatic_complexity, find_by_id.max_nesting) == (
        8,
        3,
        2,
    )
    assert metrics.most_complex[0] == find_by_id
    assert metrics.deepest[0] == find_by_id
    assert metrics.longest[0].qualified_name == "com.example.UserServlet::doGet"

    # Both reports are intended for JSON artefacts.
    json.dumps(inventory.as_dict())
    json.dumps(metrics.as_dict())
