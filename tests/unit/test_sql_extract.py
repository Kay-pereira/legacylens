"""SQL string classification, table/placeholder extraction and source attribution."""

from __future__ import annotations

import pytest

from legacylenscli.analysis import (
    build_data_access,
    detect_driver,
    looks_like_sql,
    parse_sql,
    split_sql_statements,
)
from legacylenscli.ir import Confidence, FunctionRecord, Language, Span, SqlOperation


def _function(
    qualified_name: str,
    start: int,
    end: int,
    *,
    file_path: str = "src/User.php",
) -> FunctionRecord:
    return FunctionRecord(
        file_path=file_path,
        name=qualified_name.rsplit("::", 1)[-1],
        qualified_name=qualified_name,
        signature=f"{qualified_name}()",
        span=Span(start_line=start, end_line=end),
    )


def test_split_ignores_semicolons_inside_strings_and_comments() -> None:
    text = (
        "SELECT ';' AS semi; -- trailing ; comment\n"
        "UPDATE users SET note = 'a;b' WHERE id = 1;\n"
        "/* ; */ DELETE FROM sessions"
    )

    assert split_sql_statements(text) == (
        ("SELECT ';' AS semi", 0),
        (" -- trailing ; comment\nUPDATE users SET note = 'a;b' WHERE id = 1", 0),
        ("\n/* ; */ DELETE FROM sessions", 2),
    )


def test_prose_that_starts_with_a_keyword_is_not_sql() -> None:
    assert looks_like_sql("Select a user from the list") is False
    assert looks_like_sql("Update the list of names") is False
    assert looks_like_sql("SELECT id FROM users") is True
    assert looks_like_sql("UPDATE users SET role = 'admin'") is True


def test_single_statement_keeps_tables_and_named_placeholders() -> None:
    (statement,) = parse_sql("SELECT id, email FROM users WHERE id = :id AND email = :email")

    assert statement.operation is SqlOperation.SELECT
    assert statement.tables == ("users",)
    assert statement.parameters == (":id", ":email")
    assert statement.confidence is Confidence.VERIFIED


def test_joins_report_every_table_once() -> None:
    (statement,) = parse_sql("SELECT u.id, o.total FROM users u JOIN orders o ON o.user_id = u.id")

    assert statement.tables == ("users", "orders")


def test_backtick_and_bracket_identifiers_choose_the_right_dialect() -> None:
    (backtick,) = parse_sql("SELECT COUNT(*) FROM `users` WHERE id = ?")
    (bracket,) = parse_sql("SELECT COUNT(*) FROM [users] WHERE id = ?")

    assert backtick.tables == ("users",)
    assert bracket.tables == ("users",)
    assert backtick.confidence is Confidence.VERIFIED
    assert bracket.confidence is Confidence.VERIFIED


def test_percent_and_dollar_placeholders_are_recognised() -> None:
    (percent,) = parse_sql("SELECT * FROM users WHERE email = %s AND role = %(role)s")
    (dollar,) = parse_sql("SELECT * FROM users WHERE id = $1 AND name = :name")

    assert percent.parameters == ("%s", "%(role)s")
    assert dollar.parameters == ("$1", ":name")


def test_unparseable_statement_falls_back_to_inferred_tables() -> None:
    (statement,) = parse_sql("REPLACE INTO users (id, email) VALUES (1, 'a@b.c')")

    assert statement.operation is SqlOperation.REPLACE
    assert statement.tables == ("users",)
    assert statement.confidence is Confidence.INFERRED


def test_dynamic_fragment_is_never_expanded_into_a_table() -> None:
    (statement,) = parse_sql("SELECT * FROM <dynamic> WHERE id = ?")

    assert statement.operation is SqlOperation.SELECT
    assert statement.tables == ()
    assert statement.confidence is Confidence.INFERRED


def test_insert_select_attributes_the_write_to_the_target_table() -> None:
    records = build_data_access(
        file_path="src/Log.php",
        snippets=[("INSERT INTO logs (message) SELECT message FROM raw", 4)],
    )

    assert [(item.operation, item.table_name) for item in records] == [
        (SqlOperation.INSERT, "logs"),
        (SqlOperation.SELECT, "raw"),
    ]


def test_function_attribution_uses_the_innermost_span() -> None:
    outer = _function("Service::run", 1, 20)
    inner = _function("Service::run", 5, 9)

    records = build_data_access(
        file_path="src/User.php",
        snippets=[
            ("SELECT id FROM users", 7),
            ("DELETE FROM sessions", 15),
            ("UPDATE counters SET total = 1", 25),
        ],
        functions=[outer, inner],
    )

    assert [(item.operation, item.table_name, item.function_id) for item in records] == [
        (SqlOperation.SELECT, "users", inner.id),
        (SqlOperation.DELETE, "sessions", outer.id),
        (SqlOperation.UPDATE, "counters", None),
    ]


def test_statement_line_offsets_are_applied_to_the_snippet_start() -> None:
    records = build_data_access(
        file_path="src/User.php",
        snippets=[("SELECT id FROM users;\nDELETE FROM sessions", 10)],
    )

    assert [(item.operation, item.line) for item in records] == [
        (SqlOperation.SELECT, 10),
        (SqlOperation.DELETE, 11),
    ]


@pytest.mark.parametrize(
    ("language", "text", "expected"),
    [
        (Language.PHP, "$pdo = new PDO($dsn);", "PDO"),
        (Language.PHP, "$result = mysqli_query($db, $sql);", "mysqli"),
        (Language.PHP, "global $wpdb;", "wpdb"),
        (Language.PHP, "mysql_query($sql);", None),
        (Language.JAVA, "import java.sql.Connection;", "JDBC"),
        (Language.JAVA, "import java.util.List;", None),
    ],
)
def test_driver_requires_evidence_in_the_file(
    language: Language, text: str, expected: str | None
) -> None:
    assert detect_driver(language, text) == expected
