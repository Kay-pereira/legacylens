"""End-to-end: scan a mixed PHP/Java/JSP app and report side effects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylens.analysis import build_side_effect_report
from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import Confidence, FactStore, SideEffectKind
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
        return store.list_side_effects(), build_side_effect_report(store)


def test_every_kind_is_reported_with_function_attribution(tmp_path: Path) -> None:
    records, report = _report(tmp_path)

    assert report.total == len(records) == 15
    assert {group.kind: len(group.records) for group in report.groups} == {
        SideEffectKind.FILESYSTEM: 3,
        SideEffectKind.HTTP: 1,
        SideEffectKind.EMAIL: 2,
        SideEffectKind.PROCESS: 3,
        SideEffectKind.SESSION: 6,
    }

    export = [
        item for item in records if item.function_id == "fn:public/inc/helpers.php#export_rows"
    ]
    assert [(item.kind, item.sink, item.detail) for item in export] == [
        (SideEffectKind.FILESYSTEM, "file_put_contents", "file write"),
        (SideEffectKind.EMAIL, "mail", "email send ops@example.com"),
        (SideEffectKind.HTTP, "file_get_contents", "http request https://example.com/notify"),
        (SideEffectKind.SESSION, "$_SESSION", "session write $_SESSION['last_export']"),
    ]

    purge = [
        item for item in records if item.function_id == "fn:public/inc/helpers.php#purge_export"
    ]
    assert [(item.kind, item.sink, item.detail) for item in purge] == [
        (SideEffectKind.FILESYSTEM, "unlink", "file delete"),
        (SideEffectKind.SESSION, "$_SESSION", "session unset $_SESSION['last_export']"),
    ]


def test_java_effects_use_the_disambiguated_function_and_typed_receivers(tmp_path: Path) -> None:
    records, _ = _report(tmp_path)

    archive = [
        item
        for item in records
        if item.function_id == "fn:src/main/java/com/example/SaveUserAction.java"
        "#com.example.SaveUserAction::archiveUsers"
    ]
    assert [(item.kind, item.sink, item.detail) for item in archive] == [
        (SideEffectKind.FILESYSTEM, "new FileWriter", "file write"),
        (SideEffectKind.EMAIL, "Transport.send", "email send"),
        (SideEffectKind.PROCESS, "Runtime.exec", "process run ls reports"),
        (SideEffectKind.PROCESS, "ProcessBuilder.start", "process run"),
        (SideEffectKind.SESSION, "HttpSession.setAttribute", "session write lastExport"),
        (SideEffectKind.SESSION, "HttpSession.removeAttribute", "session remove draft"),
    ]
    assert {item.confidence for item in archive} == {Confidence.VERIFIED}


def test_jsp_scriptlets_are_file_level_effects(tmp_path: Path) -> None:
    _, report = _report(tmp_path)

    assert [(item.line, item.kind, item.sink, item.confidence) for item in report.unattributed] == [
        (16, SideEffectKind.SESSION, "HttpSession.setAttribute", Confidence.VERIFIED),
        (17, SideEffectKind.SESSION, "HttpSession.removeAttribute", Confidence.INFERRED),
        (18, SideEffectKind.PROCESS, "Runtime.getRuntime().exec", Confidence.VERIFIED),
    ]
    assert {item.file_path for item in report.unattributed} == {"src/main/webapp/users/list.jsp"}


def test_report_is_json_ready_and_filterable(tmp_path: Path) -> None:
    records, report = _report(tmp_path)

    assert len([item for item in records if item.kind is SideEffectKind.PROCESS]) == 3
    assert report.groups[-1].kind is SideEffectKind.PROCESS

    data = report.as_dict()
    assert data["side_effects"] == 15
    assert data["kinds"] == {
        "filesystem": 3,
        "http": 1,
        "email": 2,
        "process": 3,
        "session": 6,
    }
    assert json.loads(json.dumps(data)) == data
