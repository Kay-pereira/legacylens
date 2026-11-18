"""Java parser: packages, declarations, spans, overloads and syntax recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacylens.ir import (
    ClassKind,
    Confidence,
    FileFacts,
    FileRecord,
    Language,
    SqlOperation,
    Visibility,
)
from legacylens.parsers import (
    JavaParser,
    JspParser,
    ParseError,
    XmlConfigParser,
    parse_source,
    parser_for,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_JAVA_APP = FIXTURES / "java_app"


def _record(path: str, language: Language = Language.JAVA) -> FileRecord:
    return FileRecord(
        path=path,
        language=language,
        content_hash="sha256:" + "0" * 64,
        size_bytes=0,
    )


def _parse_fixture(relative_path: str) -> FileFacts:
    path = _JAVA_APP / relative_path
    return parse_source(path.read_bytes(), _record(relative_path))


def test_parses_package_class_constructor_and_methods() -> None:
    facts = _parse_fixture("src/main/java/com/example/User.java")

    assert facts.file.parse_ok
    assert [(item.qualified_name, item.kind) for item in facts.classes] == [
        ("com.example.User", ClassKind.CLASS)
    ]
    assert facts.classes[0].span.start_line == 3
    assert facts.classes[0].span.end_line == 24

    assert [item.qualified_name for item in facts.functions] == [
        "com.example.User::User",
        "com.example.User::getId",
        "com.example.User::getEmail",
        "com.example.User::getRole",
    ]
    constructor, get_id, get_email, get_role = facts.functions
    assert constructor.name == "User"
    assert (constructor.span.start_line, constructor.span.end_line) == (8, 11)
    assert constructor.signature == "public User(int id, String email)"
    assert constructor.class_id == facts.classes[0].id
    assert constructor.visibility is Visibility.PUBLIC
    assert not constructor.is_static
    assert (get_id.span.start_line, get_id.span.end_line) == (13, 15)
    assert (get_email.span.start_line, get_email.span.end_line) == (17, 19)
    assert (get_role.span.start_line, get_role.span.end_line) == (21, 23)
    assert get_id.class_id == facts.classes[0].id


def test_annotations_stay_in_the_signature_and_modifiers_are_keywords() -> None:
    facts = _parse_fixture("src/main/java/com/example/UserServlet.java")

    assert [
        (item.qualified_name, item.span.start_line, item.span.end_line) for item in facts.classes
    ] == [("com.example.UserServlet", 10, 26)]
    assert len(facts.functions) == 1
    method = facts.functions[0]
    assert method.qualified_name == "com.example.UserServlet::doGet"
    assert method.visibility is Visibility.PROTECTED
    assert method.modifiers == ("protected",)
    assert method.signature.startswith("@Override protected void doGet(")
    assert (method.span.start_line, method.span.end_line) == (13, 25)


def test_interfaces_nested_types_and_overloads_are_keyed_without_collisions() -> None:
    source = b"""package com.example;

public interface Api {
    void call();
    default void ping() {}
    static void boot() {}
}

class Outer {
    static class Inner { void m() {} }

    public int add(int a, int b) { return a + b; }
    public String add(String a) { return a; }
    void add() {}
}

enum Role { ADMIN; public String label() { return "x"; } }
record Point(int x, int y) { public int sum() { return x + y; } }
"""
    facts = parse_source(source, _record("src/Outer.java"))

    assert facts.file.parse_ok
    assert [(item.qualified_name, item.kind) for item in facts.classes] == [
        ("com.example.Api", ClassKind.INTERFACE),
        ("com.example.Outer", ClassKind.CLASS),
        ("com.example.Outer.Inner", ClassKind.CLASS),
        ("com.example.Role", ClassKind.ENUM),
        ("com.example.Point", ClassKind.RECORD),
    ]
    assert [item.qualified_name for item in facts.functions] == [
        "com.example.Api::call",
        "com.example.Api::ping",
        "com.example.Api::boot",
        "com.example.Outer.Inner::m",
        "com.example.Outer::add(int,int)",
        "com.example.Outer::add(String)",
        "com.example.Outer::add()",
        "com.example.Role::label",
        "com.example.Point::sum",
    ]

    call, ping, boot = facts.functions[:3]
    assert call.visibility is Visibility.PUBLIC
    assert call.is_abstract
    assert not ping.is_abstract
    assert boot.is_static
    assert facts.functions[3].visibility is Visibility.PACKAGE
    assert {item.class_id for item in facts.functions[4:7]} == {facts.classes[1].id}


def test_default_package_uses_short_qualified_names() -> None:
    facts = parse_source(b"class Plain { void run() {} }\n", _record("src/Plain.java"))

    assert facts.classes[0].qualified_name == "Plain"
    assert facts.functions[0].qualified_name == "Plain::run"


def test_syntax_errors_are_recorded_and_recovered_declarations_kept() -> None:
    facts = parse_source(b"package p;\npublic class Broken {\n", _record("src/Broken.java"))

    assert not facts.file.parse_ok
    assert facts.file.parse_error is not None
    assert facts.file.parse_error.startswith("missing ")
    assert [item.qualified_name for item in facts.classes] == ["p.Broken"]


def test_parser_rejects_other_languages() -> None:
    with pytest.raises(ParseError, match="php"):
        JavaParser().parse(b"<?php class User {}", _record("src/User.php", Language.PHP))


def test_registry_resolves_java_and_php() -> None:
    assert isinstance(parser_for(Language.JAVA), JavaParser)
    assert isinstance(parser_for("java"), JavaParser)
    assert isinstance(parser_for(Language.JSP), JspParser)
    assert isinstance(parser_for(Language.XML), XmlConfigParser)
    with pytest.raises(LookupError, match="other"):
        parser_for(Language.OTHER)


def test_jdbc_sql_is_attributed_with_conservative_dynamic_handling() -> None:
    source = b'''package com.example;
import java.sql.Connection;
public class Dao {
    private Connection connection;
    void save() throws Exception {
        String sql = "INSERT INTO users (name) VALUES (?)";
        connection.prepareStatement(sql);
    }
    void purge() throws Exception {
        connection.createStatement().execute(
            "DELETE FROM sessions " +
            "WHERE expires < NOW()");
    }
    void dynamic(String table) throws Exception {
        connection.createStatement().executeQuery("SELECT * FROM " + table);
    }
    void block() throws Exception {
        String sql = """
            SELECT id, email
            FROM users
            WHERE role = 'admin'
            """;
        connection.createStatement().executeQuery(sql);
    }
}
'''
    facts = parse_source(source, _record("src/main/java/com/example/Dao.java"))

    assert [
        (item.operation, item.table_name, item.parameters, item.confidence)
        for item in facts.data_access
    ] == [
        (SqlOperation.INSERT, "users", ("?",), Confidence.VERIFIED),
        (SqlOperation.DELETE, "sessions", (), Confidence.VERIFIED),
        (SqlOperation.SELECT, None, (), Confidence.INFERRED),
        (SqlOperation.SELECT, "users", (), Confidence.VERIFIED),
    ]
    assert {item.driver for item in facts.data_access} == {"JDBC"}
    assert [item.function_id for item in facts.data_access] == [
        facts.functions[0].id,
        facts.functions[1].id,
        facts.functions[2].id,
        facts.functions[3].id,
    ]
    assert facts.data_access[2].sql == "SELECT * FROM <dynamic>"
