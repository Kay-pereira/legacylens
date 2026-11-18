"""JSP front-end: page entry points, includes, EL/scriptlet inputs and form fields."""

from __future__ import annotations

from legacylens.ir import (
    Confidence,
    EdgeKind,
    EntryKind,
    FileRecord,
    Language,
    RequestInputKind,
)
from legacylens.parsers import parse_source

JSP = "sha256:" + "0" * 64


def _record(path: str) -> FileRecord:
    return FileRecord(
        path=path,
        language=Language.JSP,
        content_hash=JSP,
        size_bytes=0,
    )


def _parse(source: bytes, path: str = "src/main/webapp/users/list.jsp"):
    return parse_source(source, _record(path))


def test_page_is_an_entry_point_but_web_inf_and_fragments_are_not() -> None:
    page = _parse(b"<html><body>${param.id}</body></html>")
    assert [(item.kind, item.path, item.line) for item in page.entry_points] == [
        (EntryKind.JSP, "/users/list.jsp", 1)
    ]

    hidden = _parse(b"${param.id}", "src/main/webapp/WEB-INF/views/secret.jsp")
    assert hidden.entry_points == ()

    for fragment in (
        "src/main/webapp/inc/header.jspf",
        "src/main/webapp/WEB-INF/tags/widget.tag",
        "src/main/webapp/WEB-INF/tags/widget.tagx",
    ):
        assert _parse(b"${param.id}", fragment).entry_points == ()


def test_el_scriptlet_and_form_inputs_are_collected_and_deduplicated() -> None:
    source = b"""<%@ page contentType="text/html; charset=UTF-8" %>
<html>
<body>
<form action="/users/save">
  <input type="hidden" name="id" value="${param.id}">
  <input type="text" name="name">
  <input type="file" name="avatar">
  <input type="submit" name="go" value="Save">
  <select name="role"><option>admin</option></select>
</form>
<p>${header['X-Token']} ${cookie.sid} ${sessionScope.user}</p>
<%
  String page = request.getParameter("page");
  String dynamic = request.getParameter(key);
  String agent = request.getHeader("User-Agent");
  Cookie[] cookies = request.getCookies();
%>
</body>
</html>
"""
    facts = _parse(source)

    assert [
        (item.kind, item.name, item.line, item.confidence) for item in facts.request_inputs
    ] == [
        (RequestInputKind.REQUEST, "id", 5, Confidence.VERIFIED),
        (RequestInputKind.FORM, "name", 6, Confidence.INFERRED),
        (RequestInputKind.FILE, "avatar", 7, Confidence.INFERRED),
        (RequestInputKind.FORM, "role", 9, Confidence.INFERRED),
        (RequestInputKind.COOKIE, "sid", 11, Confidence.VERIFIED),
        (RequestInputKind.HEADER, "x-token", 11, Confidence.VERIFIED),
        (RequestInputKind.REQUEST, "page", 13, Confidence.VERIFIED),
        (RequestInputKind.REQUEST, "<dynamic>", 14, Confidence.INFERRED),
        (RequestInputKind.HEADER, "user-agent", 15, Confidence.VERIFIED),
        (RequestInputKind.COOKIE, "<dynamic>", 16, Confidence.INFERRED),
    ]


def test_comments_do_not_produce_inputs_or_form_fields() -> None:
    source = b"""<%-- <input name="hidden"> ${param.secret} --%>
<!-- <input name="alsoHidden"> ${header.X-Other} -->
<p>${param.visible}</p>
"""
    facts = _parse(source)

    assert [(item.kind, item.name) for item in facts.request_inputs] == [
        (RequestInputKind.REQUEST, "visible")
    ]


def test_includes_and_page_imports_become_edges() -> None:
    source = b"""<%@ page import="java.util.List, com.example.User" %>
<%@ include file="../inc/header.jspf" %>
<jsp:include page="/inc/footer.jspf" />
<jsp:include page="<%= target %>" />
"""
    facts = _parse(source)

    assert [
        (item.kind, item.dst, item.line, item.resolved, item.confidence) for item in facts.edges
    ] == [
        (EdgeKind.IMPORT, "com.example.User", 1, False, Confidence.VERIFIED),
        (EdgeKind.IMPORT, "java.util.List", 1, False, Confidence.VERIFIED),
        (
            EdgeKind.INCLUDE,
            "src/main/webapp/inc/header.jspf",
            2,
            True,
            Confidence.VERIFIED,
        ),
        (EdgeKind.INCLUDE, "/inc/footer.jspf", 3, False, Confidence.INFERRED),
        (EdgeKind.INCLUDE, "<%= target %>", 4, False, Confidence.UNSUPPORTED),
    ]
