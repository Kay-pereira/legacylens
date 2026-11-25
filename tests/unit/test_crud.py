"""CRUD coverage pivoted from data-access statement/table rows."""

from __future__ import annotations

import json

from legacylenscli.analysis import build_crud_from_records
from legacylenscli.ir import Confidence, DataAccessRecord, SqlOperation


def _record(
    table: str | None,
    operation: SqlOperation,
    *,
    function: str | None = "UserRepository::save",
    file_path: str = "src/UserRepository.java",
    line: int = 10,
    sql: str = "",
) -> DataAccessRecord:
    return DataAccessRecord(
        file_path=file_path,
        operation=operation,
        sql=sql or f"{operation.value.upper()} {table or 'unknown'}",
        line=line,
        function_id=function,
        table_name=table,
        confidence=Confidence.VERIFIED,
    )


def test_operations_are_grouped_per_table_with_functions_and_files() -> None:
    matrix = build_crud_from_records(
        [
            _record("users", SqlOperation.SELECT),
            _record("users", SqlOperation.SELECT, function="UserRepository::find"),
            _record("users", SqlOperation.INSERT),
            _record(
                "users",
                SqlOperation.UPDATE,
                function=None,
                file_path="public/admin.php",
                line=3,
            ),
        ]
    )

    (table,) = matrix.tables
    assert table.table_name == "users"
    assert table.crud == "RCU"
    assert table.missing == ("D",)
    assert [item.operation for item in table.operations] == [
        SqlOperation.SELECT,
        SqlOperation.INSERT,
        SqlOperation.UPDATE,
    ]
    assert table.operations[0].records == 2
    assert table.operations[0].functions == (
        "UserRepository::find",
        "UserRepository::save",
    )
    assert table.functions == ("UserRepository::find", "UserRepository::save")
    assert table.files == ("public/admin.php", "src/UserRepository.java")


def test_replace_counts_as_create_and_unknown_tables_stay_separate() -> None:
    matrix = build_crud_from_records(
        [
            _record("users", SqlOperation.REPLACE),
            _record(None, SqlOperation.SELECT, sql="SELECT * FROM <dynamic>"),
            _record(None, SqlOperation.DELETE, function=None),
        ]
    )

    (table,) = matrix.tables
    assert table.crud == "C"
    assert table.missing == ("R", "U", "D")
    assert matrix.records == 3
    assert matrix.script_records == 1
    assert [(item.operation, item.table_name) for item in matrix.unknown_table] == [
        (SqlOperation.DELETE, None),
        (SqlOperation.SELECT, None),
    ]


def test_ddl_and_exec_do_not_claim_crud_letters() -> None:
    matrix = build_crud_from_records(
        [
            _record("audit_log", SqlOperation.DDL),
            _record("refresh_stats", SqlOperation.EXEC),
        ]
    )

    assert [(item.table_name, item.crud, item.missing) for item in matrix.tables] == [
        ("audit_log", "", ()),
        ("refresh_stats", "", ()),
    ]
    summary = matrix.as_dict()["summary"]
    assert summary["tables"] == 2
    assert summary["read_only"] == []
    assert summary["write_only"] == []
    assert summary["complete"] == []


def test_summary_reports_read_only_write_only_and_missing_letters() -> None:
    matrix = build_crud_from_records(
        [
            _record("articles", SqlOperation.SELECT),
            _record("users", SqlOperation.INSERT),
            _record("users", SqlOperation.UPDATE),
            _record("sessions", SqlOperation.SELECT),
            _record("sessions", SqlOperation.DELETE),
        ]
    )

    data = matrix.as_dict()
    assert [table["table"] for table in data["tables"]] == [
        "articles",
        "sessions",
        "users",
    ]
    assert data["summary"] == {
        "tables": 3,
        "read_only": ["articles"],
        "write_only": ["users"],
        "complete": [],
        "missing_operations": {
            "articles": ["C", "U", "D"],
            "sessions": ["C", "U"],
            "users": ["R", "D"],
        },
    }
    assert json.loads(json.dumps(data)) == data


def test_matrix_with_all_crud_letters_is_complete() -> None:
    matrix = build_crud_from_records(
        [
            _record("users", SqlOperation.SELECT),
            _record("users", SqlOperation.INSERT),
            _record("users", SqlOperation.UPDATE),
            _record("users", SqlOperation.DELETE),
        ]
    )

    (table,) = matrix.tables
    assert table.crud == "RCUD"
    assert table.missing == ()
    assert matrix.as_dict()["summary"]["complete"] == ["users"]
