"""XML descriptors: web.xml, Struts configuration, TLDs and malformed XML."""

from __future__ import annotations

from legacylenscli.ir import (
    Confidence,
    EdgeKind,
    EntryKind,
    FileRecord,
    Language,
    RequestInputKind,
)
from legacylenscli.parsers import parse_source

XML = "sha256:" + "0" * 64


def _record(path: str) -> FileRecord:
    return FileRecord(
        path=path,
        language=Language.XML,
        content_hash=XML,
        size_bytes=0,
    )


def _parse(source: bytes, path: str = "src/main/webapp/WEB-INF/web.xml"):
    return parse_source(source, _record(path))


def test_web_xml_maps_servlets_filters_and_jsp_files() -> None:
    source = b"""<?xml version="1.0" encoding="UTF-8"?>
<web-app xmlns="http://xmlns.jcp.org/xml/ns/javaee" version="3.1">
  <servlet>
    <servlet-name>user</servlet-name>
    <servlet-class>com.example.UserServlet</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>user</servlet-name>
    <url-pattern>/users/*</url-pattern>
  </servlet-mapping>
  <servlet>
    <servlet-name>secret</servlet-name>
    <jsp-file>/WEB-INF/views/secret.jsp</jsp-file>
  </servlet>
  <servlet-mapping>
    <servlet-name>secret</servlet-name>
    <url-pattern>/admin/secret</url-pattern>
  </servlet-mapping>
  <filter>
    <filter-name>auth</filter-name>
    <filter-class>com.example.AuthFilter</filter-class>
  </filter>
  <filter-mapping>
    <filter-name>auth</filter-name>
    <url-pattern>/secure/*</url-pattern>
  </filter-mapping>
</web-app>
"""
    facts = _parse(source)

    assert facts.file.parse_ok
    assert [
        (item.kind, item.path, item.line, item.handler_ref, item.framework)
        for item in facts.entry_points
    ] == [
        (EntryKind.SERVLET, "/users/*", 7, "com.example.UserServlet", "servlet"),
        (EntryKind.JSP, "/admin/secret", 15, "/WEB-INF/views/secret.jsp", "jsp"),
        (EntryKind.FILTER, "/secure/*", 23, "com.example.AuthFilter", "servlet"),
    ]


def test_namespace_prefixed_descriptor_tags_are_understood() -> None:
    source = b"""<?xml version="1.0" encoding="UTF-8"?>
<j2ee:web-app xmlns:j2ee="http://java.sun.com/xml/ns/j2ee" version="2.4">
  <j2ee:servlet>
    <j2ee:servlet-name>legacy</j2ee:servlet-name>
    <j2ee:servlet-class>com.example.LegacyServlet</j2ee:servlet-class>
  </j2ee:servlet>
  <j2ee:servlet-mapping>
    <j2ee:servlet-name>legacy</j2ee:servlet-name>
    <j2ee:url-pattern>/legacy</j2ee:url-pattern>
  </j2ee:servlet-mapping>
</j2ee:web-app>
"""
    facts = _parse(source, "src/main/webapp/WEB-INF/legacy-web.xml")

    assert [(item.kind, item.path, item.handler_ref) for item in facts.entry_points] == [
        (EntryKind.SERVLET, "/legacy", "com.example.LegacyServlet")
    ]


def test_struts_1_actions_and_form_properties() -> None:
    source = b"""<?xml version="1.0" encoding="UTF-8"?>
<struts-config>
  <form-beans>
    <form-bean name="userForm" type="com.example.UserForm">
      <form-property name="role" type="java.lang.String"/>
    </form-bean>
  </form-beans>
  <action-mappings>
    <action path="/user/save" type="com.example.SaveUserAction" name="userForm"/>
  </action-mappings>
</struts-config>
"""
    facts = _parse(source, "src/main/resources/struts-config.xml")

    assert [(item.kind, item.path, item.handler_ref) for item in facts.entry_points] == [
        (EntryKind.ACTION, "/user/save", "com.example.SaveUserAction")
    ]
    assert [
        (item.kind, item.name, item.line, item.framework, item.confidence)
        for item in facts.request_inputs
    ] == [
        (
            RequestInputKind.FORM,
            "role",
            5,
            "struts",
            Confidence.INFERRED,
        )
    ]


def test_struts_2_action_method_and_includes() -> None:
    source = b"""<?xml version="1.0" encoding="UTF-8"?>
<struts>
  <include file="struts-extra.xml"/>
  <package name="widgets" namespace="/widgets" extends="struts-default">
    <action name="save" class="com.example.WidgetAction" method="save"/>
    <action name="list" class="com.example.WidgetAction"/>
  </package>
</struts>
"""
    facts = _parse(source, "src/main/resources/struts.xml")

    assert [(item.kind, item.path, item.line, item.handler_ref) for item in facts.entry_points] == [
        (EntryKind.ACTION, "/widgets/save", 5, "com.example.WidgetAction::save"),
        (EntryKind.ACTION, "/widgets/list", 6, "com.example.WidgetAction"),
    ]
    assert [(item.kind, item.dst, item.resolved) for item in facts.edges] == [
        (EdgeKind.INCLUDE, "src/main/resources/struts-extra.xml", True)
    ]


def test_tld_is_parsed_without_inventing_entry_points() -> None:
    source = b"""<?xml version="1.0" encoding="UTF-8"?>
<taglib xmlns="http://java.sun.com/xml/ns/j2ee" version="2.0">
  <tlib-version>1.0</tlib-version>
  <short-name>example</short-name>
  <uri>http://example.com/tags</uri>
</taglib>
"""
    facts = _parse(source, "src/main/webapp/WEB-INF/example.tld")

    assert facts.file.parse_ok
    assert facts.entry_points == ()
    assert facts.request_inputs == ()
    assert facts.edges == ()


def test_malformed_xml_records_a_parse_error_without_crashing() -> None:
    facts = _parse(b"<web-app><servlet></web-app>", "src/main/webapp/WEB-INF/broken.xml")

    assert not facts.file.parse_ok
    assert facts.file.parse_error is not None
    assert "line" in facts.file.parse_error
    assert facts.entry_points == ()
