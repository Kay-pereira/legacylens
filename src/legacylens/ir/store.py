"""SQLite fact store: writes parser output and serves typed analysis queries.

The store is the contract between pipeline stages. It owns the connection, applies
``schema.sql``, replaces one file's facts atomically, and returns the dataclasses from
:mod:`legacylens.ir.models` — callers never hand-build rows.

Typical use::

    with FactStore(".legacylens/legacylens.db") as store:
        store.replace_file(parse_php(path, source))
        for function in store.list_functions(file_path=path):
            ...

Re-analysing a file is safe: ``replace_file`` deletes the old ``files`` row, lets foreign
keys cascade to every child fact, and inserts the new facts in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from .models import (
    CachedCompletion,
    CacheStats,
    ClaimKind,
    ClaimOrigin,
    ClaimRecord,
    ClassKind,
    ClassRecord,
    Confidence,
    DataAccessRecord,
    EdgeKind,
    EdgeRecord,
    EntryKind,
    EntryPointRecord,
    FileFacts,
    FileRecord,
    FunctionRecord,
    HttpMethod,
    Language,
    Parameter,
    RequestInputKind,
    RequestInputRecord,
    SideEffectKind,
    SideEffectRecord,
    Span,
    SqlOperation,
    Visibility,
    utc_now,
)

__all__ = [
    "FactStore",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "StoreSummary",
]

SCHEMA_VERSION = 2

_INSERT_FILE = """
INSERT INTO files (
    path, language, content_hash, size_bytes, loc, package, module,
    is_generated, is_vendored, parse_error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_CLASS = """
INSERT INTO classes (
    id, file_path, name, qualified_name, kind, extends, implements,
    span_start_line, span_end_line, span_start_column, span_end_column,
    modifiers, annotations
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_FUNCTION = """
INSERT INTO functions (
    id, file_path, class_id, name, qualified_name, signature, parameters, return_type,
    visibility, is_static, is_abstract,
    span_start_line, span_end_line, span_start_column, span_end_column,
    loc, cyclomatic_complexity, max_nesting, modifiers, annotations
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EDGE = """
INSERT INTO edges (src, dst, kind, file_path, line, resolved, confidence, raw)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_DATA_ACCESS = """
INSERT INTO data_access (
    function_id, file_path, operation, table_name, sql, line, driver, parameters,
    confidence
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ENTRY_POINT = """
INSERT INTO entry_points (
    kind, file_path, function_id, handler_ref, http_method, path, framework, line,
    requires_auth, auth_hint, confidence
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_REQUEST_INPUT = """
INSERT INTO request_inputs (
    kind, file_path, function_id, name, framework, line, raw, confidence
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_SIDE_EFFECT = """
INSERT INTO side_effects (function_id, file_path, kind, detail, sink, line, confidence)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_CLAIM = """
INSERT INTO claims (
    subject, kind, text, status, citations, origin, model, prompt_hash, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_CACHE = """
INSERT INTO llm_cache (
    cache_key, response, provider, model, prompt_hash, prompt_tokens,
    completion_tokens, cost_usd, created_at, last_used_at, hits
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (cache_key) DO UPDATE SET
    response = excluded.response,
    provider = excluded.provider,
    model = excluded.model,
    prompt_hash = excluded.prompt_hash,
    prompt_tokens = excluded.prompt_tokens,
    completion_tokens = excluded.completion_tokens,
    cost_usd = excluded.cost_usd,
    last_used_at = excluded.last_used_at
"""

_CONFIDENCE_COLUMNS = {
    "edges": "confidence",
    "data_access": "confidence",
    "entry_points": "confidence",
    "request_inputs": "confidence",
    "side_effects": "confidence",
    "claims": "status",
}


class SchemaVersionError(RuntimeError):
    """Raised when a database was created by an incompatible schema version."""


@dataclass(frozen=True, slots=True)
class StoreSummary:
    """Row counts for an inventory overview."""

    files: int = 0
    classes: int = 0
    functions: int = 0
    edges: int = 0
    unresolved_edges: int = 0
    data_access: int = 0
    entry_points: int = 0
    request_inputs: int = 0
    side_effects: int = 0
    claims: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the summary as a plain dictionary for JSON reports."""

        return {
            "files": self.files,
            "classes": self.classes,
            "functions": self.functions,
            "edges": self.edges,
            "unresolved_edges": self.unresolved_edges,
            "data_access": self.data_access,
            "entry_points": self.entry_points,
            "request_inputs": self.request_inputs,
            "side_effects": self.side_effects,
            "claims": self.claims,
        }


@cache
def _schema_sql() -> str:
    """Read the packaged schema once per process."""

    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def _enum(value: Any) -> Any:
    return value.value if isinstance(value, StrEnum) else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_list(value: str) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(value))


def _json_parameters(value: str) -> tuple[Parameter, ...]:
    return tuple(Parameter.from_dict(item) for item in json.loads(value))


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _where(conditions: Sequence[str]) -> str:
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


class FactStore:
    """A project database holding the IR plus the LLM completion cache.

    The store is not thread-safe; open one instance per thread or process. The underlying
    file uses WAL so concurrent readers in other processes remain possible.
    """

    def __init__(self, path: str | Path = ":memory:", *, create: bool = True) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(self.path, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        if create:
            try:
                self.initialize()
            except BaseException:
                self.close()
                raise

    # -- lifecycle ---------------------------------------------------------

    def _apply_pragmas(self) -> None:
        connection = self._require_connection()
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")

    def initialize(self) -> None:
        """Create the schema or verify the database's schema version."""

        connection = self._require_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.executescript(_schema_sql())
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            return
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database {self.path!r} uses schema version {version}; "
                f"this build expects {SCHEMA_VERSION}. Re-run analysis to rebuild it."
            )

    def close(self) -> None:
        """Close the connection; safe to call more than once."""

        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Escape hatch for read-only queries not covered by the typed helpers."""

        return self._require_connection()

    def _require_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("FactStore is closed")
        return self._conn

    def __enter__(self) -> FactStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        connection = self._require_connection()
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    # -- writes ------------------------------------------------------------

    def replace_file(self, facts: FileFacts) -> None:
        """Replace every fact belonging to ``facts.file.path`` atomically.

        Deleting the ``files`` row cascades to classes, functions, edges, data access,
        entry points and side effects, so stale facts cannot survive a re-parse.
        """

        file = facts.file
        with self._transaction():
            self._conn = self._require_connection()
            self._conn.execute("DELETE FROM files WHERE path = ?", (file.path,))
            self._conn.execute(
                _INSERT_FILE,
                (
                    file.path,
                    _enum(file.language),
                    file.content_hash,
                    file.size_bytes,
                    file.loc,
                    file.package,
                    file.module,
                    int(file.is_generated),
                    int(file.is_vendored),
                    file.parse_error,
                ),
            )
            self._conn.executemany(
                _INSERT_CLASS,
                [
                    (
                        item.id,
                        item.file_path,
                        item.name,
                        item.qualified_name,
                        _enum(item.kind),
                        item.extends,
                        _json(list(item.implements)),
                        item.span.start_line,
                        item.span.end_line,
                        item.span.start_column,
                        item.span.end_column,
                        _json(list(item.modifiers)),
                        _json(list(item.annotations)),
                    )
                    for item in facts.classes
                ],
            )
            self._conn.executemany(
                _INSERT_FUNCTION,
                [
                    (
                        item.id,
                        item.file_path,
                        item.class_id,
                        item.name,
                        item.qualified_name,
                        item.signature,
                        _json([parameter.as_dict() for parameter in item.parameters]),
                        item.return_type,
                        _enum(item.visibility),
                        int(item.is_static),
                        int(item.is_abstract),
                        item.span.start_line,
                        item.span.end_line,
                        item.span.start_column,
                        item.span.end_column,
                        item.loc,
                        item.cyclomatic_complexity,
                        item.max_nesting,
                        _json(list(item.modifiers)),
                        _json(list(item.annotations)),
                    )
                    for item in facts.functions
                ],
            )
            self._conn.executemany(
                _INSERT_EDGE,
                [
                    (
                        item.src,
                        item.dst,
                        _enum(item.kind),
                        item.file_path,
                        item.line,
                        int(item.resolved),
                        _enum(item.confidence),
                        item.raw,
                    )
                    for item in facts.edges
                ],
            )
            self._conn.executemany(
                _INSERT_DATA_ACCESS,
                [
                    (
                        item.function_id,
                        item.file_path,
                        _enum(item.operation),
                        item.table_name,
                        item.sql,
                        item.line,
                        item.driver,
                        _json(list(item.parameters)),
                        _enum(item.confidence),
                    )
                    for item in facts.data_access
                ],
            )
            self._conn.executemany(
                _INSERT_ENTRY_POINT,
                [
                    (
                        _enum(item.kind),
                        item.file_path,
                        item.function_id,
                        item.handler_ref,
                        _enum(item.http_method) if item.http_method is not None else None,
                        item.path,
                        item.framework,
                        item.line,
                        None if item.requires_auth is None else int(item.requires_auth),
                        item.auth_hint,
                        _enum(item.confidence),
                    )
                    for item in facts.entry_points
                ],
            )
            self._conn.executemany(
                _INSERT_REQUEST_INPUT,
                [
                    (
                        _enum(item.kind),
                        item.file_path,
                        item.function_id,
                        item.name,
                        item.framework,
                        item.line,
                        item.raw,
                        _enum(item.confidence),
                    )
                    for item in facts.request_inputs
                ],
            )
            self._conn.executemany(
                _INSERT_SIDE_EFFECT,
                [
                    (
                        item.function_id,
                        item.file_path,
                        _enum(item.kind),
                        item.detail,
                        item.sink,
                        item.line,
                        _enum(item.confidence),
                    )
                    for item in facts.side_effects
                ],
            )

    def delete_file(self, path: str) -> bool:
        """Delete a file and cascade its facts. Returns ``True`` if a row was removed."""

        with self._transaction():
            cursor = self._require_connection().execute("DELETE FROM files WHERE path = ?", (path,))
        return cursor.rowcount > 0

    # -- file reads --------------------------------------------------------

    def get_file(self, path: str) -> FileRecord | None:
        row = (
            self._require_connection()
            .execute("SELECT * FROM files WHERE path = ?", (path,))
            .fetchone()
        )
        return None if row is None else _file_from_row(row)

    def list_files(
        self,
        *,
        language: Language | str | None = None,
        generated: bool | None = None,
        vendored: bool | None = None,
    ) -> list[FileRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if language is not None:
            conditions.append("language = ?")
            parameters.append(_enum(language))
        if generated is not None:
            conditions.append("is_generated = ?")
            parameters.append(int(generated))
        if vendored is not None:
            conditions.append("is_vendored = ?")
            parameters.append(int(vendored))
        sql = f"SELECT * FROM files{_where(conditions)} ORDER BY path"
        rows = self._require_connection().execute(sql, parameters)
        return [_file_from_row(row) for row in rows]

    # -- class reads -------------------------------------------------------

    def get_class(self, key: str) -> ClassRecord | None:
        row = (
            self._require_connection()
            .execute("SELECT * FROM classes WHERE id = ?", (key,))
            .fetchone()
        )
        return None if row is None else _class_from_row(row)

    def find_class(
        self, qualified_name: str, *, file_path: str | None = None
    ) -> ClassRecord | None:
        """Find a class by qualified name; pass ``file_path`` to disambiguate."""

        conditions = ["qualified_name = ?"]
        parameters: list[Any] = [qualified_name]
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        sql = f"SELECT * FROM classes{_where(conditions)} ORDER BY file_path LIMIT 1"
        row = self._require_connection().execute(sql, parameters).fetchone()
        return None if row is None else _class_from_row(row)

    def list_classes(
        self,
        *,
        file_path: str | None = None,
        kind: ClassKind | str | None = None,
        qualified_name: str | None = None,
    ) -> list[ClassRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(_enum(kind))
        if qualified_name is not None:
            conditions.append("qualified_name = ?")
            parameters.append(qualified_name)
        sql = (
            f"SELECT * FROM classes{_where(conditions)} "
            "ORDER BY file_path, span_start_line, qualified_name"
        )
        rows = self._require_connection().execute(sql, parameters)
        return [_class_from_row(row) for row in rows]

    # -- function reads ----------------------------------------------------

    def get_function(self, key: str) -> FunctionRecord | None:
        row = (
            self._require_connection()
            .execute("SELECT * FROM functions WHERE id = ?", (key,))
            .fetchone()
        )
        return None if row is None else _function_from_row(row)

    def find_function(
        self, qualified_name: str, *, file_path: str | None = None
    ) -> FunctionRecord | None:
        """Find a function by qualified name; pass ``file_path`` to disambiguate."""

        conditions = ["qualified_name = ?"]
        parameters: list[Any] = [qualified_name]
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        sql = f"SELECT * FROM functions{_where(conditions)} ORDER BY file_path LIMIT 1"
        row = self._require_connection().execute(sql, parameters).fetchone()
        return None if row is None else _function_from_row(row)

    def list_functions(
        self,
        *,
        file_path: str | None = None,
        class_id: str | None = None,
        qualified_name: str | None = None,
    ) -> list[FunctionRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        if class_id is not None:
            conditions.append("class_id = ?")
            parameters.append(class_id)
        if qualified_name is not None:
            conditions.append("qualified_name = ?")
            parameters.append(qualified_name)
        sql = (
            f"SELECT * FROM functions{_where(conditions)} "
            "ORDER BY file_path, span_start_line, qualified_name"
        )
        rows = self._require_connection().execute(sql, parameters)
        return [_function_from_row(row) for row in rows]

    # -- edge reads --------------------------------------------------------

    def list_edges(
        self,
        *,
        kind: EdgeKind | str | None = None,
        src: str | None = None,
        dst: str | None = None,
        resolved: bool | None = None,
        file_path: str | None = None,
    ) -> list[EdgeRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(_enum(kind))
        if src is not None:
            conditions.append("src = ?")
            parameters.append(src)
        if dst is not None:
            conditions.append("dst = ?")
            parameters.append(dst)
        if resolved is not None:
            conditions.append("resolved = ?")
            parameters.append(int(resolved))
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        sql = f"SELECT * FROM edges{_where(conditions)} ORDER BY file_path, line, id"
        rows = self._require_connection().execute(sql, parameters)
        return [_edge_from_row(row) for row in rows]

    # -- data access reads -------------------------------------------------

    def list_data_access(
        self,
        *,
        function_id: str | None = None,
        file_path: str | None = None,
        table_name: str | None = None,
        operation: SqlOperation | str | None = None,
    ) -> list[DataAccessRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if function_id is not None:
            conditions.append("function_id = ?")
            parameters.append(function_id)
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        if table_name is not None:
            conditions.append("table_name = ?")
            parameters.append(table_name)
        if operation is not None:
            conditions.append("operation = ?")
            parameters.append(_enum(operation))
        sql = f"SELECT * FROM data_access{_where(conditions)} ORDER BY file_path, line, id"
        rows = self._require_connection().execute(sql, parameters)
        return [_data_access_from_row(row) for row in rows]

    # -- entry point reads -------------------------------------------------

    def list_entry_points(
        self,
        *,
        kind: EntryKind | str | None = None,
        file_path: str | None = None,
        path: str | None = None,
        http_method: HttpMethod | str | None = None,
    ) -> list[EntryPointRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(_enum(kind))
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        if path is not None:
            conditions.append("path = ?")
            parameters.append(path)
        if http_method is not None:
            conditions.append("http_method = ?")
            parameters.append(_enum(http_method))
        sql = f"SELECT * FROM entry_points{_where(conditions)} ORDER BY file_path, line, id"
        rows = self._require_connection().execute(sql, parameters)
        return [_entry_point_from_row(row) for row in rows]

    # -- request input reads -----------------------------------------------

    def list_request_inputs(
        self,
        *,
        kind: RequestInputKind | str | None = None,
        file_path: str | None = None,
        function_id: str | None = None,
        name: str | None = None,
    ) -> list[RequestInputRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(_enum(kind))
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        if function_id is not None:
            conditions.append("function_id = ?")
            parameters.append(function_id)
        if name is not None:
            conditions.append("name = ?")
            parameters.append(name)
        sql = f"SELECT * FROM request_inputs{_where(conditions)} ORDER BY file_path, line, id"
        rows = self._require_connection().execute(sql, parameters)
        return [_request_input_from_row(row) for row in rows]

    # -- side effect reads -------------------------------------------------

    def list_side_effects(
        self,
        *,
        function_id: str | None = None,
        file_path: str | None = None,
        kind: SideEffectKind | str | None = None,
    ) -> list[SideEffectRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if function_id is not None:
            conditions.append("function_id = ?")
            parameters.append(function_id)
        if file_path is not None:
            conditions.append("file_path = ?")
            parameters.append(file_path)
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(_enum(kind))
        sql = f"SELECT * FROM side_effects{_where(conditions)} ORDER BY file_path, line, id"
        rows = self._require_connection().execute(sql, parameters)
        return [_side_effect_from_row(row) for row in rows]

    # -- claims ------------------------------------------------------------

    def add_claim(self, claim: ClaimRecord) -> int:
        """Insert one claim and return its row id."""

        with self._transaction():
            cursor = self._insert_claim(claim)
        return int(cursor.lastrowid)

    def add_claims(self, claims: Iterable[ClaimRecord]) -> list[int]:
        """Insert several claims in one transaction and return their row ids."""

        ids: list[int] = []
        with self._transaction():
            for claim in claims:
                ids.append(int(self._insert_claim(claim).lastrowid))
        return ids

    def replace_claims(self, claims: Iterable[ClaimRecord], *, subject: str | None = None) -> int:
        """Replace claims for ``subject`` (or every claim when ``subject`` is ``None``)."""

        items = tuple(claims)
        if subject is not None and any(claim.subject != subject for claim in items):
            raise ValueError("all replacement claims must match the given subject")
        with self._transaction():
            if subject is None:
                self._require_connection().execute("DELETE FROM claims")
            else:
                self._require_connection().execute(
                    "DELETE FROM claims WHERE subject = ?", (subject,)
                )
            for claim in items:
                self._insert_claim(claim)
        return len(items)

    def _insert_claim(self, claim: ClaimRecord) -> sqlite3.Cursor:
        return self._require_connection().execute(
            _INSERT_CLAIM,
            (
                claim.subject,
                _enum(claim.kind),
                claim.text,
                _enum(claim.status),
                _json(list(claim.citations)),
                _enum(claim.origin),
                claim.model,
                claim.prompt_hash,
                _iso(claim.created_at),
            ),
        )

    def get_claim(self, claim_id: int) -> ClaimRecord | None:
        row = (
            self._require_connection()
            .execute("SELECT * FROM claims WHERE id = ?", (claim_id,))
            .fetchone()
        )
        return None if row is None else _claim_from_row(row)

    def list_claims(
        self,
        *,
        subject: str | None = None,
        kind: ClaimKind | str | None = None,
        status: Confidence | str | None = None,
        origin: ClaimOrigin | str | None = None,
    ) -> list[ClaimRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if subject is not None:
            conditions.append("subject = ?")
            parameters.append(subject)
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(_enum(kind))
        if status is not None:
            conditions.append("status = ?")
            parameters.append(_enum(status))
        if origin is not None:
            conditions.append("origin = ?")
            parameters.append(_enum(origin))
        sql = f"SELECT * FROM claims{_where(conditions)} ORDER BY created_at, id"
        rows = self._require_connection().execute(sql, parameters)
        return [_claim_from_row(row) for row in rows]

    def clear_claims(
        self,
        *,
        subject: str | None = None,
        origin: ClaimOrigin | str | None = None,
    ) -> int:
        """Delete claims matching the filters and return how many were removed."""

        conditions: list[str] = []
        parameters: list[Any] = []
        if subject is not None:
            conditions.append("subject = ?")
            parameters.append(subject)
        if origin is not None:
            conditions.append("origin = ?")
            parameters.append(_enum(origin))
        with self._transaction():
            cursor = self._require_connection().execute(
                f"DELETE FROM claims{_where(conditions)}", parameters
            )
        return cursor.rowcount

    # -- LLM cache ---------------------------------------------------------

    def put_cached_completion(self, entry: CachedCompletion) -> None:
        """Insert or refresh a completion; an existing hit count is preserved."""

        with self._transaction():
            self._require_connection().execute(
                _INSERT_CACHE,
                (
                    entry.cache_key,
                    entry.response,
                    entry.provider,
                    entry.model,
                    entry.prompt_hash,
                    entry.prompt_tokens,
                    entry.completion_tokens,
                    entry.cost_usd,
                    _iso(entry.created_at),
                    _iso(entry.last_used_at),
                    entry.hits,
                ),
            )

    def get_cached_completion(
        self, cache_key: str, *, touch: bool = True
    ) -> CachedCompletion | None:
        """Return a cached completion, incrementing its hit count by default."""

        row = (
            self._require_connection()
            .execute("SELECT * FROM llm_cache WHERE cache_key = ?", (cache_key,))
            .fetchone()
        )
        if row is None:
            return None
        entry = _cache_from_row(row)
        if not touch:
            return entry
        now = utc_now()
        with self._transaction():
            self._require_connection().execute(
                "UPDATE llm_cache SET hits = hits + 1, last_used_at = ? WHERE cache_key = ?",
                (_iso(now), cache_key),
            )
        return replace(entry, hits=entry.hits + 1, last_used_at=now)

    def delete_cached_completion(self, cache_key: str) -> bool:
        with self._transaction():
            cursor = self._require_connection().execute(
                "DELETE FROM llm_cache WHERE cache_key = ?", (cache_key,)
            )
        return cursor.rowcount > 0

    def cache_stats(self) -> CacheStats:
        row = (
            self._require_connection()
            .execute(
                "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                "COALESCE(SUM(cost_usd), 0) AS cost_usd "
                "FROM llm_cache"
            )
            .fetchone()
        )
        return CacheStats(
            entries=int(row["entries"]),
            hits=int(row["hits"]),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            cost_usd=float(row["cost_usd"]),
        )

    def clear_cache(self) -> int:
        with self._transaction():
            cursor = self._require_connection().execute("DELETE FROM llm_cache")
        return cursor.rowcount

    # -- aggregates --------------------------------------------------------

    def summary(self) -> StoreSummary:
        """Return row counts for the whole project."""

        connection = self._require_connection()

        def count(table: str) -> int:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        unresolved = int(
            connection.execute("SELECT COUNT(*) FROM edges WHERE resolved = 0").fetchone()[0]
        )
        return StoreSummary(
            files=count("files"),
            classes=count("classes"),
            functions=count("functions"),
            edges=count("edges"),
            unresolved_edges=unresolved,
            data_access=count("data_access"),
            entry_points=count("entry_points"),
            request_inputs=count("request_inputs"),
            side_effects=count("side_effects"),
            claims=count("claims"),
        )

    def confidence_counts(self) -> dict[str, dict[Confidence, int]]:
        """Return per-table counts for each confidence label.

        Tables without confidence labels (``files``, ``classes``, ``functions``) and the
        cache are omitted. Every table is zero-filled so reports have a stable shape.
        """

        connection = self._require_connection()
        result: dict[str, dict[Confidence, int]] = {
            table: {label: 0 for label in Confidence} for table in _CONFIDENCE_COLUMNS
        }
        for table, column in _CONFIDENCE_COLUMNS.items():
            rows = connection.execute(
                f"SELECT {column} AS label, COUNT(*) AS count FROM {table} GROUP BY {column}"
            )
            for row in rows:
                try:
                    label = Confidence(row["label"])
                except ValueError:  # defensive: ignore rows from a newer schema
                    continue
                result[table][label] = int(row["count"])
        return result


# ---------------------------------------------------------------------------
# Row -> dataclass mapping
# ---------------------------------------------------------------------------


def _span_from_row(row: sqlite3.Row) -> Span:
    return Span(
        start_line=row["span_start_line"],
        end_line=row["span_end_line"],
        start_column=row["span_start_column"],
        end_column=row["span_end_column"],
    )


def _file_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        path=row["path"],
        language=row["language"],
        content_hash=row["content_hash"],
        size_bytes=row["size_bytes"],
        loc=row["loc"],
        package=row["package"],
        module=row["module"],
        is_generated=bool(row["is_generated"]),
        is_vendored=bool(row["is_vendored"]),
        parse_error=row["parse_error"],
    )


def _class_from_row(row: sqlite3.Row) -> ClassRecord:
    return ClassRecord(
        file_path=row["file_path"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        span=_span_from_row(row),
        kind=ClassKind(row["kind"]),
        extends=row["extends"],
        implements=_json_list(row["implements"]),
        modifiers=_json_list(row["modifiers"]),
        annotations=_json_list(row["annotations"]),
    )


def _function_from_row(row: sqlite3.Row) -> FunctionRecord:
    return FunctionRecord(
        file_path=row["file_path"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        signature=row["signature"],
        span=_span_from_row(row),
        class_id=row["class_id"],
        parameters=_json_parameters(row["parameters"]),
        return_type=row["return_type"],
        visibility=Visibility(row["visibility"]),
        is_static=bool(row["is_static"]),
        is_abstract=bool(row["is_abstract"]),
        cyclomatic_complexity=row["cyclomatic_complexity"],
        max_nesting=row["max_nesting"],
        modifiers=_json_list(row["modifiers"]),
        annotations=_json_list(row["annotations"]),
    )


def _edge_from_row(row: sqlite3.Row) -> EdgeRecord:
    return EdgeRecord(
        src=row["src"],
        dst=row["dst"],
        kind=EdgeKind(row["kind"]),
        file_path=row["file_path"],
        line=row["line"],
        resolved=bool(row["resolved"]),
        confidence=Confidence(row["confidence"]),
        raw=row["raw"],
    )


def _data_access_from_row(row: sqlite3.Row) -> DataAccessRecord:
    return DataAccessRecord(
        file_path=row["file_path"],
        operation=SqlOperation(row["operation"]),
        sql=row["sql"],
        line=row["line"],
        function_id=row["function_id"],
        table_name=row["table_name"],
        driver=row["driver"],
        parameters=_json_list(row["parameters"]),
        confidence=Confidence(row["confidence"]),
    )


def _entry_point_from_row(row: sqlite3.Row) -> EntryPointRecord:
    return EntryPointRecord(
        file_path=row["file_path"],
        kind=EntryKind(row["kind"]),
        path=row["path"],
        line=row["line"],
        function_id=row["function_id"],
        handler_ref=row["handler_ref"],
        http_method=None if row["http_method"] is None else HttpMethod(row["http_method"]),
        framework=row["framework"],
        requires_auth=_optional_bool(row["requires_auth"]),
        auth_hint=row["auth_hint"],
        confidence=Confidence(row["confidence"]),
    )


def _request_input_from_row(row: sqlite3.Row) -> RequestInputRecord:
    return RequestInputRecord(
        file_path=row["file_path"],
        kind=RequestInputKind(row["kind"]),
        name=row["name"],
        line=row["line"],
        function_id=row["function_id"],
        framework=row["framework"],
        raw=row["raw"],
        confidence=Confidence(row["confidence"]),
    )


def _side_effect_from_row(row: sqlite3.Row) -> SideEffectRecord:
    return SideEffectRecord(
        file_path=row["file_path"],
        kind=SideEffectKind(row["kind"]),
        detail=row["detail"],
        line=row["line"],
        function_id=row["function_id"],
        sink=row["sink"],
        confidence=Confidence(row["confidence"]),
    )


def _claim_from_row(row: sqlite3.Row) -> ClaimRecord:
    return ClaimRecord(
        subject=row["subject"],
        kind=ClaimKind(row["kind"]),
        text=row["text"],
        status=Confidence(row["status"]),
        citations=_json_list(row["citations"]),
        origin=ClaimOrigin(row["origin"]),
        model=row["model"],
        prompt_hash=row["prompt_hash"],
        created_at=_datetime(row["created_at"]),
        id=row["id"],
    )


def _cache_from_row(row: sqlite3.Row) -> CachedCompletion:
    return CachedCompletion(
        cache_key=row["cache_key"],
        response=row["response"],
        provider=row["provider"],
        model=row["model"],
        prompt_hash=row["prompt_hash"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        cost_usd=row["cost_usd"],
        created_at=_datetime(row["created_at"]),
        last_used_at=_datetime(row["last_used_at"]),
        hits=row["hits"],
    )
