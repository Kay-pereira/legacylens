"""Deterministic Mermaid diagrams for generated documentation.

Diagrams are documentation artefacts, not analyses: every node and edge is taken from an
already-resolved analysis report. Large graphs are bounded by node and edge budgets so a
site build stays predictable; the second element of each renderer's return value tells the
caller whether the diagram was cut down, so the page can say so instead of pretending the
diagram is complete.
"""

from __future__ import annotations

from collections.abc import Iterable

import networkx as nx

from legacylens.analysis.crud import CrudMatrix
from legacylens.analysis.entrypoints import EntryPointsReport
from legacylens.analysis.side_effects import SideEffectReport
from legacylens.ir.models import ClassRecord, FunctionRecord

__all__ = [
    "call_flowchart",
    "crud_flowchart",
    "entry_point_pie",
    "inheritance_flowchart",
    "module_dependency_flowchart",
    "side_effect_pie",
]

_MAX_LABEL = 72


def module_dependency_flowchart(
    graph: nx.DiGraph,
    *,
    max_nodes: int = 60,
    max_edges: int = 120,
) -> tuple[str, bool]:
    """Render the project file include/import graph; return ``(markdown, truncated)``."""

    selected, truncated = _select_nodes(graph, max_nodes, kinds={"file"})
    body, edges_cut = _flowchart(graph, selected, max_edges=max_edges, label=_file_label)
    if not body:
        return "", truncated
    return _fence("flowchart LR", body), truncated or edges_cut


def inheritance_flowchart(
    graph: nx.DiGraph,
    classes: Iterable[ClassRecord],
    *,
    max_nodes: int = 60,
    max_edges: int = 120,
) -> tuple[str, bool]:
    """Render extends/implements/trait edges between declared classes."""

    names = {item.id: item.qualified_name for item in classes}
    selected, truncated = _select_nodes(graph, max_nodes, kinds={"class"})
    body, edges_cut = _flowchart(
        graph,
        selected,
        max_edges=max_edges,
        label=lambda node: names.get(node, _short_key(node)),
    )
    if not body:
        return "", truncated
    return _fence("flowchart LR", body), truncated or edges_cut


def call_flowchart(
    graph: nx.DiGraph,
    functions: Iterable[FunctionRecord],
    *,
    max_nodes: int = 50,
    max_edges: int = 100,
) -> tuple[str, bool]:
    """Render the busiest resolved call edges between functions and scripts."""

    names = {item.id: item.qualified_name for item in functions}
    selected, truncated = _select_nodes(graph, max_nodes, kinds={"function", "file"})
    body, edges_cut = _flowchart(
        graph,
        selected,
        max_edges=max_edges,
        label=lambda node: names.get(node, _short_key(node)),
    )
    if not body:
        return "", truncated
    return _fence("flowchart LR", body), truncated or edges_cut


def crud_flowchart(matrix: CrudMatrix, *, max_tables: int = 40) -> tuple[str, bool]:
    """Render table coverage as a diagram; no schema relationships are implied."""

    tables = matrix.tables[:max_tables]
    truncated = len(matrix.tables) > len(tables)
    if not tables:
        return "", truncated
    lines = ['  database[("database")]']
    for index, table in enumerate(tables):
        missing = ", ".join(table.missing) or "none"
        coverage = " ".join(f"{table.crud or 'none'} · missing {missing}".split())
        label = f"{_label(table.table_name)}<br/>{_label(coverage)}"
        lines.append(f'  t{index}["{label}"]')
        lines.append(f'  t{index} -->|"{_label(table.crud) or "none"}"| database')
        lines.append(f"  class t{index} {_coverage_class(table.crud, table.missing)};")
    lines.extend(
        [
            "  classDef complete fill:#dcfce7,stroke:#166534;",
            "  classDef partial fill:#fef9c3,stroke:#854d0e;",
            "  classDef read fill:#e0f2fe,stroke:#075985;",
            "  classDef write fill:#fee2e2,stroke:#991b1b;",
        ]
    )
    return _fence("flowchart LR", "\n".join(lines)), truncated


def side_effect_pie(report: SideEffectReport) -> str:
    """Render effect counts per kind."""

    rows = [(group.kind.value, len(group.records)) for group in report.groups]
    return _pie("Side effects by kind", rows)


def entry_point_pie(report: EntryPointsReport) -> str:
    """Render entry-point counts per kind."""

    counts: dict[str, int] = {}
    for detail in report.entries:
        counts[detail.entry.kind.value] = counts.get(detail.entry.kind.value, 0) + 1
    return _pie("Entry points by kind", sorted(counts.items()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eligible_nodes(graph: nx.DiGraph, *, kinds: set[str]) -> list[str]:
    return [
        node
        for node in graph.nodes
        if graph.nodes[node].get("kind") in kinds
        and not graph.nodes[node].get("external")
        and not str(node).startswith("unresolved:")
    ]


def _select_nodes(
    graph: nx.DiGraph,
    limit: int,
    *,
    kinds: set[str],
) -> tuple[list[str], bool]:
    if limit <= 0:
        return [], bool(_eligible_nodes(graph, kinds=kinds))
    eligible = _eligible_nodes(graph, kinds=kinds)
    ranked = sorted(
        eligible,
        key=lambda node: (-(graph.in_degree(node) + graph.out_degree(node)), str(node)),
    )
    return sorted(ranked[:limit], key=str), len(eligible) > limit


def _flowchart(
    graph: nx.DiGraph,
    selected: list[str],
    *,
    max_edges: int,
    label,
) -> tuple[str, bool]:
    if not selected:
        return "", False
    ids = {node: f"n{index}" for index, node in enumerate(selected)}
    lines = [f'  {ids[node]}["{_label(label(node))}"]' for node in selected]
    picked = set(selected)
    edges = [
        (src, dst) for src, dst in graph.edges() if src in picked and dst in picked and src != dst
    ]
    edges.sort(key=lambda pair: (str(pair[0]), str(pair[1])))
    cut = len(edges) > max_edges
    for src, dst in edges[: max(0, max_edges)]:
        kinds = ", ".join(graph[src][dst].get("kinds", ()))
        suffix = f'|"{_label(kinds)}"' if kinds else ""
        lines.append(f"  {ids[src]} -->{suffix} {ids[dst]}")
    return "\n".join(lines), cut


def _pie(title: str, rows: Iterable[tuple[str, int]]) -> str:
    data = [(name, count) for name, count in rows if count > 0]
    if not data:
        return ""
    lines = [f"    title {title}"]
    lines.extend(f'    "{_label(name)}" : {count}' for name, count in data)
    return _fence("pie showData", "\n".join(lines))


def _fence(declaration: str, body: str) -> str:
    return f"```mermaid\n{declaration}\n{body}\n```"


def _label(value: object, *, limit: int = _MAX_LABEL) -> str:
    text = " ".join(str(value).split())
    for character, replacement in (
        ('"', "#quot;"),
        ("<", "#lt;"),
        (">", "#gt;"),
        ("|", "#124;"),
    ):
        text = text.replace(character, replacement)
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _file_label(path: str) -> str:
    parts = str(path).replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else parts[-1]


def _short_key(key: str) -> str:
    text = str(key)
    if "#" in text:
        text = text.split("#", 1)[1]
    return text


def _coverage_class(crud: str, missing: tuple[str, ...]) -> str:
    if not crud:
        return "write"
    if not missing:
        return "complete"
    if crud == "R":
        return "read"
    return "partial"
