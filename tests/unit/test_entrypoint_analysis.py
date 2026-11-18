"""Entry-point report: handler resolution, transitive inputs and unassigned inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylens.analysis import build_entrypoints
from legacylens.ir import FactStore, FileRecord, Language
from legacylens.parsers import parse_source

_HASH = "sha256:" + "0" * 64

_BASE_SERVLET = b"""package com.example;

import javax.servlet.http.*;

public class BaseServlet extends HttpServlet {
    protected void doGet(HttpServletRequest request, HttpServletResponse response) {
        String value = read(request);
    }

    protected String read(HttpServletRequest request) {
        return request.getParameter("deep");
    }
}
"""

_CHILD_SERVLET = b"""package com.example;

import javax.servlet.annotation.WebServlet;

@WebServlet("/child")
public class ChildServlet extends BaseServlet {
}
"""

_SAVE_ACTION = b"""package com.example;

import javax.servlet.http.HttpServletRequest;

public class SaveAction {
    public String execute(HttpServletRequest request) {
        return request.getParameter("mode");
    }
}
"""

_LOOSE = b"""package com.example;

class Loose {
    void read(javax.servlet.http.HttpServletRequest request) {
        request.getParameter("orphan");
    }
}
"""

_STRUTS_CONFIG = b"""<?xml version="1.0" encoding="UTF-8"?>
<struts-config>
  <form-beans>
    <form-bean name="saveForm" type="com.example.SaveForm">
      <form-property name="role" type="java.lang.String"/>
    </form-bean>
  </form-beans>
  <action-mappings>
    <action path="/save" type="com.example.SaveAction" name="saveForm"/>
  </action-mappings>
</struts-config>
"""

_PAGE = b"""<%@ include file="inc/header.jspf" %>
<p>${param.q}</p>
"""

_HEADER = b"<header>${cookie.sid}</header>\n"


def _record(path: str) -> FileRecord:
    language = {
        ".java": Language.JAVA,
        ".xml": Language.XML,
        ".jsp": Language.JSP,
        ".jspf": Language.JSP,
    }[Path(path).suffix]
    return FileRecord(
        path=path,
        language=language,
        content_hash=_HASH,
        size_bytes=0,
    )


def _store(tmp_path: Path) -> FactStore:
    store = FactStore(tmp_path / "facts.db")
    sources = {
        "src/main/java/com/example/BaseServlet.java": _BASE_SERVLET,
        "src/main/java/com/example/ChildServlet.java": _CHILD_SERVLET,
        "src/main/java/com/example/SaveAction.java": _SAVE_ACTION,
        "src/main/java/com/example/Loose.java": _LOOSE,
        "src/main/resources/struts-config.xml": _STRUTS_CONFIG,
        "src/main/webapp/page.jsp": _PAGE,
        "src/main/webapp/inc/header.jspf": _HEADER,
    }
    for path, source in sources.items():
        store.replace_file(parse_source(source, _record(path)))
    return store


def _find(report, path: str):
    return next(item for item in report.entries if item.entry.path == path)


def test_servlet_handler_resolves_through_the_class_lineage_and_calls(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        report = build_entrypoints(store)

    entry = _find(report, "/child")
    assert entry.entry.kind.value == "servlet"
    assert [item.rsplit("#", 1)[-1] for item in entry.handlers] == [
        "com.example.BaseServlet::doGet"
    ]
    assert [(item.kind.value, item.name) for item in entry.inputs] == [("request", "deep")]


def test_struts_action_gets_handler_inputs_and_config_form_properties(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        report = build_entrypoints(store)

    entry = _find(report, "/save")
    assert [item.rsplit("#", 1)[-1] for item in entry.handlers] == [
        "com.example.SaveAction::execute"
    ]
    assert [(item.kind.value, item.name) for item in entry.inputs] == [
        ("request", "mode"),
        ("form", "role"),
    ]


def test_jsp_page_inputs_include_included_fragment_reads(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        report = build_entrypoints(store)

    entry = _find(report, "/page.jsp")
    assert [(item.kind.value, item.name, item.file_path) for item in entry.inputs] == [
        ("cookie", "sid", "src/main/webapp/inc/header.jspf"),
        ("request", "q", "src/main/webapp/page.jsp"),
    ]


def test_inputs_outside_any_entry_point_are_reported_explicitly(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        report = build_entrypoints(store)

    assert [(item.kind.value, item.name) for item in report.unassigned_inputs] == [
        ("request", "orphan")
    ]
    assert report.input_total == 6


def test_report_dict_is_deterministic_and_json_ready(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        first = build_entrypoints(store).as_dict()
        second = build_entrypoints(store).as_dict()

    assert first == second
    assert json.loads(json.dumps(first, sort_keys=True)) == first
    assert first["entry_points"] == 3
    assert first["request_inputs"] == 6
    assert first["entry_counts"] == {"action": 1, "jsp": 1, "servlet": 1}
    assert first["input_counts"] == {"cookie": 1, "form": 1, "request": 4}
    assert [item["path"] for item in first["entries"]] == ["/child", "/save", "/page.jsp"]


def test_negative_depths_are_rejected(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        with pytest.raises(ValueError, match="max_call_depth"):
            build_entrypoints(store, max_call_depth=-1)
        with pytest.raises(ValueError, match="max_include_depth"):
            build_entrypoints(store, max_include_depth=-1)
