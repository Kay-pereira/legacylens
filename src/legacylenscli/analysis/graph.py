"""Relationship graphs: includes/imports, inheritance and calls.

Parsers emit syntactic edges and resolve only what the declaring file determines. This
module rebuilds the relationships from the stored IR, resolving the remaining references
against the whole project:

* ``include``/``require`` edges link files, and ``import`` edges link a file to the file
  declaring the imported type;
* ``extends``/``implements``/``uses_trait`` edges link classes;
* ``call`` edges link functions (or a file for top-level script calls).

Resolution is deliberately conservative. A reference is resolved only when exactly one
compatible target exists; ambiguous or dynamic references stay unresolved and are added
to the graphs through explicit ``unresolved:<kind>:<reference>`` nodes. Nothing is
silently dropped or guessed.

Graphs aggregate parallel edges between the same nodes, keeping occurrence counts,
source lines, raw expressions and dangling-target markers. Fan-in/fan-out and cycle
groups are computed from the resolved relationships.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TypeVar

import networkx as nx

from legacylenscli.ir.models import (
    ClassRecord,
    Confidence,
    EdgeKind,
    EdgeRecord,
    FileRecord,
    FunctionRecord,
    Language,
)
from legacylenscli.ir.store import FactStore

__all__ = [
    "GraphSummary",
    "NodeRank",
    "RelationshipGraphs",
    "UnresolvedEdge",
    "build_graphs",
]

_Item = TypeVar("_Item", ClassRecord, FunctionRecord)

_INCLUDE_KINDS = frozenset({EdgeKind.INCLUDE, EdgeKind.REQUIRE, EdgeKind.IMPORT})
_INHERITANCE_KINDS = frozenset({EdgeKind.EXTENDS, EdgeKind.IMPLEMENTS, EdgeKind.USES_TRAIT})
_CALL_KINDS = frozenset({EdgeKind.CALL, EdgeKind.INSTANTIATE})


@dataclass(frozen=True, slots=True)
class UnresolvedEdge:
    """A reference the project-level pass could not resolve to one target."""

    kind: EdgeKind
    src: str
    reference: str
    file_path: str
    line: int
    raw: str | None
    confidence: Confidence

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "kind": self.kind.value,
            "src": self.src,
            "reference": self.reference,
            "file_path": self.file_path,
            "line": self.line,
            "raw": self.raw,
            "confidence": self.confidence.value,
        }


@dataclass(frozen=True, slots=True)
class NodeRank:
    """Fan-in/fan-out for one graph node."""

    node: str
    fan_in: int
    fan_out: int

    def as_dict(self) -> dict[str, str | int]:
        return {"node": self.node, "fan_in": self.fan_in, "fan_out": self.fan_out}


@dataclass(frozen=True, slots=True)
class GraphSummary:
    """JSON-friendly summary of one relationship graph."""

    nodes: int
    edges: int
    resolved_edges: int
    unresolved_edges: int
    dangling_edges: int
    cycles: tuple[tuple[str, ...], ...]
    top_fan_in: tuple[NodeRank, ...]
    top_fan_out: tuple[NodeRank, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "resolved_edges": self.resolved_edges,
            "unresolved_edges": self.unresolved_edges,
            "dangling_edges": self.dangling_edges,
            "cycles": [list(cycle) for cycle in self.cycles],
            "top_fan_in": [item.as_dict() for item in self.top_fan_in],
            "top_fan_out": [item.as_dict() for item in self.top_fan_out],
        }


@dataclass(frozen=True, slots=True)
class RelationshipGraphs:
    """The three NetworkX graphs plus every reference left unresolved."""

    includes: nx.DiGraph
    inheritance: nx.DiGraph
    calls: nx.DiGraph
    unresolved: tuple[UnresolvedEdge, ...]

    def summary(self, *, top_n: int = 10) -> dict[str, object]:
        """Return JSON-friendly summaries; the NetworkX objects stay in memory."""

        return {
            "includes": _summarise(self.includes, top_n=top_n).as_dict(),
            "inheritance": _summarise(self.inheritance, top_n=top_n).as_dict(),
            "calls": _summarise(self.calls, top_n=top_n).as_dict(),
            "unresolved": [item.as_dict() for item in self.unresolved],
        }

    def as_dict(self, *, top_n: int = 10) -> dict[str, object]:
        return self.summary(top_n=top_n)


def build_graphs(store: FactStore) -> RelationshipGraphs:
    """Resolve stored edges against the IR and build the relationship graphs."""

    index = _Index(store.list_files(), store.list_classes(), store.list_functions())
    includes: nx.DiGraph = nx.DiGraph()
    inheritance: nx.DiGraph = nx.DiGraph()
    calls: nx.DiGraph = nx.DiGraph()
    unresolved: list[UnresolvedEdge] = []

    for edge in store.list_edges():
        resolved = _resolve_edge(edge, index)
        if resolved.kind in _INCLUDE_KINDS:
            graph = includes
        elif resolved.kind in _INHERITANCE_KINDS:
            graph = inheritance
        elif resolved.kind in _CALL_KINDS:
            graph = calls
        else:  # pragma: no cover - future edge kinds are ignored explicitly
            continue
        _add_edge(graph, resolved, index)
        if not resolved.resolved:
            unresolved.append(
                UnresolvedEdge(
                    kind=resolved.kind,
                    src=resolved.src,
                    reference=resolved.dst,
                    file_path=resolved.file_path,
                    line=resolved.line,
                    raw=resolved.raw,
                    confidence=resolved.confidence,
                )
            )

    unresolved.sort(
        key=lambda item: (
            item.file_path,
            item.line,
            item.kind.value,
            item.src,
            item.reference,
        )
    )
    return RelationshipGraphs(
        includes=includes,
        inheritance=inheritance,
        calls=calls,
        unresolved=tuple(unresolved),
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class _Index:
    """Whole-project symbol lookup used to finish parser-level references."""

    def __init__(
        self,
        files: Iterable[FileRecord],
        classes: Iterable[ClassRecord],
        functions: Iterable[FunctionRecord],
    ) -> None:
        self.files = {item.path: item for item in files}
        self.classes = tuple(classes)
        self.functions = tuple(functions)
        self._classes_by_qualified: dict[str, list[ClassRecord]] = defaultdict(list)
        self._functions_by_qualified: dict[str, list[FunctionRecord]] = defaultdict(list)
        self._functions_by_owner: dict[tuple[str, str], list[FunctionRecord]] = defaultdict(list)
        for item in self.classes:
            self._classes_by_qualified[item.qualified_name].append(item)
        for item in self.functions:
            self._functions_by_qualified[item.qualified_name].append(item)
            if item.class_id is not None:
                self._functions_by_owner[(item.class_id, item.name)].append(item)
        self.known_nodes = (
            set(self.files)
            | {item.id for item in self.classes}
            | {item.id for item in self.functions}
        )

    def language_of(self, file_path: str) -> Language | None:
        file = self.files.get(file_path)
        return file.language if file is not None else None

    def find_class(self, reference: str, language: Language) -> ClassRecord | None:
        value = _clean_reference(reference)
        candidates = self._classes_by_qualified.get(value, ())
        unique = _compatible(candidates, language, self)
        if unique is not None:
            return unique
        if _is_qualified(value):
            return None
        short = _short_name(value)
        candidates = [
            item
            for item in self.classes
            if item.name == short and self.language_of(item.file_path) is language
        ]
        return candidates[0] if len(candidates) == 1 else None

    def find_function(self, reference: str, language: Language) -> FunctionRecord | None:
        value = _clean_reference(reference)
        candidates = self._functions_by_qualified.get(value, ())
        unique = _compatible(candidates, language, self)
        if unique is not None:
            return unique
        owner, member = _split_member(value)
        if owner is not None:
            target_class = self.find_class(owner, language)
            if target_class is None:
                return None
            matches = self._functions_by_owner.get((target_class.id, member), ())
            return matches[0] if len(matches) == 1 else None
        if _is_qualified(value):
            return None
        short = _short_name(value)
        matches = [
            item
            for item in self.functions
            if item.name == short and self.language_of(item.file_path) is language
        ]
        return matches[0] if len(matches) == 1 else None

    def find_import_target(self, reference: str, language: Language) -> str | None:
        value = _clean_reference(reference)
        if value.endswith(".*") or value == "*":
            return None
        # Import names are fully qualified; a short-name fallback would happily match an
        # unrelated project class with the same simple name.
        candidates = self._classes_by_qualified.get(value, ())
        target = _compatible(candidates, language, self)
        return target.file_path if target is not None else None


def _compatible(
    candidates: Iterable[_Item],
    language: Language,
    index: _Index,
) -> _Item | None:
    matches = [item for item in candidates if index.language_of(item.file_path) is language]
    return matches[0] if len(matches) == 1 else None


def _resolve_edge(edge: EdgeRecord, index: _Index) -> EdgeRecord:
    if edge.resolved:
        return edge
    language = index.language_of(edge.file_path)
    if language is None:
        return edge
    target: str | None = None
    if edge.kind in _INCLUDE_KINDS:
        target = index.find_import_target(edge.dst, language)
    elif edge.kind in _INHERITANCE_KINDS:
        target_class = index.find_class(edge.dst, language)
        target = target_class.id if target_class is not None else None
    elif edge.kind in _CALL_KINDS:
        target_function = index.find_function(edge.dst, language)
        target = target_function.id if target_function is not None else None
    if target is None:
        return edge
    return replace(
        edge,
        dst=target,
        resolved=True,
        confidence=Confidence.VERIFIED,
    )


def _clean_reference(reference: str) -> str:
    value = " ".join(reference.strip().split())
    if value.startswith("static "):
        value = value[len("static ") :]
    value = value.lstrip("\\")
    if "<" in value:
        value = value.split("<", 1)[0]
    return value.strip()


def _short_name(reference: str) -> str:
    value = _clean_reference(reference)
    if "::" in value:
        value = value.rsplit("::", 1)[1]
    return value.replace("\\", ".").rsplit(".", 1)[-1]


def _is_qualified(reference: str) -> bool:
    return "\\" in reference or "." in reference


def _split_member(reference: str) -> tuple[str | None, str]:
    value = _clean_reference(reference)
    if "::" not in value:
        return None, value
    owner, member = value.rsplit("::", 1)
    return owner.lstrip("\\"), member


# ---------------------------------------------------------------------------
# Graph assembly and summaries
# ---------------------------------------------------------------------------


def _add_edge(graph: nx.DiGraph, edge: EdgeRecord, index: _Index) -> None:
    src_external = edge.src not in index.known_nodes
    graph.add_node(edge.src, kind=_node_kind(edge.src), external=src_external)
    if edge.resolved:
        dst = edge.dst
        dangling = dst not in index.known_nodes
        graph.add_node(dst, kind=_node_kind(dst), external=dangling)
        _merge_edge(
            graph,
            edge.src,
            dst,
            edge,
            resolved=True,
            dangling=dangling,
        )
        return
    sentinel = f"unresolved:{edge.kind.value}:{edge.dst}"
    graph.add_node(sentinel, kind="unresolved", label=edge.dst, external=False)
    _merge_edge(
        graph,
        edge.src,
        sentinel,
        edge,
        resolved=False,
        dangling=False,
    )


def _merge_edge(
    graph: nx.DiGraph,
    src: str,
    dst: str,
    edge: EdgeRecord,
    *,
    resolved: bool,
    dangling: bool,
) -> None:
    if graph.has_edge(src, dst):
        data = graph[src][dst]
        data["count"] += 1
        data["resolved_count"] += int(resolved)
        data["unresolved_count"] += int(not resolved)
        data["dangling"] = data["dangling"] or dangling
        data["kinds"] = tuple(sorted({*data["kinds"], edge.kind.value}))
        data["lines"] = (*data["lines"], edge.line)
        if edge.raw:
            data["raws"] = (*data["raws"], edge.raw)
        data["resolved"] = data["unresolved_count"] == 0
        return
    graph.add_edge(
        src,
        dst,
        count=1,
        resolved=resolved,
        resolved_count=int(resolved),
        unresolved_count=int(not resolved),
        dangling=dangling,
        kinds=(edge.kind.value,),
        lines=(edge.line,),
        raws=((edge.raw,) if edge.raw else ()),
        confidence=edge.confidence.value,
        file_path=edge.file_path,
    )


def _summarise(graph: nx.DiGraph, *, top_n: int) -> GraphSummary:
    if top_n < 0:
        raise ValueError("top_n must be >= 0")
    resolved_edges = sum(data["resolved_count"] for _, _, data in graph.edges(data=True))
    unresolved_edges = sum(data["unresolved_count"] for _, _, data in graph.edges(data=True))
    dangling_edges = sum(
        data["count"] for _, _, data in graph.edges(data=True) if data.get("dangling")
    )
    ranks = [
        NodeRank(node=node, fan_in=graph.in_degree(node), fan_out=graph.out_degree(node))
        for node, data in graph.nodes(data=True)
        if data.get("kind") != "unresolved"
    ]
    return GraphSummary(
        nodes=graph.number_of_nodes(),
        edges=graph.number_of_edges(),
        resolved_edges=resolved_edges,
        unresolved_edges=unresolved_edges,
        dangling_edges=dangling_edges,
        cycles=_cycle_groups(graph),
        top_fan_in=tuple(
            sorted(ranks, key=lambda item: (-item.fan_in, -item.fan_out, item.node))[:top_n]
        ),
        top_fan_out=tuple(
            sorted(ranks, key=lambda item: (-item.fan_out, -item.fan_in, item.node))[:top_n]
        ),
    )


def _cycle_groups(graph: nx.DiGraph) -> tuple[tuple[str, ...], ...]:
    """Return strongly connected components that contain a cycle."""

    groups: list[tuple[str, ...]] = []
    for component in nx.strongly_connected_components(graph):
        if len(component) > 1 or any(graph.has_edge(node, node) for node in component):
            groups.append(tuple(sorted(component)))
    return tuple(sorted(groups))


def _node_kind(node: str) -> str:
    if node.startswith("fn:"):
        return "function"
    if node.startswith("class:"):
        return "class"
    if node.startswith("unresolved:"):
        return "unresolved"
    return "file"
