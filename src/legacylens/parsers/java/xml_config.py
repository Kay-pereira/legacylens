"""XML deployment descriptors: ``web.xml``, ``struts-config.xml`` and ``struts.xml``.

Descriptors are parsed with the standard library's Expat parser rather than a Tree-sitter
grammar. That keeps namespaces, entity references and exact line numbers working without
adding a runtime dependency, and it copes with the namespace-free descriptors common in
legacy applications.

The parser recognises the document by its root element, so a renamed descriptor still
works: ``web-app`` yields servlet/filter entry points, ``struts-config`` yields Struts 1
actions and form properties, and ``struts`` yields Struts 2 actions. Unknown XML (TLDs,
schemas, Spring context files) produces no facts but still parses cleanly.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field, replace
from xml.parsers import expat

from legacylens.ir.models import (
    Confidence,
    EdgeKind,
    EdgeRecord,
    EntryKind,
    EntryPointRecord,
    FileFacts,
    FileRecord,
    Language,
    RequestInputKind,
    RequestInputRecord,
)

from ..base import ParseError, Parser
from ..edges import make_edge
from ..web import normalise_url_path

__all__ = ["XmlConfigParser"]


class XmlConfigParser(Parser):
    """Extract entry points and request inputs from Java web deployment descriptors."""

    language = Language.XML

    def parse(self, source: bytes, file: FileRecord) -> FileFacts:
        if not self.supports(file):
            raise ParseError(
                f"XmlConfigParser cannot parse {file.language.value} file {file.path!r}"
            )

        try:
            root = _parse(source)
        except expat.ExpatError as error:
            message = f"XML error at line {error.lineno}: {expat.ErrorString(error.code)}"
            return FileFacts(file=replace(file, parse_error=message))

        entry_points: list[EntryPointRecord] = []
        request_inputs: list[RequestInputRecord] = []
        edges: list[EdgeRecord] = []
        if root.tag == "web-app":
            _web_facts(root, file.path, entry_points)
        elif root.tag == "struts-config":
            _struts1_facts(root, file.path, entry_points, request_inputs)
        elif root.tag == "struts":
            _struts2_facts(root, file.path, entry_points)
        _descriptor_includes(root, file.path, edges)

        entry_points.sort(key=lambda item: (item.line, item.kind.value, item.path))
        request_inputs.sort(key=lambda item: (item.line, item.kind.value, item.name))
        return FileFacts(
            file=file,
            edges=tuple(edges),
            entry_points=tuple(entry_points),
            request_inputs=tuple(request_inputs),
        )


def _web_facts(
    root: _Element,
    file_path: str,
    entry_points: list[EntryPointRecord],
) -> None:
    servlets: dict[str, _ServletTarget] = {}
    for servlet in root.children_named("servlet"):
        name = servlet.value("servlet-name")
        if not name:
            continue
        servlets[name] = _ServletTarget(
            handler=servlet.value("servlet-class") or None,
            jsp_file=servlet.value("jsp-file") or None,
        )

    for mapping in root.children_named("servlet-mapping"):
        name = mapping.value("servlet-name")
        target = servlets.get(name)
        for pattern in mapping.values("url-pattern"):
            path = normalise_url_path(pattern)
            if target is not None and target.jsp_file:
                entry_points.append(
                    EntryPointRecord(
                        file_path=file_path,
                        kind=EntryKind.JSP,
                        path=path,
                        line=mapping.line,
                        handler_ref=target.jsp_file,
                        framework="jsp",
                        confidence=Confidence.VERIFIED,
                    )
                )
                continue
            entry_points.append(
                EntryPointRecord(
                    file_path=file_path,
                    kind=EntryKind.SERVLET,
                    path=path,
                    line=mapping.line,
                    handler_ref=target.handler if target is not None else None,
                    framework="servlet",
                    confidence=Confidence.VERIFIED,
                )
            )

    filters: dict[str, str | None] = {}
    for item in root.children_named("filter"):
        name = item.value("filter-name")
        if name:
            filters[name] = item.value("filter-class") or None

    for mapping in root.children_named("filter-mapping"):
        name = mapping.value("filter-name")
        handler = filters.get(name)
        for pattern in mapping.values("url-pattern"):
            entry_points.append(
                EntryPointRecord(
                    file_path=file_path,
                    kind=EntryKind.FILTER,
                    path=normalise_url_path(pattern),
                    line=mapping.line,
                    handler_ref=handler,
                    framework="servlet",
                    confidence=Confidence.VERIFIED,
                )
            )


def _struts1_facts(
    root: _Element,
    file_path: str,
    entry_points: list[EntryPointRecord],
    request_inputs: list[RequestInputRecord],
) -> None:
    for form_beans in root.children_named("form-beans"):
        for form_bean in form_beans.children_named("form-bean"):
            for property_element in form_bean.children_named("form-property"):
                name = property_element.attrs.get("name", "").strip()
                if not name:
                    continue
                request_inputs.append(
                    RequestInputRecord(
                        file_path=file_path,
                        kind=RequestInputKind.FORM,
                        name=name,
                        line=property_element.line,
                        framework="struts",
                        raw=_attribute_text(property_element),
                        confidence=Confidence.INFERRED,
                    )
                )

    for mappings in root.children_named("action-mappings"):
        for action in mappings.children_named("action"):
            path = action.attrs.get("path", "").strip()
            if not path:
                continue
            entry_points.append(
                EntryPointRecord(
                    file_path=file_path,
                    kind=EntryKind.ACTION,
                    path=normalise_url_path(path),
                    line=action.line,
                    handler_ref=action.attrs.get("type") or None,
                    framework="struts",
                    confidence=Confidence.VERIFIED,
                )
            )


def _struts2_facts(
    root: _Element,
    file_path: str,
    entry_points: list[EntryPointRecord],
) -> None:
    for package in root.children_named("package"):
        namespace = package.attrs.get("namespace", "")
        for action in package.children_named("action"):
            name = action.attrs.get("name", "").strip()
            if not name:
                continue
            handler = action.attrs.get("class") or None
            method = action.attrs.get("method", "").strip()
            if handler and method:
                handler = f"{handler}::{method}"
            raw_path = "/".join(part.strip("/") for part in (namespace, name) if part.strip("/"))
            entry_points.append(
                EntryPointRecord(
                    file_path=file_path,
                    kind=EntryKind.ACTION,
                    path=normalise_url_path(raw_path),
                    line=action.line,
                    handler_ref=handler,
                    framework="struts",
                    confidence=Confidence.VERIFIED,
                )
            )


def _descriptor_includes(
    root: _Element,
    file_path: str,
    edges: list[EdgeRecord],
) -> None:
    for include in root.children_named("include"):
        target = include.attrs.get("file", "").strip()
        if not target:
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(file_path), target))
        dynamic = "${" in target or "<%" in target
        edges.append(
            make_edge(
                src=file_path,
                reference=target if dynamic or resolved.startswith("..") else resolved,
                kind=EdgeKind.INCLUDE,
                file_path=file_path,
                line=include.line,
                resolved=not dynamic and not resolved.startswith(".."),
                raw=_attribute_text(include),
                confidence=(Confidence.UNSUPPORTED if dynamic else Confidence.VERIFIED),
            )
        )


@dataclass(frozen=True, slots=True)
class _ServletTarget:
    handler: str | None
    jsp_file: str | None


@dataclass(slots=True)
class _Element:
    tag: str
    attrs: dict[str, str]
    line: int
    children: list[_Element] = field(default_factory=list)
    text: str = ""

    def children_named(self, tag: str) -> list[_Element]:
        return [child for child in self.children if child.tag == tag]

    def value(self, tag: str) -> str:
        child = next((item for item in self.children if item.tag == tag), None)
        return "" if child is None else child.text.strip()

    def values(self, tag: str) -> list[str]:
        return [
            child.text.strip() for child in self.children if child.tag == tag and child.text.strip()
        ]


def _parse(source: bytes) -> _Element:
    parser = expat.ParserCreate()
    root: list[_Element] = []
    stack: list[_Element] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        element = _Element(
            tag=_local_name(name),
            attrs={_local_name(key): value for key, value in attrs.items()},
            line=parser.CurrentLineNumber,
        )
        if stack:
            stack[-1].children.append(element)
        else:
            root.append(element)
        stack.append(element)

    def end(name: str) -> None:
        if stack:
            stack.pop()

    def characters(data: str) -> None:
        if stack:
            stack[-1].text += data

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.Parse(source, True)
    return root[0]


def _local_name(name: str) -> str:
    """Strip a namespace prefix, leaving the element or attribute's local name."""

    return name.rsplit(":", 1)[-1]


def _attribute_text(element: _Element) -> str:
    return " ".join(f'{key}="{value}"' for key, value in element.attrs.items())
