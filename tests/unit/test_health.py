"""Project health report: deterministic findings, status and JSON shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylenscli.analysis import (
    FindingSeverity,
    HealthStatus,
    build_health_report,
)
from legacylenscli.ingestion import ScanOptions, scan_repository
from legacylenscli.ir import FactStore
from legacylenscli.parsers import parse_source


def _store_from(root: Path, db: Path) -> FactStore:
    scan = scan_repository(root, ScanOptions())
    store = FactStore(db)
    for scanned in scan.files:
        store.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
    return store


def _write_app(root: Path) -> None:
    (root / "index.php").write_text(
        "<?php\nrequire_once 'helpers.php';\necho live_helper();\n",
        encoding="utf-8",
    )
    (root / "helpers.php").write_text(
        """<?php

function live_helper(): string
{
    if (true) {
        if (true) {
            return 'live';
        }
    }
    return 'dead';
}

function dead_helper(): string
{
    return 'dead';
}
""",
        encoding="utf-8",
    )


def test_empty_store_is_healthy(tmp_path: Path) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        report = build_health_report(store)

    assert report.status is HealthStatus.OK
    assert report.findings == ()


def test_dead_code_makes_the_report_need_attention(tmp_path: Path) -> None:
    _write_app(tmp_path)

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_health_report(store, source_root=tmp_path)

    codes = {item.code for item in report.findings}
    assert "dead-code" in codes
    assert report.status is HealthStatus.ATTENTION
    dead = next(item for item in report.findings if item.code == "dead-code")
    assert dead.severity is FindingSeverity.WARNING
    assert dead.value == 1
    assert dead.citations == ("fn:helpers.php#dead_helper",)


def test_thresholds_produce_stable_findings(tmp_path: Path) -> None:
    _write_app(tmp_path)

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_health_report(
            store,
            complexity_threshold=1,
            function_length_threshold=1,
            nesting_threshold=1,
        )

    by_code = {item.code: item for item in report.findings}
    assert by_code["high-complexity"].severity is FindingSeverity.WARNING
    assert by_code["high-complexity"].threshold == 1
    assert by_code["long-functions"].severity is FindingSeverity.INFO
    assert by_code["deep-nesting"].severity is FindingSeverity.INFO


def test_parse_errors_can_make_the_report_critical(tmp_path: Path) -> None:
    (tmp_path / "broken.php").write_text(
        "<?php\nfunction broken( {\n",
        encoding="utf-8",
    )

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_health_report(store, source_root=tmp_path)

    parse = next(item for item in report.findings if item.code == "parse-errors")
    assert parse.severity is FindingSeverity.CRITICAL
    assert report.status is HealthStatus.CRITICAL


def test_report_is_json_ready(tmp_path: Path) -> None:
    _write_app(tmp_path)

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_health_report(store, source_root=tmp_path)

    data = report.as_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["summary"]["dead_code"] == 1
    assert data["dead_code"]["candidates"][0]["name"] == "dead_helper"


def test_thresholds_are_validated(tmp_path: Path) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        with pytest.raises(ValueError, match="complexity_threshold"):
            build_health_report(store, complexity_threshold=0)
        with pytest.raises(ValueError, match="parse_error_critical_ratio"):
            build_health_report(store, parse_error_critical_ratio=1.5)
