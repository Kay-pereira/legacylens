"""End-to-end: scan a mixed PHP/Java app, store data access and build the CRUD matrix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylenscli.analysis import build_crud
from legacylenscli.ingestion import ScanOptions, scan_repository
from legacylenscli.ir import Confidence, FactStore, SqlOperation
from legacylenscli.parsers import parse_source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WEB_APP = FIXTURES / "legacy_web"


def _matrix(tmp_path: Path):
    scan = scan_repository(WEB_APP, ScanOptions())
    assert len(scan.files) == 18

    with FactStore(tmp_path / "facts.db") as store:
        for scanned in scan.files:
            store.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
        records = store.list_data_access()
        return records, build_crud(store)


def test_php_and_java_statements_keep_function_driver_and_placeholders(tmp_path: Path) -> None:
    records, _ = _matrix(tmp_path)

    assert [
        (
            item.file_path,
            item.line,
            item.operation,
            item.table_name,
            item.parameters,
            item.driver,
            item.function_id,
        )
        for item in records
    ] == [
        (
            "public/inc/helpers.php",
            13,
            SqlOperation.SELECT,
            "articles",
            (":term", ":offset"),
            "PDO",
            "fn:public/inc/helpers.php#search",
        ),
        (
            "src/main/java/com/example/SaveUserAction.java",
            31,
            SqlOperation.INSERT,
            "users",
            ("?", "?"),
            "JDBC",
            "fn:src/main/java/com/example/SaveUserAction.java#com.example.SaveUserAction::save",
        ),
        (
            "src/main/java/com/example/SaveUserAction.java",
            39,
            SqlOperation.SELECT,
            "users",
            ("?",),
            "JDBC",
            "fn:src/main/java/com/example/SaveUserAction.java"
            "#com.example.SaveUserAction::findEmail",
        ),
    ]
    assert {item.confidence for item in records} == {Confidence.VERIFIED}


def test_crud_matrix_pivots_tables_operations_and_functions(tmp_path: Path) -> None:
    records, matrix = _matrix(tmp_path)

    assert matrix.records == len(records) == 3
    assert matrix.script_records == 0
    assert matrix.unknown_table == ()
    assert [(table.table_name, table.crud, table.missing) for table in matrix.tables] == [
        ("articles", "R", ("C", "U", "D")),
        ("users", "RC", ("U", "D")),
    ]

    articles, users = matrix.tables
    assert articles.functions == ("fn:public/inc/helpers.php#search",)
    assert users.functions == (
        "fn:src/main/java/com/example/SaveUserAction.java#com.example.SaveUserAction::findEmail",
        "fn:src/main/java/com/example/SaveUserAction.java#com.example.SaveUserAction::save",
    )


def test_matrix_summary_is_json_ready_and_reports_gaps(tmp_path: Path) -> None:
    _, matrix = _matrix(tmp_path)

    data = matrix.as_dict()
    assert data["summary"] == {
        "tables": 2,
        "read_only": ["articles"],
        "write_only": [],
        "complete": [],
        "missing_operations": {
            "articles": ["C", "U", "D"],
            "users": ["U", "D"],
        },
    }
    assert json.loads(json.dumps(data)) == data
