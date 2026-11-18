"""PHP parser: declarations, namespaces, spans and recoverable syntax errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacylens.ir import ClassKind, FileFacts, FileRecord, Language, Visibility
from legacylens.parsers import ParseError, PhpParser, parse_source, parser_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_PHP_APP = FIXTURES / "php_app"


def _record(path: str, language: Language = Language.PHP) -> FileRecord:
    return FileRecord(
        path=path,
        language=language,
        content_hash="sha256:" + "0" * 64,
        size_bytes=0,
    )


def _parse_fixture(relative_path: str) -> FileFacts:
    path = _PHP_APP / relative_path
    return parse_source(path.read_bytes(), _record(relative_path))


def test_parses_class_and_methods_from_fixture() -> None:
    facts = _parse_fixture("src/User.php")

    assert facts.file.parse_ok
    assert [(item.name, item.qualified_name, item.kind) for item in facts.classes] == [
        ("User", "User", ClassKind.CLASS)
    ]
    assert facts.classes[0].span.start_line == 3
    assert facts.classes[0].span.end_line == 20

    assert [item.qualified_name for item in facts.functions] == [
        "User::find",
        "User::promote",
    ]
    find, promote = facts.functions
    assert (find.span.start_line, find.span.end_line) == (5, 12)
    assert (promote.span.start_line, promote.span.end_line) == (14, 19)
    assert find.class_id == facts.classes[0].id
    assert promote.class_id == facts.classes[0].id
    assert find.signature == "public static function find(int $id): ?array"
    assert promote.signature == "public static function promote(int $id): void"
    assert find.visibility is Visibility.PUBLIC
    assert find.is_static
    assert not find.is_abstract
    assert find.modifiers == ("public", "static")


def test_parses_top_level_function_without_class() -> None:
    facts = _parse_fixture("inc/db.php")

    assert facts.classes == ()
    assert len(facts.functions) == 1
    function = facts.functions[0]
    assert function.name == "db_connect"
    assert function.qualified_name == "db_connect"
    assert function.class_id is None
    assert (function.span.start_line, function.span.end_line) == (3, 7)
    assert function.signature == "function db_connect(): PDO"


def test_script_without_declarations_yields_only_the_file() -> None:
    facts = _parse_fixture("index.php")

    assert facts.classes == ()
    assert facts.functions == ()
    assert facts.file.parse_ok


def test_namespaced_declarations_are_qualified(tmp_path: Path) -> None:
    source = b"""<?php
namespace App\\Domain;

interface Repo
{
    public function find(int $id): ?array;
}

trait Helper
{
    public function help(): void {}
}

enum Role: string
{
    case Admin = 'admin';

    public function label(): string
    {
        return 'x';
    }
}

abstract class Base
{
    abstract protected function run(): void;

    public static function boot(): void {}
}

function format_date(string $d): string
{
    return $d;
}
"""
    facts = parse_source(source, _record("src/Service.php"))

    assert facts.file.parse_ok
    assert [(item.qualified_name, item.kind) for item in facts.classes] == [
        ("App\\Domain\\Repo", ClassKind.INTERFACE),
        ("App\\Domain\\Helper", ClassKind.TRAIT),
        ("App\\Domain\\Role", ClassKind.ENUM),
        ("App\\Domain\\Base", ClassKind.CLASS),
    ]
    assert [item.qualified_name for item in facts.functions] == [
        "App\\Domain\\Repo::find",
        "App\\Domain\\Helper::help",
        "App\\Domain\\Role::label",
        "App\\Domain\\Base::run",
        "App\\Domain\\Base::boot",
        "App\\Domain\\format_date",
    ]
    run = facts.functions[3]
    boot = facts.functions[4]
    assert run.is_abstract
    assert run.visibility is Visibility.PROTECTED
    assert boot.is_static
    assert facts.functions[5].class_id is None


def test_syntax_errors_are_recorded_and_recovered_declarations_kept() -> None:
    facts = parse_source(b"<?php\nclass Broken {\n", _record("src/Broken.php"))

    assert not facts.file.parse_ok
    assert facts.file.parse_error is not None
    assert "line" in facts.file.parse_error
    assert [item.name for item in facts.classes] == ["Broken"]


def test_duplicate_declarations_are_collapsed() -> None:
    source = b"<?php\nfunction twice() {}\nfunction twice() {}\n"
    facts = parse_source(source, _record("src/duplicates.php"))

    # The tree is valid PHP; the duplicate is a runtime redeclaration, so the parser
    # keeps the first declaration rather than emitting two identical IR keys.
    assert facts.file.parse_ok
    assert [item.qualified_name for item in facts.functions] == ["twice"]


def test_parser_rejects_other_languages() -> None:
    with pytest.raises(ParseError, match="java"):
        PhpParser().parse(b"class User {}", _record("src/User.java", Language.JAVA))


def test_registry_resolves_php_and_rejects_unregistered_languages() -> None:
    assert isinstance(parser_for(Language.PHP), PhpParser)
    assert isinstance(parser_for("php"), PhpParser)
    with pytest.raises(LookupError, match="other"):
        parser_for(Language.OTHER)
    with pytest.raises(ParseError, match="other"):
        parse_source(b"<html></html>", _record("src/page.other", Language.OTHER))
