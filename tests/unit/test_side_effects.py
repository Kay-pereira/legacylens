"""Side-effect classification, fact construction and reporting."""

from __future__ import annotations

import json

from legacylenscli.analysis import (
    SideEffectCandidate,
    SideEffectSite,
    build_side_effect_report_from_records,
    build_side_effects,
    classify_java_constructor,
    classify_java_method,
    classify_jsp_process,
    classify_jsp_session_write,
    classify_php_function_call,
    classify_php_session_write,
    classify_php_shell_command,
    resolve_java_type,
)
from legacylenscli.ir import (
    Confidence,
    FileRecord,
    Language,
    SideEffectKind,
    SideEffectRecord,
)
from legacylenscli.parsers import parse_source

HASH = "sha256:" + "0" * 64


def _record(path: str, language: Language) -> FileRecord:
    return FileRecord(path=path, language=language, content_hash=HASH, size_bytes=0)


def _php(source: bytes):
    return parse_source(source, _record("public/inc/helpers.php", Language.PHP))


def _java(source: bytes):
    return parse_source(source, _record("src/main/java/com/example/A.java", Language.JAVA))


def _jsp(source: bytes):
    return parse_source(
        source,
        _record("src/main/webapp/users/list.jsp", Language.JSP),
    )


# -- PHP classification -----------------------------------------------------


def test_php_builtins_cover_file_email_network_and_process() -> None:
    filesystem = classify_php_function_call("file_put_contents", ["'/tmp/a.csv'", "$rows"])
    assert filesystem is not None
    assert (filesystem.kind, filesystem.sink, filesystem.detail) == (
        SideEffectKind.FILESYSTEM,
        "file_put_contents",
        "file write /tmp/a.csv",
    )

    email = classify_php_function_call("mail", ["'ops@example.com'", "'report'", "$body"])
    assert email is not None
    assert (email.kind, email.detail) == (SideEffectKind.EMAIL, "email send ops@example.com")

    network = classify_php_function_call("curl_exec", ["$handle"])
    assert network is not None
    assert (network.kind, network.detail) == (SideEffectKind.HTTP, "http request")

    process = classify_php_function_call("exec", ["'ls ' . $dir"])
    assert process is not None
    assert (process.kind, process.detail) == (SideEffectKind.PROCESS, "process run")

    assert classify_php_function_call("json_encode", ["$rows"]) is None


def test_php_file_calls_become_network_effects_for_literal_urls() -> None:
    remote = classify_php_function_call("file_get_contents", ["'https://example.com/a'"])
    assert remote is not None
    assert (remote.kind, remote.detail) == (
        SideEffectKind.HTTP,
        "http request https://example.com/a",
    )

    local = classify_php_function_call("file_get_contents", ["'/tmp/a'", "true"])
    assert local is not None
    assert (local.kind, local.detail) == (SideEffectKind.FILESYSTEM, "file read /tmp/a")

    dynamic = classify_php_function_call("fopen", ["$url", "'r'"])
    assert dynamic is not None
    assert (dynamic.kind, dynamic.detail) == (SideEffectKind.FILESYSTEM, "file open")


def test_php_session_writes_and_backticks_are_explicit() -> None:
    write = classify_php_session_write("$_SESSION['cart'][]")
    assert (write.kind, write.sink, write.detail) == (
        SideEffectKind.SESSION,
        "$_SESSION",
        "session write $_SESSION['cart'][]",
    )

    unset = classify_php_session_write("$_SESSION['cart']", unset=True)
    assert unset.detail == "session unset $_SESSION['cart']"

    literal = classify_php_shell_command("`ls -la`")
    assert (literal.kind, literal.sink, literal.detail) == (
        SideEffectKind.PROCESS,
        "``",
        "process run ls -la",
    )

    dynamic = classify_php_shell_command("`ls $dir`")
    assert dynamic.detail == "process run"


def test_php_parser_attributes_effects_and_ignores_shadowed_builtins() -> None:
    facts = _php(
        b"""<?php
function mail($to) { return $to; }
function run($dir) {
    file_put_contents('/tmp/a.csv', $dir);
    mail('ops@example.com', 'x', $dir);
    exec('ls ' . $dir);
    $_SESSION['last'] = $dir;
}
$_SESSION['top'] = 1;
"""
    )

    assert [
        (item.line, item.kind, item.sink, item.detail, item.function_id)
        for item in facts.side_effects
    ] == [
        (
            4,
            SideEffectKind.FILESYSTEM,
            "file_put_contents",
            "file write /tmp/a.csv",
            "fn:public/inc/helpers.php#run",
        ),
        (6, SideEffectKind.PROCESS, "exec", "process run", "fn:public/inc/helpers.php#run"),
        (
            7,
            SideEffectKind.SESSION,
            "$_SESSION",
            "session write $_SESSION['last']",
            "fn:public/inc/helpers.php#run",
        ),
        (9, SideEffectKind.SESSION, "$_SESSION", "session write $_SESSION['top']", None),
    ]
    assert all(item.confidence is Confidence.VERIFIED for item in facts.side_effects)


# -- Java classification ----------------------------------------------------


def test_java_receiver_types_need_source_evidence() -> None:
    assert classify_java_method("File", "delete") is None
    assert resolve_java_type("File") is None

    direct = classify_java_method("File", "delete", imports=("java.io.File",))
    assert direct is not None
    assert (direct.kind, direct.sink, direct.detail) == (
        SideEffectKind.FILESYSTEM,
        "File.delete",
        "file delete",
    )

    wildcard = classify_java_method("File", "delete", imports=("java.io.*",))
    assert wildcard is not None and wildcard.sink == "File.delete"

    qualified = classify_java_method("java.io.File", "delete", imports=())
    assert qualified is not None and qualified.kind is SideEffectKind.FILESYSTEM

    assert resolve_java_type("Files", ("java.nio.file.Files",)) == "java.nio.file.Files"
    assert resolve_java_type("Foo", ("com.example.Foo",)) is None


def test_java_methods_cover_email_network_process_and_session() -> None:
    email = classify_java_method(
        "Transport",
        "send",
        ["message"],
        imports=("javax.mail.Transport",),
    )
    assert email is not None
    assert (email.kind, email.sink) == (SideEffectKind.EMAIL, "Transport.send")

    network = classify_java_method(
        "URL",
        "openConnection",
        imports=("java.net.URL",),
    )
    assert network is not None
    assert (network.kind, network.sink) == (SideEffectKind.HTTP, "URL.openConnection")

    process = classify_java_method(
        None,
        "exec",
        ['"ls"'],
        receiver_expression="Runtime.getRuntime()",
    )
    assert process is not None
    assert (process.kind, process.sink, process.detail) == (
        SideEffectKind.PROCESS,
        "Runtime.exec",
        "process run ls",
    )

    session = classify_java_method(
        "HttpSession",
        "setAttribute",
        ['"uid"', "1"],
        imports=("javax.servlet.http.HttpSession",),
    )
    assert session is not None
    assert (session.kind, session.sink, session.detail) == (
        SideEffectKind.SESSION,
        "HttpSession.setAttribute",
        "session write uid",
    )

    invalidate = classify_java_method(
        "HttpSession",
        "invalidate",
        imports=("javax.servlet.http.HttpSession",),
    )
    assert invalidate is not None and invalidate.detail == "session invalidate"


def test_java_chained_session_calls_require_a_request_receiver() -> None:
    chained = classify_java_method(
        None,
        "setAttribute",
        ['"uid"', "1"],
        receiver_expression="request.getSession()",
        request_receiver=True,
    )
    assert chained is not None and chained.kind is SideEffectKind.SESSION

    assert (
        classify_java_method(
            None,
            "setAttribute",
            ['"uid"', "1"],
            receiver_expression="thing.getSession()",
        )
        is None
    )


def test_java_constructors_are_sinks_only_for_known_types() -> None:
    writer = classify_java_constructor("FileWriter", imports=("java.io.FileWriter",))
    assert writer is not None
    assert (writer.kind, writer.sink, writer.detail) == (
        SideEffectKind.FILESYSTEM,
        "new FileWriter",
        "file write",
    )

    socket = classify_java_constructor("Socket", imports=("java.net.Socket",))
    assert socket is not None
    assert (socket.kind, socket.sink, socket.detail) == (
        SideEffectKind.HTTP,
        "new Socket",
        "socket open",
    )

    assert classify_java_constructor("StringBuilder", imports=()) is None
    assert classify_java_constructor("Files", imports=("java.nio.file.Files",)) is None


def test_java_parser_keeps_effects_inside_the_disambiguated_function() -> None:
    facts = _java(
        b"""package p;
import java.io.FileWriter;
import java.nio.file.Files;
import java.util.List;
import javax.mail.Transport;
import javax.servlet.http.HttpSession;
class A {
    void save(List<String> rows, HttpSession session) throws Exception {
        new FileWriter("reports/out.csv").write(rows.toString());
        Transport.send(null);
        Runtime.getRuntime().exec("ls");
        new ProcessBuilder("gzip", "out.csv").start();
        session.setAttribute("last", "x");
    }
    void ignore(Widget Files) throws Exception {
        Files.write(null, null);
    }
}
"""
    )

    function = "fn:src/main/java/com/example/A.java#p.A::save"
    assert [(item.line, item.kind, item.sink, item.detail) for item in facts.side_effects] == [
        (9, SideEffectKind.FILESYSTEM, "new FileWriter", "file write"),
        (10, SideEffectKind.EMAIL, "Transport.send", "email send"),
        (11, SideEffectKind.PROCESS, "Runtime.exec", "process run ls"),
        (12, SideEffectKind.PROCESS, "ProcessBuilder.start", "process run"),
        (13, SideEffectKind.SESSION, "HttpSession.setAttribute", "session write last"),
    ]
    assert {item.function_id for item in facts.side_effects} == {function}


# -- JSP --------------------------------------------------------------------


def test_jsp_scriptlets_report_session_writes_and_process_execution() -> None:
    facts = _jsp(
        b"""<html><body>
<%
  session.setAttribute("page", page);
  request.getSession().removeAttribute("draft");
  Runtime.getRuntime().exec("ls");
  String decoy = "session.setAttribute(\\"nope\\", 1)";
  // Runtime.getRuntime().exec("nope");
%>
</body></html>
"""
    )

    assert [
        (item.line, item.kind, item.sink, item.detail, item.confidence)
        for item in facts.side_effects
    ] == [
        (
            3,
            SideEffectKind.SESSION,
            "HttpSession.setAttribute",
            "session write page",
            Confidence.VERIFIED,
        ),
        (
            4,
            SideEffectKind.SESSION,
            "HttpSession.removeAttribute",
            "session remove draft",
            Confidence.INFERRED,
        ),
        (
            5,
            SideEffectKind.PROCESS,
            "Runtime.getRuntime().exec",
            "process run ls",
            Confidence.VERIFIED,
        ),
    ]
    assert classify_jsp_session_write("getAttribute") is None
    assert classify_jsp_process().detail == "process run"


# -- fact construction and reporting ---------------------------------------


def test_build_side_effects_deduplicates_and_sorts_by_line_and_kind() -> None:
    filesystem = SideEffectCandidate(SideEffectKind.FILESYSTEM, "unlink", "file delete")
    session = SideEffectCandidate(SideEffectKind.SESSION, "$_SESSION", "session write")
    sites = [
        SideEffectSite(session, 9),
        SideEffectSite(filesystem, 3, "fn:a.php#f"),
        SideEffectSite(filesystem, 3, "fn:a.php#f"),
    ]

    records = build_side_effects("a.php", sites)

    assert [(item.line, item.kind) for item in records] == [
        (3, SideEffectKind.FILESYSTEM),
        (9, SideEffectKind.SESSION),
    ]
    assert records[0].file_path == "a.php"
    assert records[0].function_id == "fn:a.php#f"


def _record_for(
    kind: SideEffectKind,
    *,
    sink: str,
    detail: str,
    line: int,
    function: str | None,
    file_path: str = "app.php",
    confidence: Confidence = Confidence.VERIFIED,
) -> SideEffectRecord:
    return SideEffectRecord(
        file_path=file_path,
        kind=kind,
        detail=detail,
        line=line,
        function_id=function,
        sink=sink,
        confidence=confidence,
    )


def test_report_groups_by_kind_and_lists_unattributed_effects() -> None:
    records = [
        _record_for(
            SideEffectKind.SESSION,
            sink="$_SESSION",
            detail="session write $_SESSION['uid']",
            line=7,
            function="fn:app.php#login",
        ),
        _record_for(
            SideEffectKind.FILESYSTEM,
            sink="unlink",
            detail="file delete uploads/a.png",
            line=4,
            function=None,
            file_path="public/cleanup.php",
        ),
        _record_for(
            SideEffectKind.EMAIL,
            sink="mail",
            detail="email send ops@example.com",
            line=5,
            function="fn:app.php#notify",
        ),
    ]

    report = build_side_effect_report_from_records(records)

    assert [group.kind for group in report.groups] == [
        SideEffectKind.FILESYSTEM,
        SideEffectKind.EMAIL,
        SideEffectKind.SESSION,
    ]
    assert report.total == 3
    assert [item.detail for item in report.unattributed] == ["file delete uploads/a.png"]
    assert report.groups[0].files == ("public/cleanup.php",)
    assert report.groups[2].functions == ("fn:app.php#login",)

    data = report.as_dict()
    assert data["side_effects"] == 3
    assert data["kinds"] == {"filesystem": 1, "email": 1, "session": 1}
    assert data["sinks"] == {"$_SESSION": 1, "mail": 1, "unlink": 1}
    assert data["groups"][1]["records"] == [
        {
            "file_path": "app.php",
            "line": 5,
            "kind": "email",
            "sink": "mail",
            "detail": "email send ops@example.com",
            "function": "fn:app.php#notify",
            "confidence": "verified",
        }
    ]
    assert report.unattributed[0].function_id is None
    assert json.loads(json.dumps(data)) == data
