"""Typed, language-agnostic facts shared by every LegacyLens stage.

The dataclasses in this module are the in-memory form of the IR and mirror the SQLite
tables in :mod:`legacylens.ir.schema`. Facts are immutable values: parsers and analyses
build new :class:`FileFacts` aggregates, the store persists them, and publishers render
them. The normative description, including key formats and citation grammar, lives in
``docs/ir-spec.md``.

Design rules:

* Every fact carries the repository-relative path of the file it came from, a 1-based
  line (or a :class:`Span`), and a :class:`Confidence` label.
* Keys for classes and functions are derived from the file path and qualified name, so
  re-analysing a file can replace its facts without dangling references.
* Enums are :class:`~enum.StrEnum` subclasses: they serialise to the strings stored in
  SQLite and read naturally in JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "CacheStats",
    "CachedCompletion",
    "ClaimKind",
    "ClaimOrigin",
    "ClaimRecord",
    "ClassKind",
    "ClassRecord",
    "Confidence",
    "DataAccessRecord",
    "EdgeKind",
    "EdgeRecord",
    "EntryKind",
    "EntryPointRecord",
    "FileFacts",
    "FileRecord",
    "FunctionRecord",
    "HttpMethod",
    "Language",
    "Parameter",
    "RequestInputKind",
    "RequestInputRecord",
    "SideEffectKind",
    "SideEffectRecord",
    "Span",
    "SqlOperation",
    "Visibility",
    "class_id",
    "entry_citation",
    "function_id",
    "utc_now",
    "weakest_confidence",
]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Confidence(StrEnum):
    """How well a fact is grounded in source."""

    VERIFIED = "verified"
    INFERRED = "inferred"
    UNSUPPORTED = "unsupported"


_CONFIDENCE_RANK = {
    Confidence.UNSUPPORTED: 0,
    Confidence.INFERRED: 1,
    Confidence.VERIFIED: 2,
}


def weakest_confidence(*labels: Confidence) -> Confidence:
    """Return the least trustworthy label in ``labels``.

    Generated claims inherit the weakest label of their evidence. At least one label is
    required because a claim with no evidence is a programming error, not an
    ``unsupported`` fact.
    """

    if not labels:
        raise ValueError("weakest_confidence() requires at least one label")
    return min(labels, key=_CONFIDENCE_RANK.__getitem__)


class Language(StrEnum):
    """Languages and descriptor formats understood by the parsers."""

    PHP = "php"
    JAVA = "java"
    JSP = "jsp"
    XML = "xml"
    OTHER = "other"


class Visibility(StrEnum):
    """Member visibility; ``PACKAGE`` is Java's default/package-private scope."""

    PUBLIC = "public"
    PROTECTED = "protected"
    PRIVATE = "private"
    PACKAGE = "package"


class ClassKind(StrEnum):
    """Kind of type declaration."""

    CLASS = "class"
    INTERFACE = "interface"
    TRAIT = "trait"
    ENUM = "enum"
    RECORD = "record"


class EdgeKind(StrEnum):
    """Relationship between two units.

    ``INCLUDE`` and ``REQUIRE`` resolve to file paths, ``CALL``/``INSTANTIATE`` to
    function or class keys, and ``ROUTE``/``VIEW`` to an entry point or template.
    """

    INCLUDE = "include"
    REQUIRE = "require"
    IMPORT = "import"
    CALL = "call"
    INSTANTIATE = "instantiate"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    USES_TRAIT = "uses_trait"
    ROUTE = "route"
    VIEW = "view"


class SqlOperation(StrEnum):
    """Normalised SQL statement operation."""

    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    REPLACE = "replace"
    MERGE = "merge"
    DDL = "ddl"
    EXEC = "exec"
    OTHER = "other"

    @property
    def crud(self) -> str | None:
        """Return ``R``/``C``/``U``/``D`` for the CRUD matrix, or ``None``."""

        return {
            SqlOperation.SELECT: "R",
            SqlOperation.INSERT: "C",
            SqlOperation.REPLACE: "C",
            SqlOperation.UPDATE: "U",
            SqlOperation.DELETE: "D",
        }.get(self)


class HttpMethod(StrEnum):
    """HTTP method attached to an entry point; ``ANY`` covers catch-all routes."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    ANY = "ANY"


class EntryKind(StrEnum):
    """How control enters the application."""

    ROUTE = "route"
    SERVLET = "servlet"
    FILTER = "filter"
    ACTION = "action"
    JSP = "jsp"
    CLI = "cli"
    CRON = "cron"


class RequestInputKind(StrEnum):
    """Where a user-controlled request value comes from."""

    QUERY = "query"
    FORM = "form"
    REQUEST = "request"
    COOKIE = "cookie"
    FILE = "file"
    HEADER = "header"
    BODY = "body"
    PATH = "path"
    SESSION = "session"
    OTHER = "other"


class SideEffectKind(StrEnum):
    """External effect performed by a function."""

    DATABASE = "database"
    FILESYSTEM = "filesystem"
    HTTP = "http"
    EMAIL = "email"
    SESSION = "session"
    OUTPUT = "output"
    PROCESS = "process"
    LOGGING = "logging"
    CACHE = "cache"
    OTHER = "other"


class ClaimKind(StrEnum):
    """Subject matter of a generated or derived claim."""

    SUMMARY = "summary"
    PURPOSE = "purpose"
    BEHAVIOUR = "behaviour"
    BUSINESS_RULE = "business_rule"
    RISK = "risk"
    DATA_MODEL = "data_model"
    ENTRY_POINT = "entry_point"
    TABLE = "table"
    OTHER = "other"


class ClaimOrigin(StrEnum):
    """Where a claim was produced."""

    STATIC = "static"
    LLM = "llm"


# ---------------------------------------------------------------------------
# Value objects and keys
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def class_id(file_path: str, qualified_name: str) -> str:
    """Build the stable key for a class: ``class:<path>#<qualified-name>``."""

    return f"class:{file_path}#{qualified_name}"


def function_id(file_path: str, qualified_name: str) -> str:
    """Build the stable key for a function: ``fn:<path>#<qualified-name>``."""

    return f"fn:{file_path}#{qualified_name}"


def entry_citation(kind: EntryKind, method: HttpMethod | None, path: str) -> str:
    """Build an entry-point citation: ``entry:<kind>:<method-or-dash>:<path>``."""

    method_text = method.value if method is not None else "-"
    return f"entry:{kind.value}:{method_text}:{path}"


@dataclass(frozen=True, slots=True)
class Span:
    """Inclusive source span. Lines are 1-based, columns 0-based (Tree-sitter)."""

    start_line: int
    end_line: int
    start_column: int = 0
    end_column: int = 0

    def __post_init__(self) -> None:
        if self.start_line < 1:
            raise ValueError("span start_line must be >= 1")
        if self.end_line < self.start_line:
            raise ValueError("span end_line must be >= start_line")
        if self.start_column < 0 or self.end_column < 0:
            raise ValueError("span columns must be >= 0")

    @property
    def line_count(self) -> int:
        """Number of source lines covered by the span."""

        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class Parameter:
    """One declared function parameter, stored as JSON on the function row."""

    name: str
    type: str | None = None
    default: str | None = None
    variadic: bool = False
    by_reference: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("parameter name must not be empty")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        return {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "variadic": self.variadic,
            "by_reference": self.by_reference,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Parameter:
        """Rebuild a parameter from :meth:`as_dict` output."""

        return cls(
            name=str(data.get("name", "")),
            type=data.get("type"),
            default=data.get("default"),
            variadic=bool(data.get("variadic", False)),
            by_reference=bool(data.get("by_reference", False)),
        )


def _coerce_enum(enum_type: type[StrEnum], value: Any, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError as exc:  # pragma: no cover - defensive, message matters
        allowed = ", ".join(repr(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of {allowed}; got {value!r}") from exc


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_key_component(value: str, field_name: str) -> None:
    """Validate text that becomes part of a ``fn:``/``class:`` key or citation."""

    _require_text(value, field_name)
    if "#" in value:
        raise ValueError(f"{field_name} must not contain '#' (reserved key separator)")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must not contain a newline")


def _require_non_negative(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _normalise_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRecord:
    """A source file known to the project.

    ``path`` is the stable identity of a file and the anchor for all other facts.
    """

    path: str
    language: Language
    content_hash: str
    size_bytes: int
    loc: int = 0
    package: str | None = None
    module: str | None = None
    is_generated: bool = False
    is_vendored: bool = False
    parse_error: str | None = None

    def __post_init__(self) -> None:
        _require_key_component(self.path, "file path")
        _require_text(self.content_hash, "content_hash")
        _require_non_negative(self.size_bytes, "size_bytes")
        _require_non_negative(self.loc, "loc")
        object.__setattr__(self, "language", _coerce_enum(Language, self.language, "language"))

    @property
    def parse_ok(self) -> bool:
        """Whether the file parsed without recorded errors."""

        return self.parse_error is None


@dataclass(frozen=True, slots=True)
class ClassRecord:
    """A class, interface, trait, enum or record declared in one file."""

    file_path: str
    name: str
    qualified_name: str
    span: Span
    kind: ClassKind = ClassKind.CLASS
    extends: str | None = None
    implements: tuple[str, ...] = ()
    modifiers: tuple[str, ...] = ()
    annotations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_key_component(self.file_path, "class file_path")
        _require_text(self.name, "class name")
        _require_key_component(self.qualified_name, "class qualified_name")
        object.__setattr__(self, "kind", _coerce_enum(ClassKind, self.kind, "class kind"))
        for field_name in ("implements", "modifiers", "annotations"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    @property
    def id(self) -> str:
        return class_id(self.file_path, self.qualified_name)


@dataclass(frozen=True, slots=True)
class FunctionRecord:
    """A function, method, closure or script body.

    ``qualified_name`` uses ``::`` as the member separator for every language, e.g.
    ``User::find`` (PHP) or ``com.example.UserRepository::findById`` (Java). Parser
    generated closures must receive a stable synthetic name (for example
    ``{closure@index.php:12:1}``) so keys remain deterministic.
    """

    file_path: str
    name: str
    qualified_name: str
    signature: str
    span: Span
    class_id: str | None = None
    parameters: tuple[Parameter, ...] = ()
    return_type: str | None = None
    visibility: Visibility = Visibility.PUBLIC
    is_static: bool = False
    is_abstract: bool = False
    cyclomatic_complexity: int = 1
    max_nesting: int = 0
    modifiers: tuple[str, ...] = ()
    annotations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_key_component(self.file_path, "function file_path")
        _require_text(self.name, "function name")
        _require_key_component(self.qualified_name, "function qualified_name")
        _require_text(self.signature, "function signature")
        if self.cyclomatic_complexity < 1:
            raise ValueError("cyclomatic_complexity must be >= 1")
        _require_non_negative(self.max_nesting, "max_nesting")
        object.__setattr__(
            self, "visibility", _coerce_enum(Visibility, self.visibility, "visibility")
        )
        object.__setattr__(self, "parameters", tuple(self.parameters))
        for field_name in ("modifiers", "annotations"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    @property
    def id(self) -> str:
        return function_id(self.file_path, self.qualified_name)

    @property
    def loc(self) -> int:
        """Source lines covered by the declaration, taken from ``span``."""

        return self.span.line_count


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """A directed relationship.

    ``src`` and ``dst`` are unit keys (file paths, ``fn:`` or ``class:`` keys). When an
    edge cannot be resolved, ``resolved`` is ``False`` and ``dst`` plus ``raw`` retain
    the source reference verbatim instead of guessing a target.
    """

    src: str
    dst: str
    kind: EdgeKind
    file_path: str
    line: int
    resolved: bool = True
    confidence: Confidence = Confidence.VERIFIED
    raw: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.src, "edge src")
        _require_text(self.dst, "edge dst")
        _require_text(self.file_path, "edge file_path")
        if self.line < 1:
            raise ValueError("edge line must be >= 1")
        object.__setattr__(self, "kind", _coerce_enum(EdgeKind, self.kind, "edge kind"))
        object.__setattr__(
            self, "confidence", _coerce_enum(Confidence, self.confidence, "confidence")
        )


@dataclass(frozen=True, slots=True)
class DataAccessRecord:
    """A SQL statement observed in a function or script."""

    file_path: str
    operation: SqlOperation
    sql: str
    line: int
    function_id: str | None = None
    table_name: str | None = None
    driver: str | None = None
    parameters: tuple[str, ...] = ()
    confidence: Confidence = Confidence.VERIFIED

    def __post_init__(self) -> None:
        _require_text(self.file_path, "data access file_path")
        _require_text(self.sql, "data access sql")
        if self.line < 1:
            raise ValueError("data access line must be >= 1")
        object.__setattr__(
            self, "operation", _coerce_enum(SqlOperation, self.operation, "sql operation")
        )
        object.__setattr__(
            self, "confidence", _coerce_enum(Confidence, self.confidence, "confidence")
        )
        object.__setattr__(self, "parameters", tuple(self.parameters))

    @property
    def is_write(self) -> bool:
        return self.operation in (
            SqlOperation.INSERT,
            SqlOperation.UPDATE,
            SqlOperation.DELETE,
            SqlOperation.REPLACE,
            SqlOperation.MERGE,
            SqlOperation.DDL,
        )


@dataclass(frozen=True, slots=True)
class EntryPointRecord:
    """A way into the application: route, servlet, action, JSP, CLI script or cron job."""

    file_path: str
    kind: EntryKind
    path: str
    line: int
    function_id: str | None = None
    handler_ref: str | None = None
    http_method: HttpMethod | None = None
    framework: str | None = None
    requires_auth: bool | None = None
    auth_hint: str | None = None
    confidence: Confidence = Confidence.VERIFIED

    def __post_init__(self) -> None:
        _require_text(self.file_path, "entry point file_path")
        _require_text(self.path, "entry point path")
        if "\n" in self.path or "\r" in self.path:
            raise ValueError("entry point path must not contain a newline")
        if self.handler_ref is not None and ("\n" in self.handler_ref or "\r" in self.handler_ref):
            raise ValueError("entry point handler_ref must not contain a newline")
        if self.line < 1:
            raise ValueError("entry point line must be >= 1")
        object.__setattr__(self, "kind", _coerce_enum(EntryKind, self.kind, "entry kind"))
        if self.http_method is not None:
            object.__setattr__(
                self,
                "http_method",
                _coerce_enum(HttpMethod, self.http_method, "http method"),
            )
        object.__setattr__(
            self, "confidence", _coerce_enum(Confidence, self.confidence, "confidence")
        )

    @property
    def citation(self) -> str:
        return entry_citation(self.kind, self.http_method, self.path)


@dataclass(frozen=True, slots=True)
class RequestInputRecord:
    """A user-controlled value read from an HTTP request.

    ``name`` is the parameter, header, cookie, file-field or path key when the source
    expression determines it, and ``<dynamic>`` otherwise. ``raw`` keeps the original
    expression so dynamic reads stay inspectable.
    """

    file_path: str
    kind: RequestInputKind
    name: str
    line: int
    function_id: str | None = None
    framework: str | None = None
    raw: str | None = None
    confidence: Confidence = Confidence.VERIFIED

    def __post_init__(self) -> None:
        _require_text(self.file_path, "request input file_path")
        _require_text(self.name, "request input name")
        if "\n" in self.name or "\r" in self.name:
            raise ValueError("request input name must not contain a newline")
        if self.line < 1:
            raise ValueError("request input line must be >= 1")
        object.__setattr__(
            self, "kind", _coerce_enum(RequestInputKind, self.kind, "request input kind")
        )
        object.__setattr__(
            self, "confidence", _coerce_enum(Confidence, self.confidence, "confidence")
        )


@dataclass(frozen=True, slots=True)
class SideEffectRecord:
    """An externally visible effect performed by a function."""

    file_path: str
    kind: SideEffectKind
    detail: str
    line: int
    function_id: str | None = None
    sink: str | None = None
    confidence: Confidence = Confidence.VERIFIED

    def __post_init__(self) -> None:
        _require_text(self.file_path, "side effect file_path")
        _require_text(self.detail, "side effect detail")
        if self.line < 1:
            raise ValueError("side effect line must be >= 1")
        object.__setattr__(
            self, "kind", _coerce_enum(SideEffectKind, self.kind, "side effect kind")
        )
        object.__setattr__(
            self, "confidence", _coerce_enum(Confidence, self.confidence, "confidence")
        )


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """A statement about the code, ready for verification and publication."""

    subject: str
    kind: ClaimKind
    text: str
    status: Confidence = Confidence.INFERRED
    citations: tuple[str, ...] = ()
    origin: ClaimOrigin = ClaimOrigin.STATIC
    model: str | None = None
    prompt_hash: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.subject, "claim subject")
        _require_text(self.text, "claim text")
        object.__setattr__(self, "kind", _coerce_enum(ClaimKind, self.kind, "claim kind"))
        object.__setattr__(self, "status", _coerce_enum(Confidence, self.status, "claim status"))
        object.__setattr__(self, "origin", _coerce_enum(ClaimOrigin, self.origin, "claim origin"))
        object.__setattr__(self, "citations", tuple(self.citations))
        object.__setattr__(self, "created_at", _normalise_datetime(self.created_at))


@dataclass(frozen=True, slots=True)
class FileFacts:
    """All IR facts produced for one file, written atomically by the store.

    Keeping the aggregate explicit means a parser either returns a complete,
    self-consistent picture of a file or raises; the store never persists a half-parsed
    file.
    """

    file: FileRecord
    classes: tuple[ClassRecord, ...] = ()
    functions: tuple[FunctionRecord, ...] = ()
    edges: tuple[EdgeRecord, ...] = ()
    data_access: tuple[DataAccessRecord, ...] = ()
    entry_points: tuple[EntryPointRecord, ...] = ()
    request_inputs: tuple[RequestInputRecord, ...] = ()
    side_effects: tuple[SideEffectRecord, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "classes",
            "functions",
            "edges",
            "data_access",
            "entry_points",
            "request_inputs",
            "side_effects",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

        path = self.file.path
        child_groups = (
            ("class", self.classes),
            ("function", self.functions),
            ("edge", self.edges),
            ("data access", self.data_access),
            ("entry point", self.entry_points),
            ("request input", self.request_inputs),
            ("side effect", self.side_effects),
        )
        for label, items in child_groups:
            for item in items:
                if item.file_path != path:
                    raise ValueError(
                        f"{label} for {item.file_path!r} cannot be stored with file {path!r}"
                    )

        class_ids = {item.id for item in self.classes}
        if len(class_ids) != len(self.classes):
            raise ValueError(f"duplicate class keys in {path!r}")
        function_ids = {item.id for item in self.functions}
        if len(function_ids) != len(self.functions):
            raise ValueError(f"duplicate function keys in {path!r}")
        for function in self.functions:
            if function.class_id is not None and function.class_id not in class_ids:
                raise ValueError(
                    f"function {function.qualified_name!r} references unknown class "
                    f"{function.class_id!r} in {path!r}"
                )


@dataclass(frozen=True, slots=True)
class CachedCompletion:
    """A cached LLM response; operational data, not a published fact."""

    cache_key: str
    response: str
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime = field(default_factory=utc_now)
    last_used_at: datetime = field(default_factory=utc_now)
    hits: int = 0

    def __post_init__(self) -> None:
        _require_text(self.cache_key, "cache_key")
        _require_non_negative(self.prompt_tokens, "prompt_tokens")
        _require_non_negative(self.completion_tokens, "completion_tokens")
        _require_non_negative(self.hits, "hits")
        if self.cost_usd < 0:
            raise ValueError("cost_usd must be >= 0")
        object.__setattr__(self, "created_at", _normalise_datetime(self.created_at))
        object.__setattr__(self, "last_used_at", _normalise_datetime(self.last_used_at))


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Aggregate cache usage for evaluation reports."""

    entries: int = 0
    hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
