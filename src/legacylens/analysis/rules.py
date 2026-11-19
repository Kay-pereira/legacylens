"""Deterministic heuristics for dead code and business-rule candidates.

Both analyses are deliberately conservative. They never execute the application, never
call an LLM, and never promote a guess to a fact:

* **Dead code** follows the project-resolved call graph from entry points. Only
  non-public methods and top-level functions are reported; public/protected methods,
  abstract/interface/trait members, constructors, magic methods and ``main`` are treated
  as API or framework surface and excluded. A function referenced by an unresolved call
  edge is suppressed too, because the missing caller may be the only reason it looks
  unreachable.
* **Business-rule candidates** are mined from four kinds of deterministic evidence:
  conditions in PHP/Java source (``if``/``elseif``/``while``), calls to known validation
  or authorisation APIs, validation/authorisation annotations already stored in the IR,
  and entry points that record an authentication requirement.

Source is re-read through the repository-relative paths in the IR. That is the documented
way for consumers to see bodies without storing them in the store; only PHP and Java files
are scanned. Every candidate carries the exact evidence, a citation and a confidence
label. Interpretation stays ``inferred`` even when the underlying expression is observed,
except for an explicit entry-point auth mapping, which keeps its stored label.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import networkx as nx

from legacylens.ir.models import (
    ClassKind,
    ClassRecord,
    Confidence,
    EdgeKind,
    EdgeRecord,
    EntryPointRecord,
    FileRecord,
    FunctionRecord,
    Language,
    Visibility,
)
from legacylens.ir.store import FactStore

from .entrypoints import EntryPointsReport, build_entrypoints
from .graph import build_graphs

__all__ = [
    "BusinessRuleReport",
    "DeadCodeCandidate",
    "DeadCodeReport",
    "RuleCandidate",
    "RuleKind",
    "RuleSource",
    "build_business_rules",
    "build_dead_code",
]

_CALL_KINDS = frozenset({EdgeKind.CALL, EdgeKind.INSTANTIATE})
_MAGIC_METHODS = frozenset(
    {
        "__call",
        "__callstatic",
        "__clone",
        "__construct",
        "__debuginfo",
        "__destruct",
        "__get",
        "__invoke",
        "__isset",
        "__serialize",
        "__set",
        "__set_state",
        "__sleep",
        "__tostring",
        "__unserialize",
        "__unset",
        "__wakeup",
    }
)
_NON_CONCRETE_OWNERS = frozenset({ClassKind.INTERFACE, ClassKind.TRAIT})
_PUBLIC_VISIBILITIES = frozenset({Visibility.PUBLIC, Visibility.PROTECTED})
_SCANNED_LANGUAGES = frozenset({Language.PHP, Language.JAVA})


# ---------------------------------------------------------------------------
# Dead code
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeadCodeCandidate:
    """A function the entry-point-driven call graph cannot reach."""

    function_id: str
    file_path: str
    qualified_name: str
    name: str
    start_line: int
    end_line: int
    loc: int
    cyclomatic_complexity: int
    visibility: Visibility
    incoming_callers: tuple[str, ...]
    reason: str
    confidence: Confidence = Confidence.INFERRED

    def as_dict(self) -> dict[str, object]:
        return {
            "function_id": self.function_id,
            "file_path": self.file_path,
            "qualified_name": self.qualified_name,
            "name": self.name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "loc": self.loc,
            "cyclomatic_complexity": self.cyclomatic_complexity,
            "visibility": self.visibility.value,
            "incoming_callers": list(self.incoming_callers),
            "reason": self.reason,
            "confidence": self.confidence.value,
        }


@dataclass(frozen=True, slots=True)
class DeadCodeReport:
    """Dead-code candidates plus the conservative exclusions that produced them."""

    candidates: tuple[DeadCodeCandidate, ...]
    functions: int
    reachable_functions: int
    entry_points: int
    skipped_reason: str | None = None
    excluded: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "functions": self.functions,
            "reachable_functions": self.reachable_functions,
            "entry_points": self.entry_points,
            "dead_code": len(self.candidates),
            "skipped_reason": self.skipped_reason,
            "excluded": dict(self.excluded),
            "candidates": [item.as_dict() for item in self.candidates],
        }


def build_dead_code(
    store: FactStore,
    *,
    include_generated: bool = False,
    include_vendored: bool = False,
) -> DeadCodeReport:
    """Report functions unreachable from any entry point in the resolved call graph.

    Report-only heuristics must not turn missing call edges into confident claims. When no
    entry point exists, reachability cannot be established and the report is empty with
    ``skipped_reason`` set instead of flagging every function.
    """

    functions = store.list_functions()
    entries = build_entrypoints(store)
    if not entries.entries:
        return DeadCodeReport(
            candidates=(),
            functions=len(functions),
            reachable_functions=0,
            entry_points=0,
            skipped_reason="no entry points detected" if functions else None,
        )

    graphs = build_graphs(store)
    classes = {item.id: item for item in store.list_classes()}
    files = {item.path: item for item in store.list_files()}
    roots = _entry_roots(entries)
    roots = _reachable(graphs.includes, roots)
    reachable = _reachable(graphs.calls, roots)
    unresolved_names = _unresolved_call_names(store.list_edges(resolved=False))

    excluded: Counter[str] = Counter()
    candidates: list[DeadCodeCandidate] = []
    for function in functions:
        if function.id in reachable:
            continue
        exclusion = _exclusion(
            function,
            classes,
            files,
            include_generated=include_generated,
            include_vendored=include_vendored,
        )
        if exclusion is not None:
            excluded[exclusion] += 1
            continue
        if function.name in unresolved_names:
            excluded["unresolved_caller"] += 1
            continue
        incoming = _incoming_callers(graphs.calls, function.id)
        candidates.append(
            DeadCodeCandidate(
                function_id=function.id,
                file_path=function.file_path,
                qualified_name=function.qualified_name,
                name=function.name,
                start_line=function.span.start_line,
                end_line=function.span.end_line,
                loc=function.loc,
                cyclomatic_complexity=function.cyclomatic_complexity,
                visibility=function.visibility,
                incoming_callers=incoming,
                reason=(
                    "no resolved callers"
                    if not incoming
                    else "callers are not reachable from an entry point"
                ),
            )
        )

    candidates.sort(key=lambda item: (item.file_path, item.start_line, item.qualified_name))
    return DeadCodeReport(
        candidates=tuple(candidates),
        functions=len(functions),
        reachable_functions=sum(1 for item in functions if item.id in reachable),
        entry_points=len(entries.entries),
        excluded=tuple(sorted(excluded.items())),
    )


def _entry_roots(entries: EntryPointsReport) -> set[str]:
    roots: set[str] = set()
    for detail in entries.entries:
        entry = detail.entry
        roots.add(entry.file_path)
        roots.update(detail.handlers)
        if entry.function_id is not None:
            roots.add(entry.function_id)
    return roots


def _reachable(graph: nx.DiGraph, roots: Iterable[str]) -> set[str]:
    """Breadth-first closure over a NetworkX graph, ignoring unresolved sentinels."""

    seen: set[str] = set()
    stack = list(dict.fromkeys(item for item in roots if item))
    while stack:
        node = stack.pop()
        if node in seen or node.startswith("unresolved:"):
            continue
        seen.add(node)
        if node in graph:
            stack.extend(graph.successors(node))
    return seen


def _unresolved_call_names(edges: Iterable[EdgeRecord]) -> set[str]:
    names: set[str] = set()
    for edge in edges:
        if edge.kind not in _CALL_KINDS:
            continue
        names.add(_short_reference(edge.dst))
    names.discard("")
    return names


def _short_reference(reference: str) -> str:
    value = " ".join(reference.strip().split())
    value = value.lstrip("\\").lstrip("$")
    if "::" in value:
        value = value.rsplit("::", 1)[1]
    value = re.split(r"->|\.", value)[-1]
    return value.strip()


def _incoming_callers(graph: nx.DiGraph, function_id: str) -> tuple[str, ...]:
    if function_id not in graph:
        return ()
    return tuple(
        sorted(
            node
            for node in graph.predecessors(function_id)
            if not node.startswith("unresolved:")
        )
    )


def _exclusion(
    function: FunctionRecord,
    classes: dict[str, ClassRecord],
    files: dict[str, FileRecord],
    *,
    include_generated: bool,
    include_vendored: bool,
) -> str | None:
    file = files.get(function.file_path)
    if file is not None and file.is_generated and not include_generated:
        return "generated"
    if file is not None and file.is_vendored and not include_vendored:
        return "vendored"
    if function.is_abstract:
        return "abstract"
    if function.name.lower() in _MAGIC_METHODS:
        return "magic"
    if function.name == "main":
        return "entry_like"
    if any(_annotation_name(item) == "Override" for item in function.annotations):
        return "override"

    if function.class_id is None:
        return None

    owner = classes.get(function.class_id)
    if owner is None:
        return None
    if function.name == owner.name:
        return "constructor"
    if owner.kind in _NON_CONCRETE_OWNERS:
        return "non_concrete_owner"
    if function.visibility in _PUBLIC_VISIBILITIES:
        return "public_api"
    return None


# ---------------------------------------------------------------------------
# Business-rule candidates
# ---------------------------------------------------------------------------


class RuleKind(StrEnum):
    """Business-rule family."""

    VALIDATION = "validation"
    THRESHOLD = "threshold"
    AUTHORISATION = "authorisation"


class RuleSource(StrEnum):
    """Where a candidate was observed."""

    CONDITION = "condition"
    CALL = "call"
    ANNOTATION = "annotation"
    ENTRY_POINT = "entry_point"


_KIND_ORDER = {
    RuleKind.AUTHORISATION: 0,
    RuleKind.THRESHOLD: 1,
    RuleKind.VALIDATION: 2,
}


@dataclass(frozen=True, slots=True)
class RuleCandidate:
    """One deterministic business-rule candidate with its evidence and citation."""

    kind: RuleKind
    source: RuleSource
    summary: str
    evidence: str
    subject: str
    file_path: str
    line: int
    function_id: str | None = None
    function_name: str | None = None
    value: str | None = None
    confidence: Confidence = Confidence.INFERRED
    citations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source": self.source.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "subject": self.subject,
            "file_path": self.file_path,
            "line": self.line,
            "function_id": self.function_id,
            "function_name": self.function_name,
            "value": self.value,
            "confidence": self.confidence.value,
            "citations": list(self.citations),
        }


@dataclass(frozen=True, slots=True)
class BusinessRuleReport:
    """Business-rule candidates plus source-scan coverage."""

    candidates: tuple[RuleCandidate, ...]
    files_scanned: int
    files_missing: int
    files_skipped: int

    def as_dict(self) -> dict[str, object]:
        counts = Counter(item.kind for item in self.candidates)
        return {
            "business_rules": len(self.candidates),
            "counts": {kind.value: counts[kind] for kind in _KIND_ORDER},
            "files_scanned": self.files_scanned,
            "files_missing": self.files_missing,
            "files_skipped": self.files_skipped,
            "candidates": [item.as_dict() for item in self.candidates],
        }


def build_business_rules(
    store: FactStore,
    *,
    source_root: str | Path | None = None,
    include_generated: bool = False,
    include_vendored: bool = False,
) -> BusinessRuleReport:
    """Mine deterministic business-rule candidates from the IR and source.

    ``source_root`` is the directory the stored repository-relative paths are resolved
    against. Without it, only IR-only evidence (annotations and entry-point auth) is
    reported; no source file is guessed.
    """

    functions = store.list_functions()
    classes = {item.id: item for item in store.list_classes()}
    candidates: list[RuleCandidate] = []
    candidates.extend(_annotation_candidates(functions, classes))
    candidates.extend(_entry_point_candidates(store.list_entry_points()))

    files_scanned = 0
    files_missing = 0
    files_skipped = 0
    if source_root is not None:
        root = Path(source_root)
        by_file: dict[str, list[FunctionRecord]] = defaultdict(list)
        for function in functions:
            by_file[function.file_path].append(function)
        for file in store.list_files():
            if file.language not in _SCANNED_LANGUAGES:
                continue
            if (file.is_generated and not include_generated) or (
                file.is_vendored and not include_vendored
            ):
                files_skipped += 1
                continue
            path = root / Path(file.path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                files_missing += 1
                continue
            files_scanned += 1
            candidates.extend(
                _source_candidates(
                    file.path,
                    text,
                    by_file.get(file.path, ()),
                    file.language,
                )
            )

    unique: dict[tuple[str, int, str, str, str, str], RuleCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.file_path,
            candidate.line,
            candidate.kind.value,
            candidate.source.value,
            candidate.function_id or "",
            candidate.evidence,
        )
        unique.setdefault(key, candidate)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.file_path,
            item.line,
            _KIND_ORDER[item.kind],
            item.summary,
            item.evidence,
        ),
    )
    return BusinessRuleReport(
        candidates=tuple(ordered),
        files_scanned=files_scanned,
        files_missing=files_missing,
        files_skipped=files_skipped,
    )


# -- IR-only evidence ----------------------------------------------------


_VALIDATION_ANNOTATIONS = frozenset(
    {
        "DecimalMax",
        "DecimalMin",
        "Digits",
        "Email",
        "Future",
        "Max",
        "Min",
        "Negative",
        "NotBlank",
        "NotEmpty",
        "NotNull",
        "Null",
        "Past",
        "Pattern",
        "Positive",
        "Size",
        "Valid",
    }
)
_AUTHORISATION_ANNOTATIONS = frozenset(
    {
        "DenyAll",
        "PermitAll",
        "PreAuthorize",
        "PostAuthorize",
        "RolesAllowed",
        "Secured",
    }
)


def _annotation_candidates(
    functions: Sequence[FunctionRecord],
    classes: dict[str, ClassRecord],
) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    for function in functions:
        for annotation in function.annotations:
            candidate = _annotation_candidate(
                annotation,
                subject=function.id,
                file_path=function.file_path,
                line=function.span.start_line,
                function_id=function.id,
                function_name=function.qualified_name,
            )
            if candidate is not None:
                candidates.append(candidate)
    for class_record in classes.values():
        for annotation in class_record.annotations:
            candidate = _annotation_candidate(
                annotation,
                subject=class_record.id,
                file_path=class_record.file_path,
                line=class_record.span.start_line,
                function_id=None,
                function_name=class_record.qualified_name,
            )
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _annotation_candidate(
    annotation: str,
    *,
    subject: str,
    file_path: str,
    line: int,
    function_id: str | None,
    function_name: str | None,
) -> RuleCandidate | None:
    name = _annotation_name(annotation)
    if name in _AUTHORISATION_ANNOTATIONS:
        kind = RuleKind.AUTHORISATION
        summary = f"Authorisation annotation: @{name}"
    elif name in _VALIDATION_ANNOTATIONS:
        kind = RuleKind.VALIDATION
        summary = f"Validation annotation: @{name}"
    else:
        return None
    return RuleCandidate(
        kind=kind,
        source=RuleSource.ANNOTATION,
        summary=summary,
        evidence=_normalise(annotation),
        subject=subject,
        file_path=file_path,
        line=line,
        function_id=function_id,
        function_name=function_name,
        value=_first_string_literal(annotation) if kind is RuleKind.AUTHORISATION else None,
        citations=_citations(file_path, line, subject),
    )


def _entry_point_candidates(entries: Iterable[EntryPointRecord]) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    for entry in entries:
        if entry.requires_auth is not True:
            continue
        evidence = entry.auth_hint or "requires_auth = true"
        candidates.append(
            RuleCandidate(
                kind=RuleKind.AUTHORISATION,
                source=RuleSource.ENTRY_POINT,
                summary=f"Entry point requires authentication ({entry.path})",
                evidence=_normalise(evidence),
                subject=entry.citation,
                file_path=entry.file_path,
                line=entry.line,
                function_id=entry.function_id,
                value=entry.auth_hint,
                confidence=entry.confidence,
                citations=_citations(entry.file_path, entry.line, entry.citation),
            )
        )
    return candidates


# -- source evidence -----------------------------------------------------


_CONDITION_START = re.compile(r"\b(if|elseif|while)\s*\(", re.IGNORECASE)
_CALL_START = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CALL_KEYWORDS = frozenset(
    {
        "array",
        "case",
        "catch",
        "class",
        "clone",
        "declare",
        "do",
        "echo",
        "eval",
        "exit",
        "extends",
        "finally",
        "for",
        "foreach",
        "function",
        "if",
        "implements",
        "include",
        "include_once",
        "instanceof",
        "interface",
        "list",
        "match",
        "namespace",
        "new",
        "print",
        "require",
        "require_once",
        "return",
        "self",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "trait",
        "try",
        "unset",
        "use",
        "while",
        "yield",
    }
)
_AUTHORISATION_TEXT = re.compile(
    r"\b(?:acl|admin|administrator|auth|authenticated|authorisation|authorization|"
    r"authorised|authorized|can_access|cannot|denied|deny|forbidden|grant|is_admin|"
    r"is_logged_in|is_owner|logged_in|login|logout|permission|permissions|privilege|"
    r"require_auth|requires_auth|role|roles|staff|unauthorised|unauthorized|"
    r"unauthenticated|user_in_role)\b",
    re.IGNORECASE,
)
_THRESHOLD_LEFT = re.compile(
    r"(?<![<>=!])(?P<operator><=|>=|<|>)(?!=)\s*(?P<value>-?\d+(?:\.\d+)?)\b"
)
_THRESHOLD_RIGHT = re.compile(
    r"(?<![\w.])(?P<value2>-?\d+(?:\.\d+)?)\s*(?P<operator2><=|>=|<|>)(?!=)"
)
_VALIDATION_TEXT = re.compile(
    r"\b(?:empty|filter_input|filter_var|in_array|invalid|is_array|is_bool|is_float|"
    r"is_int|is_integer|is_null|is_numeric|is_object|is_string|isset|mb_strlen|null|"
    r"preg_match|preg_match_all|strlen|valid|validate)\b",
    re.IGNORECASE,
)
_AUTHORISATION_CALLS = frozenset(
    {
        "authorise",
        "authorize",
        "canaccess",
        "checkpermission",
        "checkpermissions",
        "denyall",
        "haspermission",
        "haspermissions",
        "hasrole",
        "hasroles",
        "isadmin",
        "isauthorized",
        "isauthorised",
        "isauthenticated",
        "isloggedin",
        "isowner",
        "isuserinrole",
        "permitall",
        "requireauth",
        "requirelogin",
    }
)
_VALIDATION_CALLS = frozenset(
    {
        "array_key_exists",
        "assert",
        "assertfalse",
        "asserttrue",
        "checknotnull",
        "ctype_alnum",
        "ctype_alpha",
        "ctype_digit",
        "filter_input",
        "filter_var",
        "in_array",
        "is_array",
        "is_bool",
        "is_float",
        "is_int",
        "is_integer",
        "is_null",
        "is_numeric",
        "is_object",
        "is_string",
        "isblank",
        "isempty",
        "isnotblank",
        "isnotempty",
        "isvalid",
        "mb_strlen",
        "notblank",
        "preg_match",
        "preg_match_all",
        "require_non_null",
        "requirenonnull",
        "strlen",
        "validate",
        "validates",
    }
)


@dataclass(frozen=True, slots=True)
class _FunctionSpan:
    start: int
    end: int
    function: FunctionRecord


def _source_candidates(
    file_path: str,
    text: str,
    functions: Sequence[FunctionRecord],
    language: Language,
) -> list[RuleCandidate]:
    masked = _mask_code(text, language)
    spans = tuple(
        _FunctionSpan(item.span.start_line, item.span.end_line, item)
        for item in functions
    )
    candidates: list[RuleCandidate] = []
    condition_ranges: list[tuple[int, int]] = []

    for match in _CONDITION_START.finditer(masked):
        open_paren = masked.find("(", match.start())
        if open_paren < 0:
            continue
        close_paren = _matching_paren(masked, open_paren)
        if close_paren is None:
            continue
        condition_ranges.append((open_paren, close_paren))
        expression = _normalise(text[open_paren + 1 : close_paren])
        if not expression:
            continue
        line = text.count("\n", 0, match.start()) + 1
        function = _innermost_function(line, spans)
        candidate = _condition_candidate(
            expression,
            file_path=file_path,
            line=line,
            function=function,
        )
        if candidate is not None:
            candidates.append(candidate)

    for match in _CALL_START.finditer(masked):
        start = match.start()
        if any(left <= start <= right for left, right in condition_ranges):
            continue
        name = match.group(1)
        lowered = name.lower()
        if lowered in _CALL_KEYWORDS or _preceded_by_function_keyword(masked, start):
            continue
        open_paren = masked.find("(", start)
        if open_paren < 0:
            continue
        close_paren = _matching_paren(masked, open_paren)
        if close_paren is None:
            continue
        line = text.count("\n", 0, start) + 1
        function = _innermost_function(line, spans)
        if function is not None and function.span.start_line == line and function.name == name:
            continue
        evidence = _normalise(text[start : close_paren + 1])
        candidate = _call_candidate(
            lowered,
            evidence,
            file_path=file_path,
            line=line,
            function=function,
        )
        if candidate is not None:
            candidates.append(candidate)

    return candidates


def _condition_candidate(
    expression: str,
    *,
    file_path: str,
    line: int,
    function: FunctionRecord | None,
) -> RuleCandidate | None:
    kind: RuleKind | None = None
    summary = ""
    value: str | None = None

    if _AUTHORISATION_TEXT.search(expression):
        kind = RuleKind.AUTHORISATION
        summary = "Authorisation condition"
        value = _first_string_literal(expression)
    else:
        threshold = _THRESHOLD_LEFT.search(expression) or _THRESHOLD_RIGHT.search(expression)
        if threshold is not None:
            groups = threshold.groupdict()
            operator = groups.get("operator") or groups.get("operator2") or ""
            number = groups.get("value") or groups.get("value2") or ""
            kind = RuleKind.THRESHOLD
            summary = f"Threshold comparison: {operator} {number}".strip()
            value = f"{operator} {number}".strip()
        elif _VALIDATION_TEXT.search(expression):
            kind = RuleKind.VALIDATION
            summary = "Validation condition"

    if kind is None:
        return None
    return _candidate(
        kind=kind,
        source=RuleSource.CONDITION,
        summary=summary,
        evidence=expression,
        value=value,
        file_path=file_path,
        line=line,
        function=function,
    )


def _call_candidate(
    name: str,
    evidence: str,
    *,
    file_path: str,
    line: int,
    function: FunctionRecord | None,
) -> RuleCandidate | None:
    normalized = name.replace("_", "")
    if normalized in _AUTHORISATION_CALLS:
        kind = RuleKind.AUTHORISATION
        summary = f"Authorisation call: {name}()"
        value = _first_string_literal(_arguments(evidence))
    elif normalized in _VALIDATION_CALLS:
        kind = RuleKind.VALIDATION
        summary = f"Validation call: {name}()"
        value = None
    else:
        return None
    return _candidate(
        kind=kind,
        source=RuleSource.CALL,
        summary=summary,
        evidence=evidence,
        value=value,
        file_path=file_path,
        line=line,
        function=function,
    )


def _candidate(
    *,
    kind: RuleKind,
    source: RuleSource,
    summary: str,
    evidence: str,
    value: str | None,
    file_path: str,
    line: int,
    function: FunctionRecord | None,
) -> RuleCandidate:
    subject = function.id if function is not None else file_path
    return RuleCandidate(
        kind=kind,
        source=source,
        summary=summary,
        evidence=evidence,
        subject=subject,
        file_path=file_path,
        line=line,
        function_id=function.id if function is not None else None,
        function_name=function.qualified_name if function is not None else None,
        value=value,
        citations=_citations(file_path, line, subject),
    )


def _innermost_function(line: int, spans: Sequence[_FunctionSpan]) -> FunctionRecord | None:
    matches = [
        span for span in spans if span.start <= line <= span.end
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: (item.end - item.start, item.start)).function


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _preceded_by_function_keyword(text: str, start: int) -> bool:
    return re.search(r"\bfunction\s*$", text[max(0, start - 24) : start]) is not None


def _mask_code(text: str, language: Language) -> str:
    """Blank comments and string bodies while preserving offsets and newlines."""

    chars = list(text)
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            else:
                chars[index] = " "
        elif block_comment:
            if char == "*" and nxt == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                block_comment = False
                continue
            if char != "\n":
                chars[index] = " "
        elif quote is not None:
            if char == "\\":
                chars[index] = " "
                if index + 1 < len(text):
                    chars[index + 1] = " "
                index += 2
                continue
            if char == quote:
                chars[index] = " "
                quote = None
            elif char != "\n":
                chars[index] = " "
        elif char in {"'", '"'} or (language is Language.PHP and char == "`"):
            chars[index] = " "
            quote = char
        elif char == "/" and nxt == "/":
            chars[index] = chars[index + 1] = " "
            index += 2
            line_comment = True
            continue
        elif char == "/" and nxt == "*":
            chars[index] = chars[index + 1] = " "
            index += 2
            block_comment = True
            continue
        elif language is Language.PHP and char == "#" and nxt != "[":
            chars[index] = " "
            line_comment = True
        index += 1
    return "".join(chars)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _arguments(evidence: str) -> str:
    start = evidence.find("(")
    end = evidence.rfind(")")
    return evidence[start + 1 : end] if start >= 0 and end > start else ""


def _first_string_literal(text: str) -> str | None:
    match = re.search(r"'([^'\\]*(?:\\.[^'\\]*)*)'|\"([^\"\\]*(?:\\.[^\"\\]*)*)\"", text)
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _annotation_name(annotation: str) -> str:
    value = annotation.strip().lstrip("@")
    value = value.split("(", 1)[0].strip()
    value = value.split()[0] if value else ""
    return value.rsplit(".", 1)[-1]


def _citations(file_path: str, line: int, subject: str) -> tuple[str, ...]:
    values = [f"{file_path}:{line}"]
    if subject and subject != file_path and subject not in values:
        values.append(subject)
    return tuple(values)
