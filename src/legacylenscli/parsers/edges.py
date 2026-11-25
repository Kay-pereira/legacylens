"""Helpers shared by parsers that emit relationship edges.

A parser sees one file at a time, so it can only resolve references that are fully
determined by that file: a literal ``require`` path, a class declared in the same file,
or a method of the declaring class. Everything else is emitted unresolved. For those
edges ``dst`` is a stable reference (a path, a class name or ``Type::member``) and
``raw`` keeps the exact source expression. Dynamic callees use their source text as the
reference.

:func:`resolve_local_edges` applies the same-file part of the resolution rules. The
whole-project pass lives in :mod:`legacylenscli.analysis.graph`, which resolves the
remaining references against the stored IR and keeps anything ambiguous explicit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from legacylenscli.ir.models import (
    ClassRecord,
    Confidence,
    EdgeKind,
    EdgeRecord,
    FunctionRecord,
)

__all__ = [
    "INHERITANCE_KINDS",
    "make_edge",
    "resolve_local_edges",
    "short_name",
    "split_member",
    "trim_type",
]

INHERITANCE_KINDS = frozenset({EdgeKind.EXTENDS, EdgeKind.IMPLEMENTS, EdgeKind.USES_TRAIT})

_TYPE_PREFIXES = ("final ", "readonly ", "var ")


def make_edge(
    *,
    src: str,
    reference: str,
    kind: EdgeKind,
    file_path: str,
    line: int,
    resolved: bool = False,
    raw: str | None = None,
    confidence: Confidence = Confidence.INFERRED,
) -> EdgeRecord:
    """Build one edge, normalising whitespace in the reference.

    ``reference`` must be a stable, minimal form of the target. If a grammar exposes no
    usable name (which should be rare), the raw expression is the fallback so an edge is
    never silently dropped.
    """

    reference = " ".join(reference.split())
    if not reference:
        reference = " ".join((raw or "").split())
    if not reference:
        reference = "<unknown>"
    return EdgeRecord(
        src=src,
        dst=reference,
        kind=kind,
        file_path=file_path,
        line=line,
        resolved=resolved,
        confidence=confidence,
        raw=raw,
    )


def trim_type(text: str) -> str:
    """Reduce a declared type to the class name used for receiver inference.

    Annotations, modifiers, generic arguments and array suffixes are removed. Union and
    intersection types return an empty string because picking one arm would invent a
    receiver type; callers then leave the call unresolved.
    """

    value = " ".join(text.strip().split())
    if not value:
        return ""
    while value.startswith("@"):
        _, _, value = value.partition(" ")
        value = value.strip()
    for prefix in _TYPE_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
    while value.endswith("[]"):
        value = value[:-2]
    if "<" in value:
        value = value.split("<", 1)[0]
    if "&" in value:
        return ""
    if "|" in value:
        arms = [
            arm.strip().lstrip("?\\.")
            for arm in value.split("|")
            if arm.strip().lower() not in {"", "null"}
        ]
        return arms[0] if len(arms) == 1 else ""
    return value.strip().lstrip("?\\.")


def short_name(reference: str) -> str:
    r"""Return the last segment of a class/function reference (``\`` and ``.`` aware)."""

    value = " ".join(reference.strip().split()).lstrip("\\")
    if "::" in value:
        value = value.rsplit("::", 1)[1]
    return value.replace("\\", ".").rsplit(".", 1)[-1]


def split_member(reference: str) -> tuple[str | None, str]:
    """Split ``Type::member`` into its owner and member parts."""

    value = " ".join(reference.strip().split()).lstrip("\\")
    if "::" not in value:
        return None, value
    owner, member = value.rsplit("::", 1)
    return owner.lstrip("\\"), member


def resolve_local_edges(
    edges: tuple[EdgeRecord, ...] | list[EdgeRecord],
    classes: tuple[ClassRecord, ...] | list[ClassRecord],
    functions: tuple[FunctionRecord, ...] | list[FunctionRecord],
) -> tuple[EdgeRecord, ...]:
    """Resolve edges whose target is declared in the same file.

    Anything else is returned unchanged and stays explicit as an unresolved edge.
    """

    class_index = _ClassIndex(classes)
    function_index = _FunctionIndex(functions)
    result: list[EdgeRecord] = []
    for edge in edges:
        if edge.resolved:
            result.append(edge)
            continue
        target: str | None = None
        if edge.kind in INHERITANCE_KINDS:
            target_class = class_index.find(edge.dst)
            if target_class is not None:
                target = target_class.id
        elif edge.kind is EdgeKind.CALL:
            target_function = function_index.find(edge.dst, class_index)
            if target_function is not None:
                target = target_function.id
        if target is None:
            result.append(edge)
        else:
            result.append(
                replace(
                    edge,
                    dst=target,
                    resolved=True,
                    confidence=Confidence.VERIFIED,
                )
            )
    return tuple(result)


class _ClassIndex:
    """Lookup by exact qualified name, then by unique short name."""

    def __init__(self, classes: tuple[ClassRecord, ...] | list[ClassRecord]) -> None:
        self._exact = {item.qualified_name: item for item in classes}
        self._by_short: dict[str, list[ClassRecord]] = defaultdict(list)
        for item in classes:
            self._by_short[item.name].append(item)

    def find(self, reference: str) -> ClassRecord | None:
        value = " ".join(reference.strip().split()).lstrip("\\")
        exact = self._exact.get(value)
        if exact is not None:
            return exact
        candidates = self._by_short.get(short_name(value), ())
        return candidates[0] if len(candidates) == 1 else None


class _FunctionIndex:
    """Lookup by exact qualified name, then by class + member, then by unique name."""

    def __init__(self, functions: tuple[FunctionRecord, ...] | list[FunctionRecord]) -> None:
        self._exact = {item.qualified_name: item for item in functions}
        self._by_name: dict[str, list[FunctionRecord]] = defaultdict(list)
        self._by_owner: dict[tuple[str, str], list[FunctionRecord]] = defaultdict(list)
        for item in functions:
            self._by_name[item.name].append(item)
            if item.class_id is not None:
                self._by_owner[(item.class_id, item.name)].append(item)

    def find(self, reference: str, classes: _ClassIndex) -> FunctionRecord | None:
        value = " ".join(reference.strip().split()).lstrip("\\")
        exact = self._exact.get(value)
        if exact is not None:
            return exact
        owner, member = split_member(value)
        if owner is not None:
            target_class = classes.find(owner)
            if target_class is None:
                return None
            candidates = self._by_owner.get((target_class.id, member), ())
            return candidates[0] if len(candidates) == 1 else None
        candidates = self._by_name.get(short_name(value), ())
        return candidates[0] if len(candidates) == 1 else None
