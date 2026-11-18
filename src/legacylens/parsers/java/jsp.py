"""JSP front-end: page entry points, includes, form fields and request inputs.

JSP has no dependable Tree-sitter grammar, so this front-end scans the page text while
keeping every fact tied to a real line. Directives and scriptlets are matched with
anchored patterns; nothing is inferred from tag structure that the page does not state.

Coverage:

* a ``.jsp``/``.jspx`` page outside ``WEB-INF`` becomes a JSP entry point;
* ``.jspf``/``.tag``/``.tagx`` files and pages below ``WEB-INF`` are fragments or
  includes, never directly addressable entry points;
* ``<%@ include %>``, ``<jsp:include>`` and ``<%@ page import %>`` become edges;
* EL (``${param.x}``, ``${header.x}``, ``${cookie.x}`` ...), scriptlet servlet calls and
  HTML form controls become request inputs;
* a form field is marked inferred and dropped when an explicit read of the same name
  already exists.
"""

from __future__ import annotations

import posixpath
import re
from bisect import bisect_right
from dataclasses import dataclass

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
    SideEffectRecord,
)

from ...analysis.side_effects import (
    SideEffectSite,
    build_side_effects,
    classify_jsp_process,
    classify_jsp_session_write,
)
from ..base import ParseError, Parser
from ..edges import make_edge
from ..web import DYNAMIC_INPUT, is_under_web_inf, web_path
from .servlet_api import EL_INPUT_OBJECTS, SERVLET_REQUEST_CALLS

__all__ = ["JspParser"]

_FRAGMENT_SUFFIXES = frozenset({".jspf", ".tag", ".tagx"})
_PAGE_SUFFIXES = frozenset({".jsp", ".jspx"})
_FORM_KINDS = frozenset({RequestInputKind.FORM, RequestInputKind.QUERY, RequestInputKind.REQUEST})
_NON_DATA_INPUT_TYPES = frozenset({"button", "image", "reset", "submit"})

_COMMENT_RE = re.compile(r"<%--.*?--%>|<!--.*?-->", re.DOTALL)
_PAGE_DIRECTIVE_RE = re.compile(r"<%@\s*page\b(?P<body>.*?)%>", re.DOTALL | re.IGNORECASE)
_IMPORT_ATTRIBUTE_RE = re.compile(
    r"\bimport\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    re.DOTALL | re.IGNORECASE,
)
_STATIC_INCLUDE_RE = re.compile(
    r"<%@\s*include\s+file\s*=\s*(?:\"([^\"]*)\"|'([^']*)')\s*%>",
    re.IGNORECASE,
)
_DYNAMIC_INCLUDE_RE = re.compile(
    r"<jsp:include\b[^>]*?\bpage\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_SCRIPTLET_RE = re.compile(r"<%(?![-@])=?(?P<body>.*?)%>", re.DOTALL)
_JSP_SESSION_RE = re.compile(
    r"\b(?P<receiver>(?:request|req)\s*\.\s*getSession\s*\(\s*\)|session)\s*\.\s*"
    r"(?P<method>setAttribute|putValue|removeAttribute|removeValue|invalidate)\s*"
    r"\((?P<arguments>[^)]*)\)"
)
_JSP_EXEC_RE = re.compile(
    r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\s*\((?P<arguments>[^)]*)\)"
)
_EL_DOT_RE = re.compile(
    r"\$\{\s*(?P<object>param|paramValues|header|headerValues|cookie)\s*\.\s*"
    r"(?P<name>[A-Za-z_$][\w$-]*)\s*\}"
)
_EL_BRACKET_RE = re.compile(
    r"\$\{\s*(?P<object>param|paramValues|header|headerValues|cookie)\s*\[\s*"
    r"(?P<quote>['\"])(?P<name>.*?)(?P=quote)\s*\]\s*\}",
    re.DOTALL,
)
_JSP_CALL_RE = re.compile(
    r"\b(?P<receiver>[A-Za-z_$][\w$]*)\s*\.\s*(?P<method>"
    + "|".join(sorted(SERVLET_REQUEST_CALLS, key=len, reverse=True))
    + r")\s*\((?P<arguments>[^)]*)\)"
)
_FORM_CONTROL_RE = re.compile(r"<(?P<tag>input|select|textarea|button)\b(?P<attrs>[^>]*)>", re.I)
_ATTRIBUTE_RE = re.compile(
    r"(?P<key>[A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"
    r"(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"
)
_REQUEST_RECEIVERS = frozenset({"httprequest", "req", "request", "servletrequest"})


class JspParser(Parser):
    """Extract page-level facts from JSP and JSP fragment files without a full grammar."""

    language = Language.JSP

    def parse(self, source: bytes, file: FileRecord) -> FileFacts:
        if not self.supports(file):
            raise ParseError(f"JspParser cannot parse {file.language.value} file {file.path!r}")

        text = source.decode("utf-8", errors="replace")
        masked = _mask_comments(text)
        lines = _LineIndex(text)
        inputs = _request_inputs(masked, lines, file.path)
        return FileFacts(
            file=file,
            edges=_edges(masked, lines, file.path),
            entry_points=_entry_points(file),
            request_inputs=inputs,
            side_effects=_side_effects(masked, lines, file.path),
        )


def _entry_points(file: FileRecord) -> tuple[EntryPointRecord, ...]:
    suffix = posixpath.splitext(file.path)[1].lower()
    if suffix not in _PAGE_SUFFIXES or is_under_web_inf(file.path):
        return ()
    return (
        EntryPointRecord(
            file_path=file.path,
            kind=EntryKind.JSP,
            path=web_path(file.path),
            line=1,
            framework="jsp",
            confidence=Confidence.INFERRED,
        ),
    )


def _request_inputs(
    text: str,
    lines: _LineIndex,
    file_path: str,
) -> tuple[RequestInputRecord, ...]:
    explicit: list[RequestInputRecord] = []
    explicit.extend(_el_inputs(text, lines, file_path))
    explicit.extend(_scriptlet_inputs(text, lines, file_path))

    keys = {(item.kind, item.name) for item in explicit}
    read_names = {item.name for item in explicit if item.kind in _FORM_KINDS}
    form_inputs: list[RequestInputRecord] = []
    for record in _form_control_inputs(text, lines, file_path):
        key = (record.kind, record.name)
        if key in keys:
            continue
        if record.kind is RequestInputKind.FORM and record.name in read_names:
            continue
        keys.add(key)
        form_inputs.append(record)

    result = [*explicit, *form_inputs]
    result.sort(key=lambda item: (item.line, item.kind.value, item.name))
    return tuple(result)


def _side_effects(
    text: str,
    lines: _LineIndex,
    file_path: str,
) -> tuple[SideEffectRecord, ...]:
    """Detect session writes and process execution inside scriptlets only."""

    sites: list[SideEffectSite] = []
    for block in _SCRIPTLET_RE.finditer(text):
        body = block.group("body")
        masked_body = _mask_java_code(body)
        offset = block.start("body")
        for match in _JSP_SESSION_RE.finditer(masked_body):
            raw_argument = body[match.start("arguments") : match.end("arguments")]
            argument = _first_argument(raw_argument)
            candidate = classify_jsp_session_write(
                match.group("method"),
                (argument,) if argument is not None else (),
                confidence=(
                    Confidence.INFERRED
                    if "getSession" in match.group("receiver")
                    else Confidence.VERIFIED
                ),
            )
            if candidate is None:
                continue
            sites.append(
                SideEffectSite(
                    candidate=candidate,
                    line=lines.line(offset + match.start()),
                )
            )
        for match in _JSP_EXEC_RE.finditer(masked_body):
            raw_argument = body[match.start("arguments") : match.end("arguments")]
            argument = _first_argument(raw_argument)
            sites.append(
                SideEffectSite(
                    candidate=classify_jsp_process((argument,) if argument is not None else ()),
                    line=lines.line(offset + match.start()),
                )
            )
    return build_side_effects(file_path, sites)


def _first_argument(raw: str) -> str | None:
    """Return the first argument expression, stopping at a top-level comma."""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ",":
            raw = raw[:index]
            break
    value = raw.strip()
    return value or None


def _mask_java_code(text: str) -> str:
    """Blank string, character and comment bodies inside a scriptlet, keeping offsets."""

    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "/":
            while index < length and text[index] != "\n":
                chars[index] = " "
                index += 1
        elif char == "/" and nxt == "*":
            chars[index] = " "
            chars[index + 1] = " "
            index += 2
            while index < length:
                if text[index] == "*" and index + 1 < length and text[index + 1] == "/":
                    chars[index] = " "
                    chars[index + 1] = " "
                    index += 2
                    break
                chars[index] = "\n" if text[index] == "\n" else " "
                index += 1
        elif char in {'"', "'"}:
            quote = char
            chars[index] = " "
            index += 1
            while index < length:
                if text[index] == "\\":
                    chars[index] = " "
                    if index + 1 < length:
                        chars[index + 1] = " "
                    index += 2
                    continue
                if text[index] == quote:
                    chars[index] = " "
                    index += 1
                    break
                chars[index] = "\n" if text[index] == "\n" else " "
                index += 1
        else:
            index += 1
    return "".join(chars)


def _el_inputs(
    text: str,
    lines: _LineIndex,
    file_path: str,
) -> list[RequestInputRecord]:
    records: list[RequestInputRecord] = []
    for pattern in (_EL_DOT_RE, _EL_BRACKET_RE):
        for match in pattern.finditer(text):
            kind = EL_INPUT_OBJECTS.get(match.group("object"))
            name = match.group("name")
            if kind is None or not name:
                continue
            if kind is RequestInputKind.HEADER:
                name = name.lower()
            records.append(
                _input_record(
                    file_path,
                    kind,
                    name,
                    lines.line(match.start()),
                    match.group(0),
                    confidence=Confidence.VERIFIED,
                )
            )
    return records


def _scriptlet_inputs(
    text: str,
    lines: _LineIndex,
    file_path: str,
) -> list[RequestInputRecord]:
    records: list[RequestInputRecord] = []
    for block in _SCRIPTLET_RE.finditer(text):
        body = block.group("body")
        body_offset = block.start("body")
        for call in _JSP_CALL_RE.finditer(body):
            receiver = call.group("receiver")
            if receiver.lower() not in _REQUEST_RECEIVERS:
                continue
            kind, uses_name = SERVLET_REQUEST_CALLS[call.group("method")]
            name = DYNAMIC_INPUT
            if uses_name:
                name = _literal_argument(call.group("arguments")) or DYNAMIC_INPUT
            if kind is RequestInputKind.HEADER and name != DYNAMIC_INPUT:
                name = name.lower()
            records.append(
                _input_record(
                    file_path,
                    kind,
                    name,
                    lines.line(body_offset + call.start()),
                    call.group(0),
                    confidence=(
                        Confidence.VERIFIED if name != DYNAMIC_INPUT else Confidence.INFERRED
                    ),
                )
            )
    return records


def _form_control_inputs(
    text: str,
    lines: _LineIndex,
    file_path: str,
) -> list[RequestInputRecord]:
    records: list[RequestInputRecord] = []
    for match in _FORM_CONTROL_RE.finditer(text):
        attributes = _attributes(match.group("attrs"))
        name = attributes.get("name", "").strip()
        if not name:
            continue
        tag = match.group("tag").lower()
        input_type = attributes.get("type", "text").strip().lower() or "text"
        if tag in {"input", "button"} and input_type in _NON_DATA_INPUT_TYPES:
            continue
        kind = RequestInputKind.FILE if input_type == "file" else RequestInputKind.FORM
        if "<%" in name or "${" in name:
            name = DYNAMIC_INPUT
        records.append(
            _input_record(
                file_path,
                kind,
                name,
                lines.line(match.start()),
                match.group(0),
                confidence=Confidence.INFERRED,
            )
        )
    return records


def _input_record(
    file_path: str,
    kind: RequestInputKind,
    name: str,
    line: int,
    raw: str,
    *,
    confidence: Confidence,
) -> RequestInputRecord:
    return RequestInputRecord(
        file_path=file_path,
        kind=kind,
        name=name,
        line=line,
        framework="jsp",
        raw=" ".join(raw.split()),
        confidence=confidence,
    )


def _edges(text: str, lines: _LineIndex, file_path: str) -> tuple[EdgeRecord, ...]:
    edges: list[EdgeRecord] = []
    for directive in _PAGE_DIRECTIVE_RE.finditer(text):
        body = directive.group("body")
        for attribute in _IMPORT_ATTRIBUTE_RE.finditer(body):
            imports = attribute.group(1) if attribute.group(1) is not None else attribute.group(2)
            for reference in imports.split(","):
                reference = reference.strip()
                if not reference:
                    continue
                edges.append(
                    make_edge(
                        src=file_path,
                        reference=reference,
                        kind=EdgeKind.IMPORT,
                        file_path=file_path,
                        line=lines.line(directive.start()),
                        raw=directive.group(0),
                        confidence=Confidence.VERIFIED,
                    )
                )

    for match in _STATIC_INCLUDE_RE.finditer(text):
        target = match.group(1) if match.group(1) is not None else match.group(2)
        edges.append(_include_edge(target, file_path, lines.line(match.start()), match.group(0)))

    for match in _DYNAMIC_INCLUDE_RE.finditer(text):
        target = next((group for group in match.groups() if group is not None), "")
        edges.append(_include_edge(target, file_path, lines.line(match.start()), match.group(0)))

    edges.sort(key=lambda item: (item.line, item.kind.value, item.dst))
    return tuple(edges)


def _include_edge(target: str, file_path: str, line: int, raw: str) -> EdgeRecord:
    value = target.strip()
    if not value or "<%" in value or "${" in value:
        return make_edge(
            src=file_path,
            reference=value or raw,
            kind=EdgeKind.INCLUDE,
            file_path=file_path,
            line=line,
            raw=raw,
            confidence=Confidence.UNSUPPORTED,
        )
    if value.startswith("/"):
        return make_edge(
            src=file_path,
            reference=value,
            kind=EdgeKind.INCLUDE,
            file_path=file_path,
            line=line,
            raw=raw,
            confidence=Confidence.INFERRED,
        )
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(file_path), value))
    if resolved.startswith(".."):
        return make_edge(
            src=file_path,
            reference=value,
            kind=EdgeKind.INCLUDE,
            file_path=file_path,
            line=line,
            raw=raw,
            confidence=Confidence.UNSUPPORTED,
        )
    return make_edge(
        src=file_path,
        reference=resolved,
        kind=EdgeKind.INCLUDE,
        file_path=file_path,
        line=line,
        resolved=True,
        raw=raw,
        confidence=Confidence.VERIFIED,
    )


def _attributes(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _ATTRIBUTE_RE.finditer(raw):
        value = next(
            (
                item
                for item in (
                    match.group("double"),
                    match.group("single"),
                    match.group("bare"),
                )
                if item is not None
            ),
            "",
        )
        result[match.group("key").lower()] = value
    return result


def _literal_argument(raw: str) -> str | None:
    value = raw.strip()
    if len(value) < 2 or value[0] not in {'"', "'"} or value[0] != value[-1]:
        return None
    inner = value[1:-1]
    quote = value[0]
    return inner.replace("\\" + quote, quote).replace("\\\\", "\\")


def _mask_comments(text: str) -> str:
    """Replace JSP/HTML comment bodies with spaces, preserving offsets and newlines."""

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _COMMENT_RE.sub(blank, text)


@dataclass(frozen=True, slots=True)
class _LineIndex:
    """Map a character offset to its 1-based line without rescanning the page."""

    starts: tuple[int, ...]

    def __init__(self, text: str) -> None:
        starts = [0]
        starts.extend(match.end() for match in re.finditer("\n", text))
        object.__setattr__(self, "starts", tuple(starts))

    def line(self, offset: int) -> int:
        return bisect_right(self.starts, offset)
