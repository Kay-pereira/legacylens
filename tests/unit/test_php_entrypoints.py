"""PHP entry points and request inputs: pages, superglobals and filter_input."""

from __future__ import annotations

from legacylens.ir import (
    Confidence,
    EntryKind,
    FileRecord,
    Language,
    RequestInputKind,
)
from legacylens.parsers import parse_source

PY = "sha256:" + "0" * 64


def _record(path: str) -> FileRecord:
    return FileRecord(
        path=path,
        language=Language.PHP,
        content_hash=PY,
        size_bytes=0,
    )


def _inputs(source: bytes, path: str = "public/page.php"):
    return parse_source(source, _record(path)).request_inputs


def test_page_script_is_a_route_with_superglobal_inputs() -> None:
    source = b"""<?php
$id = $_GET['id'];
$user = $_POST['user']['name'];
$key = $_REQUEST[$k];
$sid = $_COOKIE['sid'];
$upload = $_FILES['doc'];
$token = $_SERVER['HTTP_X_TOKEN'];
$length = $_SERVER['CONTENT_LENGTH'];
$remote = $_SERVER['REMOTE_ADDR'];
$email = filter_input(INPUT_POST, 'email');
$cookies = filter_input_array(INPUT_COOKIE);
"""
    facts = parse_source(source, _record("public/users.php"))

    assert len(facts.entry_points) == 1
    entry = facts.entry_points[0]
    assert entry.kind is EntryKind.ROUTE
    assert entry.path == "/users.php"
    assert entry.line == 2
    assert entry.confidence is Confidence.INFERRED

    assert [
        (item.kind, item.name, item.line, item.confidence) for item in facts.request_inputs
    ] == [
        (RequestInputKind.QUERY, "id", 2, Confidence.VERIFIED),
        (RequestInputKind.FORM, "user", 3, Confidence.VERIFIED),
        (RequestInputKind.REQUEST, "<dynamic>", 4, Confidence.INFERRED),
        (RequestInputKind.COOKIE, "sid", 5, Confidence.VERIFIED),
        (RequestInputKind.FILE, "doc", 6, Confidence.VERIFIED),
        (RequestInputKind.HEADER, "x-token", 7, Confidence.VERIFIED),
        (RequestInputKind.HEADER, "content-length", 8, Confidence.VERIFIED),
        (RequestInputKind.FORM, "email", 10, Confidence.VERIFIED),
        (RequestInputKind.COOKIE, "<dynamic>", 11, Confidence.INFERRED),
    ]


def test_declarations_and_include_only_files_are_not_pages() -> None:
    source = b"""<?php
namespace App;

require_once 'lib.php';
use Foo\\Bar;

function helper(): void {}

class Service
{
    public function run(): void {}
}
"""
    facts = parse_source(source, _record("src/Service.php"))

    assert facts.entry_points == ()
    assert {item.qualified_name for item in facts.functions} == {
        "App\\Service::run",
        "App\\helper",
    }


def test_shebang_marks_a_cli_script() -> None:
    source = b"""#!/usr/bin/env php
<?php
$format = $_GET['format'] ?? 'csv';
echo $format;
"""
    facts = parse_source(source, _record("bin/export.php"))

    assert [(item.kind, item.path) for item in facts.entry_points] == [
        (EntryKind.CLI, "bin/export.php")
    ]
    assert [(item.kind, item.name) for item in facts.request_inputs] == [
        (RequestInputKind.QUERY, "format")
    ]


def test_function_inputs_are_attributed_to_the_inner_function() -> None:
    source = b"""<?php
function read_id(): string
{
    return $_GET['id'];
}

class Reader
{
    public function read(): string
    {
        return $_POST['name'];
    }
}
"""
    facts = parse_source(source, _record("src/Reader.php"))
    functions = {item.qualified_name: item.id for item in facts.functions}

    assert [(item.kind, item.name, item.function_id) for item in facts.request_inputs] == [
        (RequestInputKind.QUERY, "id", functions["read_id"]),
        (RequestInputKind.FORM, "name", functions["Reader::read"]),
    ]


def test_dynamic_superglobal_key_stays_explicit() -> None:
    facts = _inputs(b"<?php\n$value = $_GET[$key];\necho $value;\n")

    assert len(facts) == 1
    assert facts[0].name == "<dynamic>"
    assert facts[0].raw == "$_GET[$key]"
    assert facts[0].confidence is Confidence.INFERRED


def test_plain_session_and_unrelated_server_keys_are_not_request_inputs() -> None:
    source = b"""<?php
$a = $_SESSION['uid'];
$b = $_SERVER['DOCUMENT_ROOT'];
$c = $_SERVER['HTTP_ACCEPT'];
"""
    inputs = _inputs(source)

    assert [(item.kind, item.name) for item in inputs] == [(RequestInputKind.HEADER, "accept")]
