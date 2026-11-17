"""End-to-end thin slice: scanner -> PHP parser -> SQLite store for one fixture file."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import ClassKind, FactStore
from legacylens.parsers import parse_source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_one_php_file_round_trips_from_scan_to_store(tmp_path: Path) -> None:
    scan = scan_repository(
        FIXTURES / "php_app",
        ScanOptions(include=("src/User.php",)),
    )
    assert len(scan.files) == 1
    scanned = scan.files[0]
    assert scanned.record.path == "src/User.php"
    assert scanned.record.loc > 0

    facts = parse_source(scanned.absolute_path.read_bytes(), scanned.record)
    assert facts.file == scanned.record

    with FactStore(tmp_path / "facts.db") as store:
        store.replace_file(facts)

        stored_file = store.get_file("src/User.php")
        classes = store.list_classes(file_path="src/User.php")
        functions = store.list_functions(file_path="src/User.php")

    assert stored_file == facts.file
    assert [(item.qualified_name, item.kind) for item in classes] == [("User", ClassKind.CLASS)]
    assert [(item.qualified_name, item.span.start_line) for item in functions] == [
        ("User::find", 5),
        ("User::promote", 14),
    ]
    assert {item.class_id for item in functions} == {classes[0].id}
