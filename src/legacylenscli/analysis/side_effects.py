"""Side-effect classification, fact construction and reporting.

Parsers own the syntax traversal: they know the line, the innermost function and the
argument expressions. This module owns the language-specific sink tables, so PHP and Java
produce the same ``SideEffectRecord`` shape through the same IR, and the store/report path
stays language-independent.

The classification is deliberately narrow:

* a call is a sink only when it names a known API, or when its receiver type is known to
  be a builtin class from evidence in the source (an import, a fully qualified type or a
  ``java.lang`` class);
* dynamic receivers and shadowed builtin function names produce nothing rather than a
  guessed effect;
* ``detail`` carries a literal target when the source states one and stays action-only
  when the target is dynamic. Dynamic text is never expanded.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from legacylenscli.ir.models import Confidence, SideEffectKind, SideEffectRecord
from legacylenscli.ir.store import FactStore

__all__ = [
    "SideEffectCandidate",
    "SideEffectGroup",
    "SideEffectReport",
    "SideEffectSite",
    "build_side_effect_report",
    "build_side_effect_report_from_records",
    "build_side_effects",
    "classify_java_constructor",
    "classify_java_method",
    "classify_jsp_process",
    "classify_jsp_session_write",
    "classify_php_function_call",
    "classify_php_session_write",
    "classify_php_shell_command",
    "resolve_java_type",
]

_KIND_ORDER = {
    SideEffectKind.DATABASE: 0,
    SideEffectKind.FILESYSTEM: 1,
    SideEffectKind.HTTP: 2,
    SideEffectKind.EMAIL: 3,
    SideEffectKind.SESSION: 4,
    SideEffectKind.OUTPUT: 5,
    SideEffectKind.PROCESS: 6,
    SideEffectKind.LOGGING: 7,
    SideEffectKind.CACHE: 8,
    SideEffectKind.OTHER: 9,
}

_PHP_LITERAL_RE = re.compile(r"^(?P<quote>['\"])(?P<body>.*)(?P=quote)$", re.DOTALL)
_URL_RE = re.compile(r"^(?:https?|ftps?)://", re.IGNORECASE)

# PHP builtins: name -> (kind, action, target). ``target`` names the argument that is
# printed in ``detail`` when it is a literal; ``None`` keeps the detail action-only.
_PHP_CALLS: dict[str, tuple[SideEffectKind, str, str | None]] = {
    # filesystem
    "file_put_contents": (SideEffectKind.FILESYSTEM, "file write", "path"),
    "fputcsv": (SideEffectKind.FILESYSTEM, "file write", None),
    "fputs": (SideEffectKind.FILESYSTEM, "file write", None),
    "fwrite": (SideEffectKind.FILESYSTEM, "file write", None),
    "fopen": (SideEffectKind.FILESYSTEM, "file open", "path"),
    "file_get_contents": (SideEffectKind.FILESYSTEM, "file read", "path"),
    "file": (SideEffectKind.FILESYSTEM, "file read", "path"),
    "readfile": (SideEffectKind.FILESYSTEM, "file read", "path"),
    "unlink": (SideEffectKind.FILESYSTEM, "file delete", "path"),
    "rename": (SideEffectKind.FILESYSTEM, "file rename", "path"),
    "copy": (SideEffectKind.FILESYSTEM, "file copy", "path"),
    "mkdir": (SideEffectKind.FILESYSTEM, "directory create", "path"),
    "rmdir": (SideEffectKind.FILESYSTEM, "directory delete", "path"),
    "chmod": (SideEffectKind.FILESYSTEM, "file chmod", "path"),
    "chown": (SideEffectKind.FILESYSTEM, "file chown", "path"),
    "touch": (SideEffectKind.FILESYSTEM, "file touch", "path"),
    "tmpfile": (SideEffectKind.FILESYSTEM, "temporary file create", None),
    "tempnam": (SideEffectKind.FILESYSTEM, "temporary file create", "path"),
    "move_uploaded_file": (SideEffectKind.FILESYSTEM, "file move", "path"),
    # email
    "mail": (SideEffectKind.EMAIL, "email send", "recipient"),
    "mb_send_mail": (SideEffectKind.EMAIL, "email send", "recipient"),
    "wp_mail": (SideEffectKind.EMAIL, "email send", "recipient"),
    # network
    "curl_exec": (SideEffectKind.HTTP, "http request", None),
    "curl_multi_exec": (SideEffectKind.HTTP, "http request", None),
    "fsockopen": (SideEffectKind.HTTP, "socket open", "host"),
    "pfsockopen": (SideEffectKind.HTTP, "socket open", "host"),
    "stream_socket_client": (SideEffectKind.HTTP, "socket open", "host"),
    "socket_connect": (SideEffectKind.HTTP, "socket connect", None),
    "socket_sendto": (SideEffectKind.HTTP, "socket send", None),
    # process
    "exec": (SideEffectKind.PROCESS, "process run", "command"),
    "system": (SideEffectKind.PROCESS, "process run", "command"),
    "passthru": (SideEffectKind.PROCESS, "process run", "command"),
    "shell_exec": (SideEffectKind.PROCESS, "process run", "command"),
    "popen": (SideEffectKind.PROCESS, "process run", "command"),
    "proc_open": (SideEffectKind.PROCESS, "process run", "command"),
    "pcntl_exec": (SideEffectKind.PROCESS, "process run", "command"),
}
_PHP_URL_AWARE = frozenset({"fopen", "file_get_contents", "file", "readfile"})

# Java sinks keyed by the resolved fully qualified type. Only these types are trusted; a
# same-named user class stays invisible because it does not resolve to the builtin.
_JAVA_METHODS: dict[str, dict[str, tuple[SideEffectKind, str]]] = {
    "java.nio.file.Files": {
        "copy": (SideEffectKind.FILESYSTEM, "file copy"),
        "createDirectories": (SideEffectKind.FILESYSTEM, "directory create"),
        "createDirectory": (SideEffectKind.FILESYSTEM, "directory create"),
        "createFile": (SideEffectKind.FILESYSTEM, "file create"),
        "createTempDirectory": (SideEffectKind.FILESYSTEM, "temporary directory create"),
        "createTempFile": (SideEffectKind.FILESYSTEM, "temporary file create"),
        "delete": (SideEffectKind.FILESYSTEM, "file delete"),
        "deleteIfExists": (SideEffectKind.FILESYSTEM, "file delete"),
        "move": (SideEffectKind.FILESYSTEM, "file move"),
        "newBufferedReader": (SideEffectKind.FILESYSTEM, "file read"),
        "newBufferedWriter": (SideEffectKind.FILESYSTEM, "file write"),
        "newByteChannel": (SideEffectKind.FILESYSTEM, "file open"),
        "newInputStream": (SideEffectKind.FILESYSTEM, "file read"),
        "newOutputStream": (SideEffectKind.FILESYSTEM, "file write"),
        "readAllBytes": (SideEffectKind.FILESYSTEM, "file read"),
        "readAllLines": (SideEffectKind.FILESYSTEM, "file read"),
        "readString": (SideEffectKind.FILESYSTEM, "file read"),
        "write": (SideEffectKind.FILESYSTEM, "file write"),
        "writeString": (SideEffectKind.FILESYSTEM, "file write"),
    },
    "java.io.File": {
        "createNewFile": (SideEffectKind.FILESYSTEM, "file create"),
        "delete": (SideEffectKind.FILESYSTEM, "file delete"),
        "deleteOnExit": (SideEffectKind.FILESYSTEM, "file delete"),
        "mkdir": (SideEffectKind.FILESYSTEM, "directory create"),
        "mkdirs": (SideEffectKind.FILESYSTEM, "directory create"),
        "renameTo": (SideEffectKind.FILESYSTEM, "file rename"),
        "setExecutable": (SideEffectKind.FILESYSTEM, "file chmod"),
        "setReadable": (SideEffectKind.FILESYSTEM, "file chmod"),
        "setWritable": (SideEffectKind.FILESYSTEM, "file chmod"),
    },
    "javax.mail.Transport": {
        "send": (SideEffectKind.EMAIL, "email send"),
    },
    "jakarta.mail.Transport": {
        "send": (SideEffectKind.EMAIL, "email send"),
    },
    "java.net.URL": {
        "openConnection": (SideEffectKind.HTTP, "http open"),
        "openStream": (SideEffectKind.HTTP, "http request"),
    },
    "java.net.URLConnection": {
        "connect": (SideEffectKind.HTTP, "http connect"),
        "getInputStream": (SideEffectKind.HTTP, "http request"),
        "getOutputStream": (SideEffectKind.HTTP, "http request"),
    },
    "java.net.HttpURLConnection": {
        "connect": (SideEffectKind.HTTP, "http connect"),
        "getInputStream": (SideEffectKind.HTTP, "http request"),
        "getOutputStream": (SideEffectKind.HTTP, "http request"),
    },
    "java.net.Socket": {
        "connect": (SideEffectKind.HTTP, "socket connect"),
    },
    "java.net.http.HttpClient": {
        "send": (SideEffectKind.HTTP, "http request"),
        "sendAsync": (SideEffectKind.HTTP, "http request"),
    },
    "java.lang.ProcessBuilder": {
        "start": (SideEffectKind.PROCESS, "process run"),
    },
    "java.lang.Runtime": {
        "exec": (SideEffectKind.PROCESS, "process run"),
    },
    "javax.servlet.http.HttpSession": {
        "invalidate": (SideEffectKind.SESSION, "session invalidate"),
        "putValue": (SideEffectKind.SESSION, "session write"),
        "removeAttribute": (SideEffectKind.SESSION, "session remove"),
        "removeValue": (SideEffectKind.SESSION, "session remove"),
        "setAttribute": (SideEffectKind.SESSION, "session write"),
    },
    "jakarta.servlet.http.HttpSession": {
        "invalidate": (SideEffectKind.SESSION, "session invalidate"),
        "putValue": (SideEffectKind.SESSION, "session write"),
        "removeAttribute": (SideEffectKind.SESSION, "session remove"),
        "removeValue": (SideEffectKind.SESSION, "session remove"),
        "setAttribute": (SideEffectKind.SESSION, "session write"),
    },
}
_JAVA_CONSTRUCTORS: dict[str, tuple[SideEffectKind, str]] = {
    "java.io.FileInputStream": (SideEffectKind.FILESYSTEM, "file read"),
    "java.io.FileOutputStream": (SideEffectKind.FILESYSTEM, "file write"),
    "java.io.FileReader": (SideEffectKind.FILESYSTEM, "file read"),
    "java.io.FileWriter": (SideEffectKind.FILESYSTEM, "file write"),
    "java.io.RandomAccessFile": (SideEffectKind.FILESYSTEM, "file open"),
    "java.net.DatagramSocket": (SideEffectKind.HTTP, "socket open"),
    "java.net.ServerSocket": (SideEffectKind.HTTP, "socket open"),
    "java.net.Socket": (SideEffectKind.HTTP, "socket open"),
}
# Types in ``java.lang`` need no import; short names are enough evidence.
_IMPLICIT_TYPES = frozenset(
    {
        "java.lang.ProcessBuilder",
        "java.lang.Runtime",
    }
)
_SINK_TYPES = frozenset(_JAVA_METHODS) | frozenset(_JAVA_CONSTRUCTORS) | _IMPLICIT_TYPES
_JAVA_SESSION_METHODS = frozenset(_JAVA_METHODS["javax.servlet.http.HttpSession"])
_RUNTIME_EXEC = "exec"


@dataclass(frozen=True, slots=True)
class SideEffectCandidate:
    """A classified sink before it is tied to a file, line and function."""

    kind: SideEffectKind
    sink: str
    detail: str
    confidence: Confidence = Confidence.VERIFIED

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SideEffectKind):
            object.__setattr__(self, "kind", SideEffectKind(self.kind))
        if not isinstance(self.confidence, Confidence):
            object.__setattr__(self, "confidence", Confidence(self.confidence))


@dataclass(frozen=True, slots=True)
class SideEffectSite:
    """A classified sink at an exact line, optionally inside a function."""

    candidate: SideEffectCandidate
    line: int
    function_id: str | None = None


@dataclass(frozen=True, slots=True)
class SideEffectGroup:
    """Every recorded effect of one kind."""

    kind: SideEffectKind
    records: tuple[SideEffectRecord, ...]
    sinks: tuple[str, ...]
    files: tuple[str, ...]
    functions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "count": len(self.records),
            "sinks": list(self.sinks),
            "files": list(self.files),
            "functions": list(self.functions),
            "records": [_record_dict(item) for item in self.records],
        }


@dataclass(frozen=True, slots=True)
class SideEffectReport:
    """Stored side effects grouped by kind, plus effects outside any function."""

    groups: tuple[SideEffectGroup, ...]
    unattributed: tuple[SideEffectRecord, ...]
    total: int

    def as_dict(self) -> dict[str, object]:
        return {
            "side_effects": self.total,
            "kinds": {group.kind.value: len(group.records) for group in self.groups},
            "sinks": _counts(
                record.sink or "" for group in self.groups for record in group.records
            ),
            "groups": [group.as_dict() for group in self.groups],
            "unattributed": [_record_dict(item) for item in self.unattributed],
        }


def build_side_effects(
    file_path: str,
    sites: Iterable[SideEffectSite],
) -> tuple[SideEffectRecord, ...]:
    """Turn classified sites into deterministic, de-duplicated records."""

    records: list[SideEffectRecord] = []
    seen: set[tuple[int, str, str, str, str | None]] = set()
    for site in sites:
        candidate = site.candidate
        key = (
            site.line,
            candidate.kind.value,
            candidate.sink,
            candidate.detail,
            site.function_id,
        )
        if key in seen:
            continue
        seen.add(key)
        records.append(
            SideEffectRecord(
                file_path=file_path,
                kind=candidate.kind,
                detail=candidate.detail,
                line=site.line,
                function_id=site.function_id,
                sink=candidate.sink,
                confidence=candidate.confidence,
            )
        )
    records.sort(
        key=lambda item: (
            item.line,
            _KIND_ORDER[item.kind],
            item.sink or "",
            item.detail,
        )
    )
    return tuple(records)


def build_side_effect_report(store: FactStore) -> SideEffectReport:
    """Build the report from every stored ``side_effects`` row."""

    return build_side_effect_report_from_records(store.list_side_effects())


def build_side_effect_report_from_records(
    records: Iterable[SideEffectRecord],
) -> SideEffectReport:
    """Build the report from an existing record sequence."""

    materialised = tuple(records)
    by_kind: dict[SideEffectKind, list[SideEffectRecord]] = defaultdict(list)
    unattributed: list[SideEffectRecord] = []
    for record in materialised:
        by_kind[record.kind].append(record)
        if record.function_id is None:
            unattributed.append(record)

    groups: list[SideEffectGroup] = []
    for kind in sorted(by_kind, key=lambda item: _KIND_ORDER[item]):
        items = tuple(
            sorted(
                by_kind[kind],
                key=lambda item: (
                    item.file_path,
                    item.line,
                    item.sink or "",
                    item.detail,
                    item.function_id or "",
                ),
            )
        )
        groups.append(
            SideEffectGroup(
                kind=kind,
                records=items,
                sinks=tuple(sorted({item.sink for item in items if item.sink})),
                files=tuple(sorted({item.file_path for item in items})),
                functions=tuple(sorted({item.function_id for item in items if item.function_id})),
            )
        )
    unattributed.sort(key=lambda item: (item.file_path, item.line, item.kind.value))
    return SideEffectReport(
        groups=tuple(groups),
        unattributed=tuple(unattributed),
        total=len(materialised),
    )


# -- PHP -----------------------------------------------------------------


def classify_php_function_call(
    name: str,
    arguments: Sequence[str] = (),
) -> SideEffectCandidate | None:
    """Classify a PHP builtin call, or return ``None`` when it is not a sink.

    ``arguments`` are the argument expressions as written, in source order. ``name`` is the
    called name without a leading namespace separator; matching is case-insensitive like
    PHP itself. The caller is responsible for checking that a same-file function does not
    shadow the builtin.
    """

    normalized = name.strip().lstrip("\\").lower()
    spec = _PHP_CALLS.get(normalized)
    if spec is None:
        return None
    kind, action, target = spec

    if normalized in _PHP_URL_AWARE:
        url = _php_literal(arguments[0]) if arguments else None
        if url is not None and _URL_RE.match(url):
            return SideEffectCandidate(
                kind=SideEffectKind.HTTP,
                sink=normalized,
                detail=_join(action="http request", target=url),
            )

    detail = action
    if target is not None:
        literal = _php_literal(arguments[0]) if arguments else None
        if literal is not None:
            detail = _join(action=action, target=literal)
    return SideEffectCandidate(kind=kind, sink=normalized, detail=detail)


def classify_php_session_write(expression: str, *, unset: bool = False) -> SideEffectCandidate:
    """Classify an assignment to or ``unset`` of a ``$_SESSION`` entry."""

    action = "session unset" if unset else "session write"
    target = " ".join(expression.split())
    return SideEffectCandidate(
        kind=SideEffectKind.SESSION,
        sink="$_SESSION",
        detail=_join(action=action, target=target),
    )


def classify_php_shell_command(text: str) -> SideEffectCandidate:
    """Classify a PHP backtick command. Interpolated commands keep no literal target."""

    inner = text.strip()
    if len(inner) >= 2 and inner.startswith("`") and inner.endswith("`"):
        inner = inner[1:-1]
    inner = " ".join(inner.split())
    detail = "process run"
    if inner and "$" not in inner:
        detail = _join(action=detail, target=inner)
    return SideEffectCandidate(kind=SideEffectKind.PROCESS, sink="``", detail=detail)


def _php_literal(text: str) -> str | None:
    """Return the value of a quoted PHP string with no interpolation, else ``None``."""

    match = _PHP_LITERAL_RE.match(text.strip())
    if match is None:
        return None
    body = match.group("body")
    if "$" in body:
        return None
    return body


# -- Java ----------------------------------------------------------------


def classify_java_method(
    receiver_type: str | None,
    method: str,
    arguments: Sequence[str] = (),
    *,
    receiver_expression: str | None = None,
    imports: Iterable[str] = (),
    request_receiver: bool = False,
) -> SideEffectCandidate | None:
    """Classify a Java method invocation.

    ``receiver_type`` is the declared or lexically known type of the receiver, if any.
    ``receiver_expression`` is the receiver source text, used for chained
    ``request.getSession()`` and ``Runtime.getRuntime()`` calls. ``imports`` are the file's
    import targets; they turn a simple static-receiver name into a trusted builtin type.
    """

    resolved = resolve_java_type(receiver_type, imports)
    confidence = Confidence.VERIFIED

    if resolved is None and receiver_expression:
        if (
            request_receiver
            and _session_chain_receiver(receiver_expression)
            and method in _JAVA_SESSION_METHODS
        ):
            resolved = "javax.servlet.http.HttpSession"
        elif _is_runtime_expression(receiver_expression) and method == _RUNTIME_EXEC:
            resolved = "java.lang.Runtime"

    if resolved is None:
        return None
    spec = _JAVA_METHODS.get(resolved, {}).get(method)
    if spec is None:
        return None
    kind, action = spec
    target = _java_literal(arguments[0]) if arguments else None
    if kind not in {SideEffectKind.SESSION, SideEffectKind.PROCESS}:
        target = None
    detail = action if target is None else _join(action=action, target=target)
    return SideEffectCandidate(
        kind=kind,
        sink=f"{_short_name(resolved)}.{method}",
        detail=detail,
        confidence=confidence,
    )


def classify_java_constructor(
    type_name: str,
    *,
    imports: Iterable[str] = (),
) -> SideEffectCandidate | None:
    """Classify a constructor that opens a file or network resource."""

    resolved = resolve_java_type(type_name, imports)
    if resolved is None:
        return None
    spec = _JAVA_CONSTRUCTORS.get(resolved)
    if spec is None:
        return None
    kind, action = spec
    return SideEffectCandidate(
        kind=kind,
        sink=f"new {_short_name(resolved)}",
        detail=action,
    )


def resolve_java_type(type_name: str | None, imports: Iterable[str] = ()) -> str | None:
    """Resolve a simple type name to a known sink type using source evidence only."""

    if not type_name:
        return None
    cleaned = " ".join(type_name.split()).strip()
    if not cleaned:
        return None
    if cleaned.startswith("?"):
        return None
    if "." in cleaned:
        base = cleaned.split("<", 1)[0].strip()
        return base if base in _SINK_TYPES else None

    short = cleaned.split("<", 1)[0].strip()
    if not short:
        return None
    full_names = set(imports)
    if short in {"Runtime", "ProcessBuilder"}:
        candidate = f"java.lang.{short}"
        if candidate in _IMPLICIT_TYPES:
            return candidate
    exact = sorted(name for name in full_names if name.rsplit(".", 1)[-1] == short)
    if len(exact) == 1 and exact[0] in _SINK_TYPES:
        return exact[0]
    for name in sorted(full_names):
        if not name.endswith(".*"):
            continue
        candidate = f"{name[:-2]}.{short}"
        if candidate in _SINK_TYPES:
            return candidate
    return None


def _session_chain_receiver(expression: str) -> bool:
    """Return ``True`` when the receiver text ends in a ``getSession()`` call."""

    return re.search(r"\.\s*getSession\s*\(\s*\)\s*$", expression) is not None


def _is_runtime_expression(expression: str) -> bool:
    text = " ".join(expression.split())
    return text in {"Runtime.getRuntime()", "java.lang.Runtime.getRuntime()"}


def _java_literal(text: str) -> str | None:
    value = text.strip()
    if len(value) >= 6 and value.startswith('"""') and value.endswith('"""'):
        inner = value[3:-3].strip()
        return inner or None
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return None
    inner = value[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\") or None


# -- JSP -----------------------------------------------------------------


def classify_jsp_session_write(
    method: str,
    arguments: Sequence[str] = (),
    *,
    confidence: Confidence = Confidence.VERIFIED,
) -> SideEffectCandidate | None:
    """Classify a scriptlet session write against the servlet session API."""

    spec = _JAVA_METHODS["javax.servlet.http.HttpSession"].get(method)
    if spec is None:
        return None
    kind, action = spec
    target = _java_literal(arguments[0]) if arguments else None
    detail = action if target is None else _join(action=action, target=target)
    return SideEffectCandidate(
        kind=kind,
        sink=f"HttpSession.{method}",
        detail=detail,
        confidence=confidence,
    )


def classify_jsp_process(arguments: Sequence[str] = ()) -> SideEffectCandidate:
    """Classify ``Runtime.getRuntime().exec`` inside a JSP scriptlet."""

    target = _java_literal(arguments[0]) if arguments else None
    detail = "process run" if target is None else _join(action="process run", target=target)
    return SideEffectCandidate(
        kind=SideEffectKind.PROCESS,
        sink="Runtime.getRuntime().exec",
        detail=detail,
    )


# -- shared --------------------------------------------------------------


def _join(*, action: str, target: str) -> str:
    cleaned = " ".join(target.split())
    return f"{action} {cleaned}" if cleaned else action


def _short_name(type_name: str) -> str:
    return type_name.rsplit(".", 1)[-1]


def _record_dict(record: SideEffectRecord) -> dict[str, object]:
    return {
        "file_path": record.file_path,
        "line": record.line,
        "kind": record.kind.value,
        "sink": record.sink,
        "detail": record.detail,
        "function": record.function_id,
        "confidence": record.confidence.value,
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(sorted(counts.items()))
