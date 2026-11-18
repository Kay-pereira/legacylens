"""End-to-end: scan a legacy web app, parse it, and report entry-point inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylens.analysis import build_entrypoints
from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import EntryKind, FactStore, RequestInputKind
from legacylens.parsers import parse_source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WEB_APP = FIXTURES / "legacy_web"


def _report(tmp_path: Path):
    scan = scan_repository(WEB_APP, ScanOptions())
    assert len(scan.files) == 18

    with FactStore(tmp_path / "facts.db") as store:
        for scanned in scan.files:
            store.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
        return build_entrypoints(store)


def _entry(report, kind: EntryKind, path: str, file_path: str):
    return next(
        detail
        for detail in report.entries
        if detail.entry.kind is kind
        and detail.entry.path == path
        and detail.entry.file_path == file_path
    )


def test_php_pages_and_cli_scripts_get_superglobal_inputs(tmp_path: Path) -> None:
    report = _report(tmp_path)

    page = _entry(report, EntryKind.ROUTE, "/index.php", "public/index.php")
    assert {(item.kind.value, item.name) for item in page.inputs} == {
        ("cookie", "uid"),
        ("form", "term"),
        ("query", "q"),
        ("query", "page"),
    }

    cli = _entry(report, EntryKind.CLI, "public/export.php", "public/export.php")
    assert [(item.kind, item.name) for item in cli.inputs] == [(RequestInputKind.QUERY, "format")]


def test_servlet_annotations_and_web_xml_both_resolve_handlers(tmp_path: Path) -> None:
    report = _report(tmp_path)

    for file_path in (
        "src/main/java/com/example/UserServlet.java",
        "src/main/webapp/WEB-INF/web.xml",
    ):
        servlet = _entry(
            report,
            EntryKind.SERVLET,
            "/api/users/*" if file_path.endswith(".java") else "/users/*",
            file_path,
        )
        assert [item.rsplit("#", 1)[-1] for item in servlet.handlers] == [
            "com.example.UserServlet::doGet",
            "com.example.UserServlet::doPost",
        ]
        assert {(item.kind.value, item.name) for item in servlet.inputs} == {
            ("request", "id"),
            ("header", "x-sort"),
            ("request", "name"),
            ("file", "avatar"),
        }


def test_filter_entries_use_do_filter_and_header_inputs(tmp_path: Path) -> None:
    report = _report(tmp_path)

    for file_path in (
        "src/main/java/com/example/AuthFilter.java",
        "src/main/webapp/WEB-INF/web.xml",
    ):
        auth = _entry(report, EntryKind.FILTER, "/secure/*", file_path)
        assert [item.rsplit("#", 1)[-1] for item in auth.handlers] == [
            "com.example.AuthFilter::doFilter"
        ]
        assert [(item.kind.value, item.name) for item in auth.inputs] == [("header", "x-token")]


def test_struts_1_and_2_actions_resolve_their_configured_handlers(tmp_path: Path) -> None:
    report = _report(tmp_path)

    struts1 = _entry(
        report,
        EntryKind.ACTION,
        "/user/save",
        "src/main/resources/struts-config.xml",
    )
    assert [item.rsplit("#", 1)[-1] for item in struts1.handlers] == [
        "com.example.SaveUserAction::execute"
    ]
    assert {(item.kind.value, item.name) for item in struts1.inputs} == {
        ("request", "mode"),
        ("form", "role"),
    }

    struts2 = _entry(
        report,
        EntryKind.ACTION,
        "/widgets/save",
        "src/main/resources/struts.xml",
    )
    assert [item.rsplit("#", 1)[-1] for item in struts2.handlers] == [
        "com.example.WidgetAction::save"
    ]
    assert [(item.kind.value, item.name) for item in struts2.inputs] == [("request", "widget")]


def test_jsp_pages_include_el_scriptlet_form_and_fragment_inputs(tmp_path: Path) -> None:
    report = _report(tmp_path)

    page = _entry(report, EntryKind.JSP, "/users/list.jsp", "src/main/webapp/users/list.jsp")
    assert {(item.kind.value, item.name) for item in page.inputs} == {
        ("cookie", "sid"),
        ("request", "q"),
        ("request", "id"),
        ("form", "name"),
        ("file", "avatar"),
        ("form", "role"),
        ("header", "x-token"),
        ("request", "page"),
        ("request", "<dynamic>"),
    }

    secret = _entry(
        report,
        EntryKind.JSP,
        "/admin/secret",
        "src/main/webapp/WEB-INF/web.xml",
    )
    assert [(item.kind.value, item.name, item.file_path) for item in secret.inputs] == [
        ("request", "token", "src/main/webapp/WEB-INF/views/secret.jsp")
    ]

    jsp_paths = {
        detail.entry.file_path for detail in report.entries if detail.entry.kind is EntryKind.JSP
    }
    assert "src/main/webapp/WEB-INF/views/secret.jsp" not in jsp_paths
    assert "src/main/webapp/inc/header.jspf" not in jsp_paths
    assert "src/main/webapp/WEB-INF/tags/widget.tag" not in jsp_paths


def test_unassigned_inputs_and_summary_are_explicit(tmp_path: Path) -> None:
    report = _report(tmp_path)

    assert {(item.kind.value, item.name, item.file_path) for item in report.unassigned_inputs} == {
        ("query", "campaign", "public/inc/analytics.php"),
        ("form", "name", "src/main/java/com/example/UserForm.java"),
        ("form", "email", "src/main/java/com/example/UserForm.java"),
    }

    summary = report.as_dict()
    assert summary["entry_points"] == len(report.entries)
    assert summary["request_inputs"] == 27
    assert summary["entry_counts"] == {
        "action": 2,
        "cli": 1,
        "filter": 2,
        "jsp": 2,
        "route": 1,
        "servlet": 2,
    }
    assert json.loads(json.dumps(summary)) == summary
