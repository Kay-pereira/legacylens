"""Java entry points and request inputs: servlet annotations, request APIs and forms."""

from __future__ import annotations

from legacylenscli.ir import (
    Confidence,
    EntryKind,
    FileRecord,
    Language,
    RequestInputKind,
)
from legacylenscli.parsers import parse_source

JAVA = "sha256:" + "0" * 64


def _record(path: str = "src/main/java/com/example/UserServlet.java") -> FileRecord:
    return FileRecord(
        path=path,
        language=Language.JAVA,
        content_hash=JAVA,
        size_bytes=0,
    )


def test_web_servlet_annotation_maps_patterns_to_the_class() -> None:
    source = b"""package com.example;

import javax.servlet.http.*;
import javax.servlet.annotation.WebServlet;

@WebServlet(name = "user", urlPatterns = {"/users", "/users/*"})
public class UserServlet extends HttpServlet {
    protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    }
}
"""
    facts = parse_source(source, _record())

    assert [
        (item.kind, item.path, item.line, item.handler_ref, item.framework)
        for item in facts.entry_points
    ] == [
        (EntryKind.SERVLET, "/users", 6, "com.example.UserServlet", "servlet"),
        (EntryKind.SERVLET, "/users/*", 6, "com.example.UserServlet", "servlet"),
    ]


def test_web_filter_annotation_and_shorthand_value() -> None:
    source = b"""package com.example;

import javax.servlet.annotation.WebFilter;

@WebFilter("/secure/*")
public class AuthFilter {
    public void doFilter(javax.servlet.ServletRequest req) {
    }
}
"""
    facts = parse_source(source, _record("src/main/java/com/example/AuthFilter.java"))

    assert [(item.kind, item.path, item.handler_ref) for item in facts.entry_points] == [
        (EntryKind.FILTER, "/secure/*", "com.example.AuthFilter")
    ]


def test_servlet_request_api_inputs_are_typed_and_dynamic_cases_are_explicit() -> None:
    source = b"""package com.example;

import javax.servlet.http.*;

public class UserServlet extends HttpServlet {
    protected void doGet(HttpServletRequest request, HttpServletResponse response) {
        String id = request.getParameter("id");
        String[] tags = request.getParameterValues("tags");
        String token = request.getHeader("X-Token");
        Cookie[] cookies = request.getCookies();
        String path = request.getRequestURI();
        String query = request.getQueryString();
        java.io.InputStream body = request.getInputStream();
        Part part = request.getPart("upload");
        String other = thing.getParameter("not-request");
    }

    protected void doPost(ServletRequest req, ServletResponse response) {
        String name = req.getParameter("name");
    }
}
"""
    facts = parse_source(source, _record())
    do_get, do_post = facts.functions

    assert [
        (item.kind, item.name, item.line, item.function_id, item.confidence)
        for item in facts.request_inputs
    ] == [
        (RequestInputKind.REQUEST, "id", 7, do_get.id, Confidence.VERIFIED),
        (RequestInputKind.REQUEST, "tags", 8, do_get.id, Confidence.VERIFIED),
        (RequestInputKind.HEADER, "x-token", 9, do_get.id, Confidence.VERIFIED),
        (RequestInputKind.COOKIE, "<dynamic>", 10, do_get.id, Confidence.INFERRED),
        (RequestInputKind.PATH, "<dynamic>", 11, do_get.id, Confidence.INFERRED),
        (RequestInputKind.QUERY, "<dynamic>", 12, do_get.id, Confidence.INFERRED),
        (RequestInputKind.BODY, "<dynamic>", 13, do_get.id, Confidence.INFERRED),
        (RequestInputKind.FILE, "upload", 14, do_get.id, Confidence.VERIFIED),
        (RequestInputKind.REQUEST, "name", 19, do_post.id, Confidence.VERIFIED),
    ]


def test_struts_form_fields_become_inferred_inputs() -> None:
    source = b"""package com.example;

import org.apache.struts.action.ActionForm;

public class UserForm extends ActionForm {
    private static final long serialVersionUID = 1L;
    private String name;
    protected int age;
}
"""
    facts = parse_source(source, _record("src/main/java/com/example/UserForm.java"))

    assert [
        (item.kind, item.name, item.line, item.framework, item.confidence)
        for item in facts.request_inputs
    ] == [
        (
            RequestInputKind.FORM,
            "name",
            7,
            "struts",
            Confidence.INFERRED,
        ),
        (
            RequestInputKind.FORM,
            "age",
            8,
            "struts",
            Confidence.INFERRED,
        ),
    ]


def test_static_and_untyped_receivers_do_not_produce_inputs() -> None:
    source = b"""package com.example;

class Cache {
    static String value;

    String read(Unknown thing) {
        return thing.getParameter("id");
    }
}
"""
    facts = parse_source(source, _record("src/main/java/com/example/Cache.java"))

    assert facts.request_inputs == ()
