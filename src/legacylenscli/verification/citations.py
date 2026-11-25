"""Parse the citation grammar from ``docs/ir-spec.md`` §4.

Citations are deliberately parsed without a store: parsing answers *what kind of evidence*
a claim offers, while :mod:`legacylenscli.verification.verifier` answers whether that evidence
exists in the IR. Keeping the two steps separate means a malformed citation is reported as a
grammar error instead of being silently treated as a missing target.

The grammar is::

    <file-path>
    <file-path>:<line>
    fn:<file-path>#<qualified-name>
    class:<file-path>#<qualified-name>
    table:<table-name>
    entry:<kind>:<method-or-dash>:<path>

File lines are parsed from the right because repository-relative POSIX paths may contain
``:``. Entry paths are parsed with at most three separators for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from legacylenscli.ir import EntryKind, HttpMethod

__all__ = [
    "Citation",
    "CitationError",
    "CitationKind",
    "parse_citation",
]


class CitationError(ValueError):
    """Raised when text does not follow the citation grammar."""


class CitationKind(StrEnum):
    """The evidence a citation points at."""

    FILE = "file"
    FILE_LINE = "file_line"
    FUNCTION = "function"
    CLASS = "class"
    TABLE = "table"
    ENTRY = "entry"


@dataclass(frozen=True, slots=True)
class Citation:
    """A parsed citation with only the fields relevant to its kind."""

    raw: str
    kind: CitationKind
    path: str | None = None
    line: int | None = None
    qualified_name: str | None = None
    table_name: str | None = None
    entry_kind: EntryKind | None = None
    http_method: HttpMethod | None = None
    entry_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "kind": self.kind.value,
            "path": self.path,
            "line": self.line,
            "qualified_name": self.qualified_name,
            "table_name": self.table_name,
            "entry_kind": self.entry_kind.value if self.entry_kind else None,
            "http_method": self.http_method.value if self.http_method else None,
            "entry_path": self.entry_path,
        }


def parse_citation(text: str) -> Citation:
    """Parse one citation or raise :class:`CitationError`.

    Surrounding whitespace is ignored; everything else must follow the grammar exactly.
    A bare path is accepted as a file key (IR spec §4.1), because file identity is a path.
    """

    if not isinstance(text, str):
        raise CitationError("citation must be a string")
    raw = text
    citation = text.strip()
    if not citation:
        raise CitationError("citation must not be empty")
    if "\n" in citation or "\r" in citation:
        raise CitationError("citation must not contain a newline")

    if citation.startswith("fn:"):
        path, name = _key_parts(citation, "fn:")
        return Citation(
            raw=raw,
            kind=CitationKind.FUNCTION,
            path=path,
            qualified_name=name,
        )
    if citation.startswith("class:"):
        path, name = _key_parts(citation, "class:")
        return Citation(
            raw=raw,
            kind=CitationKind.CLASS,
            path=path,
            qualified_name=name,
        )
    if citation.startswith("table:"):
        table = citation[len("table:") :].strip()
        if not table:
            raise CitationError("table citation must name a table")
        if "#" in table:
            raise CitationError("table citation must not contain '#'")
        return Citation(raw=raw, kind=CitationKind.TABLE, table_name=table)
    if citation.startswith("entry:"):
        body = citation[len("entry:") :]
        parts = body.split(":", 2)
        if len(parts) != 3:
            raise CitationError(
                "entry citation must be entry:<kind>:<method-or-dash>:<path>"
            )
        kind_text, method_text, entry_path = parts
        try:
            entry_kind = EntryKind(kind_text)
        except ValueError as exc:
            allowed = ", ".join(repr(item.value) for item in EntryKind)
            raise CitationError(f"entry kind must be one of {allowed}") from exc
        if method_text == "-":
            http_method = None
        else:
            try:
                http_method = HttpMethod(method_text)
            except ValueError as exc:
                allowed = ", ".join(repr(item.value) for item in HttpMethod)
                raise CitationError(
                    f"entry method must be '-' or one of {allowed}"
                ) from exc
        entry_path = entry_path.strip()
        if not entry_path:
            raise CitationError("entry citation must name a path")
        return Citation(
            raw=raw,
            kind=CitationKind.ENTRY,
            entry_kind=entry_kind,
            http_method=http_method,
            entry_path=entry_path,
        )

    if "#" in citation:
        raise CitationError("'#' is reserved for fn: and class: keys")

    path, separator, line_text = citation.rpartition(":")
    if separator and path and line_text.isdigit():
        line = int(line_text)
        if line < 1:
            raise CitationError("citation line must be >= 1")
        return Citation(raw=raw, kind=CitationKind.FILE_LINE, path=path, line=line)

    return Citation(raw=raw, kind=CitationKind.FILE, path=citation)


def _key_parts(citation: str, prefix: str) -> tuple[str, str]:
    body = citation[len(prefix) :]
    path, separator, name = body.partition("#")
    if not separator or not path or not name:
        raise CitationError(f"{prefix.rstrip(':')} citation must be <path>#<name>")
    if "#" in name:
        raise CitationError("qualified name must not contain '#'")
    if not path.strip() or not name.strip():
        raise CitationError(f"{prefix.rstrip(':')} citation must be <path>#<name>")
    return path, name
