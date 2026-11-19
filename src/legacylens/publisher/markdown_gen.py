"""Markdown documentation generated from stored IR facts.

Every statement on every page is derived from an analysis report or a fact-store query;
the publisher makes no model calls and never guesses a relationship the IR does not
contain. Pages are deterministic: sections are ordered explicitly, tables are sorted by
their source data, paths are repository-relative and no timestamps are written.

The page set is fixed for this milestone:

``index.md``
    Project totals, the page index and the scope of the documentation.
``architecture.md``
    Module dependencies, inheritance, calls, graph statistics and unresolved references.
``entry-points.md``
    Routes, servlets, filters, actions, JSPs and the request inputs that reach them.
``database.md``
    The CRUD coverage matrix for every table named in a stored SQL statement.
``side-effects.md``
    Filesystem, HTTP, email, process and session effects, grouped by kind.
``hotspots.md``
    Function length, complexity and nesting rankings.
``files.md``
    Every scanned file with its language, size and declaration counts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar

from jinja2 import Environment, PackageLoader

from legacylens.analysis import (
    CrudMatrix,
    Distribution,
    EntryPointDetail,
    EntryPointsReport,
    FunctionMetric,
    Inventory,
    MetricsReport,
    RelationshipGraphs,
    SideEffectReport,
    build_crud,
    build_entrypoints,
    build_graphs,
    build_inventory,
    build_metrics,
    build_side_effect_report,
)
from legacylens.ir import (
    ClassRecord,
    FactStore,
    FileRecord,
    FunctionRecord,
    RequestInputRecord,
)

from .diagrams import (
    call_flowchart,
    crud_flowchart,
    entry_point_pie,
    inheritance_flowchart,
    module_dependency_flowchart,
    side_effect_pie,
)

__all__ = [
    "Documentation",
    "MarkdownPage",
    "build_documentation",
]

_Item = TypeVar("_Item")

_PAGE_DESCRIPTIONS: dict[str, str] = {
    "architecture": "Module dependencies, inheritance, calls and unresolved references.",
    "entry-points": "Routes, servlets, actions, JSPs and the request inputs they reach.",
    "database": "CRUD coverage for every table named in a stored SQL statement.",
    "side-effects": "Filesystem, HTTP, email, process and session effects by kind.",
    "hotspots": "Function length, cyclomatic complexity and nesting rankings.",
    "files": "Every scanned file with language, size and declaration counts.",
}


@dataclass(frozen=True, slots=True)
class MarkdownPage:
    """One rendered documentation page."""

    key: str
    filename: str
    title: str
    nav_title: str
    description: str
    markdown: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "filename": self.filename,
            "title": self.title,
            "nav_title": self.nav_title,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class Documentation:
    """Every page for one project plus the fact totals used to build them."""

    project: str
    pages: tuple[MarkdownPage, ...]
    facts: dict[str, int]

    def page(self, key: str) -> MarkdownPage:
        for item in self.pages:
            if item.key == key:
                return item
        raise KeyError(key)

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "pages": [item.as_dict() for item in self.pages],
            "facts": {key: self.facts[key] for key in sorted(self.facts)},
        }


def build_documentation(
    store: FactStore,
    *,
    project: str = "LegacyLens",
    max_nodes: int = 60,
    max_edges: int = 120,
    max_call_nodes: int = 50,
    max_call_edges: int = 100,
    max_tables: int = 40,
    max_rows: int = 200,
) -> Documentation:
    """Build every documentation page from the facts currently in ``store``."""

    for name, value in (
        ("max_nodes", max_nodes),
        ("max_edges", max_edges),
        ("max_call_nodes", max_call_nodes),
        ("max_call_edges", max_call_edges),
        ("max_tables", max_tables),
        ("max_rows", max_rows),
    ):
        if value < 1:
            raise ValueError(f"{name} must be >= 1")

    inventory = build_inventory(store)
    metrics = build_metrics(store)
    graphs = build_graphs(store)
    entries = build_entrypoints(store)
    crud = build_crud(store)
    effects = build_side_effect_report(store)
    files = store.list_files()
    classes = store.list_classes()
    functions = store.list_functions()

    others = (
        _architecture_page(
            graphs,
            classes,
            functions,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_call_nodes=max_call_nodes,
            max_call_edges=max_call_edges,
            max_rows=max_rows,
        ),
        _entry_points_page(entries, max_rows=max_rows),
        _database_page(crud, max_tables=max_tables, max_rows=max_rows),
        _side_effects_page(effects, max_rows=max_rows),
        _hotspots_page(metrics),
        _files_page(inventory, files, classes, functions, max_rows=max_rows),
    )
    index = _index_page(project, inventory, metrics, entries, crud, effects, others)

    facts = {
        "files": inventory.files,
        "loc": inventory.loc,
        "classes": inventory.classes,
        "functions": inventory.functions,
        "entry_points": len(entries.entries),
        "request_inputs": entries.input_total,
        "data_access": crud.records,
        "tables": len(crud.tables),
        "side_effects": effects.total,
    }
    return Documentation(project=project, pages=(index, *others), facts=facts)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _index_page(
    project: str,
    inventory: Inventory,
    metrics: MetricsReport,
    entries: EntryPointsReport,
    crud: CrudMatrix,
    effects: SideEffectReport,
    pages: Sequence[MarkdownPage],
) -> MarkdownPage:
    totals = _table(
        ("Fact", "Count"),
        [
            ("Files", _number(inventory.files)),
            ("Lines of code", _number(inventory.loc)),
            ("Classes", _number(inventory.classes)),
            ("Functions and methods", _number(inventory.functions)),
            ("Entry points", _number(len(entries.entries))),
            ("Request inputs", _number(entries.input_total)),
            ("SQL statements", _number(crud.records)),
            ("Tables named in SQL", _number(len(crud.tables))),
            ("Recorded side effects", _number(effects.total)),
        ],
    )

    languages = _table(
        ("Language", "Files", "Lines of code", "Bytes"),
        [
            (
                _cell(item.language.value),
                _number(item.files),
                _number(item.loc),
                _number(item.size_bytes),
            )
            for item in inventory.languages
        ],
        empty="_No files have been scanned._",
    )

    page_rows = [
        (f"[{_cell(item.title)}]({item.filename})", _cell(item.description)) for item in pages
    ]
    summary = "\n\n".join(
        [
            (
                f"This site documents **{_cell(project)}** from static facts stored in the "
                "LegacyLens IR. Counts cover files that were successfully scanned and parsed."
            ),
            "## At a glance",
            totals,
            "## Documentation",
            _table(("Page", "Contents"), page_rows),
            "## Languages",
            languages,
            "## Scope",
            _bullets(
                (
                    "Every page is generated from stored facts; publishing makes no model calls.",
                    (
                        "Parsed functions: "
                        f"{_number(metrics.function_count)} across {_number(inventory.files)} "
                        "files."
                    ),
                    "Unknown tables, dynamic values and unresolved references are reported as "
                    "unknown, never guessed.",
                    "Diagrams are bounded; any truncation is stated next to the diagram.",
                    "There are no per-file pages or schema relationships in this milestone.",
                )
            ),
        ]
    )
    return MarkdownPage(
        key="index",
        filename="index.md",
        title=f"{project} documentation",
        nav_title="Overview",
        description="Project totals, the page index and the scope of this documentation.",
        markdown=_render("index.md.j2", project=project, summary=summary),
    )


def _architecture_page(
    graphs: RelationshipGraphs,
    classes: Sequence[ClassRecord],
    functions: Sequence[FunctionRecord],
    *,
    max_nodes: int,
    max_edges: int,
    max_call_nodes: int,
    max_call_edges: int,
    max_rows: int,
) -> MarkdownPage:
    summary = graphs.summary(top_n=10)
    includes_diagram, includes_cut = module_dependency_flowchart(
        graphs.includes,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    inheritance_diagram, inheritance_cut = inheritance_flowchart(
        graphs.inheritance,
        classes,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    calls_diagram, calls_cut = call_flowchart(
        graphs.calls,
        functions,
        max_nodes=max_call_nodes,
        max_edges=max_call_edges,
    )

    diagram_rows = (
        (
            "Includes and imports",
            includes_diagram,
            includes_cut,
            max_nodes,
            max_edges,
            "_No resolved include or import edges are stored for this project._",
        ),
        (
            "Inheritance",
            inheritance_diagram,
            inheritance_cut,
            max_nodes,
            max_edges,
            "_No resolved inheritance edges are stored for this project._",
        ),
        (
            "Calls",
            calls_diagram,
            calls_cut,
            max_call_nodes,
            max_call_edges,
            "_No resolved call edges are stored for this project._",
        ),
    )
    sections = [
        (
            "This page shows relationships resolved from stored edges. A reference that could "
            "not be resolved to exactly one target is reported below instead of being drawn."
        )
    ]
    for heading, diagram, cut, node_budget, edge_budget, empty in diagram_rows:
        sections.append(f"## {heading}")
        if diagram:
            sections.append(diagram)
            sections.append(_diagram_note(cut, node_budget, edge_budget))
        else:
            sections.append(empty)

    summary_rows = [
        _graph_summary_row("Includes and imports", summary.get("includes")),
        _graph_summary_row("Inheritance", summary.get("inheritance")),
        _graph_summary_row("Calls", summary.get("calls")),
    ]
    sections.extend(
        [
            "## Graph summary",
            _table(
                ("Graph", "Nodes", "Edges", "Resolved edges", "Unresolved edges", "Cycles"),
                summary_rows,
            ),
        ]
    )

    included = _mapping(summary.get("includes"))
    fan_in = _items(included.get("top_fan_in"))
    fan_rows = [
        (
            _code(_mapping(item).get("node", "")),
            _number(_int(_mapping(item).get("fan_in"))),
            _number(_int(_mapping(item).get("fan_out"))),
        )
        for item in fan_in[:10]
    ]
    sections.extend(
        [
            "## Most connected files",
            _table(("File", "Fan-in", "Fan-out"), fan_rows, empty="_No file edges are stored._"),
        ]
    )

    unresolved = list(graphs.unresolved)
    shown, _ = _limit(unresolved, min(max_rows, 25))
    rows = [
        (
            _cell(item.kind.value),
            _code(_short_key(item.src)),
            _code(item.reference),
            _code(f"{item.file_path}:{item.line}"),
        )
        for item in shown
    ]
    sections.extend(
        [
            "## Unresolved references",
            (
                "These references were kept verbatim because the analysis could not resolve "
                "them to exactly one target."
            ),
            _table(
                ("Kind", "Source", "Reference", "Location"),
                rows,
                empty="_Every stored reference was resolved._",
            ),
            _truncation_note(len(shown), len(unresolved), what="unresolved references"),
        ]
    )
    return _page(
        key="architecture",
        title="Architecture",
        nav_title="Architecture",
        body="\n\n".join(section for section in sections if section),
    )


def _entry_points_page(
    report: EntryPointsReport,
    *,
    max_rows: int,
) -> MarkdownPage:
    counts = Counter(detail.entry.kind.value for detail in report.entries)
    kind_rows = [
        (_cell(kind), _number(count), _percent(count, len(report.entries)))
        for kind, count in sorted(counts.items())
    ]
    sections = [
        (
            "Entry points are how control enters the application. Each row lists the stored "
            "route, servlet, filter, action, JSP or CLI path and the handler methods resolved "
            "for it; inputs are the request values that reach the entry point through those "
            "handlers."
        ),
        "## Entry points by kind",
    ]
    pie = entry_point_pie(report)
    if pie:
        sections.append(pie)
    sections.extend(
        [
            _table(
                ("Kind", "Entry points", "Share"),
                kind_rows,
                empty="_No entry points are stored._",
            ),
        ]
    )

    shown, _ = _limit(report.entries, max_rows)
    rows = [
        (
            _cell(detail.entry.kind.value),
            _cell(detail.entry.http_method.value if detail.entry.http_method else "-"),
            _code(detail.entry.path),
            _code(_handler_text(detail)),
            _number(len(detail.inputs)),
            _cell(_auth_text(detail.entry.requires_auth, detail.entry.auth_hint)),
            _cell(detail.entry.framework or "-"),
            _code(f"{detail.entry.file_path}:{detail.entry.line}"),
        )
        for detail in shown
    ]
    sections.extend(
        [
            "## Entry points",
            _table(
                (
                    "Kind",
                    "Method",
                    "Path",
                    "Handler",
                    "Inputs",
                    "Auth",
                    "Framework",
                    "Location",
                ),
                rows,
                empty="_No entry points are stored._",
            ),
            _truncation_note(len(shown), len(report.entries), what="entry points"),
        ]
    )

    inputs = list(_all_inputs(report))
    input_counts = Counter(item.kind.value for item in inputs)
    sections.extend(
        [
            "## Request inputs by kind",
            _table(
                ("Kind", "Inputs", "Share"),
                [
                    (_cell(kind), _number(count), _percent(count, len(inputs)))
                    for kind, count in sorted(input_counts.items())
                ],
                empty="_No request inputs are stored._",
            ),
        ]
    )

    shown_inputs, _ = _limit(report.unassigned_inputs, min(max_rows, 50))
    input_rows = [
        (
            _cell(item.kind.value),
            _code(item.name),
            _code(item.raw or "-"),
            _code(f"{item.file_path}:{item.line}"),
        )
        for item in shown_inputs
    ]
    sections.extend(
        [
            "## Inputs outside any entry point",
            (
                "These inputs were read from the request but no stored entry point accounts "
                "for them."
            ),
            _table(
                ("Kind", "Name", "Expression", "Location"),
                input_rows,
                empty="_No unassigned request inputs are stored._",
            ),
            _truncation_note(
                len(shown_inputs),
                len(report.unassigned_inputs),
                what="unassigned inputs",
            ),
        ]
    )
    return _page(
        key="entry-points",
        title="Entry points",
        nav_title="Entry points",
        body="\n\n".join(section for section in sections if section),
    )


def _database_page(
    matrix: CrudMatrix,
    *,
    max_tables: int,
    max_rows: int,
) -> MarkdownPage:
    complete = [item for item in matrix.tables if item.crud and not item.missing]
    read_only = [item for item in matrix.tables if item.crud == "R"]
    write_only = [item for item in matrix.tables if item.crud and "R" not in item.crud]
    diagram, diagram_cut = crud_flowchart(matrix, max_tables=max_tables)

    shown, _ = _limit(matrix.tables, max_rows)
    matrix_rows = [
        (
            _code(item.table_name),
            _cell(item.crud or "-"),
            _cell(", ".join(item.missing) or "none"),
            _cell(", ".join(f"{op.operation.value} ({op.records})" for op in item.operations)),
            _number(len(item.functions)),
            _number(len(item.files)),
        )
        for item in shown
    ]
    incomplete = [item for item in matrix.tables if item.missing]
    incomplete_shown, _ = _limit(incomplete, min(max_rows, 50))
    incomplete_rows = [
        (_code(item.table_name), _cell(", ".join(item.missing))) for item in incomplete_shown
    ]

    unknown_shown, _ = _limit(matrix.unknown_table, min(max_rows, 25))
    unknown_rows = [
        (
            _code(f"{item.file_path}:{item.line}"),
            _cell(item.operation.value),
            _cell(item.driver or "-"),
            _code(item.sql, limit=100),
        )
        for item in unknown_shown
    ]

    sections = [
        (
            "The CRUD matrix pivots stored SQL statements into table, operation and function "
            "coverage. Operation letters are R (select), C (insert), U (update) and D (delete). "
            "No foreign keys or schema relationships are inferred: a table appears only when a "
            "stored statement names it."
        ),
        "## Coverage summary",
        _table(
            ("Fact", "Count"),
            [
                ("SQL statements", _number(matrix.records)),
                ("Statements outside a function", _number(matrix.script_records)),
                ("Tables", _number(len(matrix.tables))),
                ("Tables with complete CRUD coverage", _number(len(complete))),
                ("Read-only tables", _number(len(read_only))),
                ("Write-only tables", _number(len(write_only))),
                ("Statements with no table", _number(len(matrix.unknown_table))),
            ],
        ),
        "## CRUD coverage",
    ]
    if diagram:
        sections.append(diagram)
        sections.append(_table_budget_note(diagram_cut, max_tables))
    sections.extend(
        [
            "## CRUD matrix",
            _table(
                (
                    "Table",
                    "CRUD",
                    "Missing",
                    "Operations",
                    "Functions",
                    "Files",
                ),
                matrix_rows,
                empty="_No SQL statement names a table._",
            ),
            _truncation_note(len(shown), len(matrix.tables), what="tables"),
            "## Tables with incomplete coverage",
            _table(
                ("Table", "Missing operations"),
                incomplete_rows,
                empty="_Every table has all four CRUD operations._",
            ),
            _truncation_note(len(incomplete_shown), len(incomplete), what="incomplete tables"),
            "## Statements with no table",
            (
                "These statements were classified but no table name could be extracted, so they "
                "stay out of the matrix."
            ),
            _table(
                ("Location", "Operation", "Driver", "SQL"),
                unknown_rows,
                empty="_Every stored statement names a table._",
            ),
            _truncation_note(
                len(unknown_shown), len(matrix.unknown_table), what="unattributed statements"
            ),
        ]
    )
    return _page(
        key="database",
        title="Database usage",
        nav_title="Database",
        body="\n\n".join(section for section in sections if section),
    )


def _side_effects_page(
    report: SideEffectReport,
    *,
    max_rows: int,
) -> MarkdownPage:
    kind_rows = [
        (
            _cell(group.kind.value),
            _number(len(group.records)),
            _cell(", ".join(group.sinks) or "-"),
            _number(len(group.files)),
            _number(len(group.functions)),
        )
        for group in report.groups
    ]
    records = [(group.kind, item) for group in report.groups for item in group.records]
    shown, _ = _limit(records, max_rows)
    site_rows = [
        (
            _cell(kind.value),
            _cell(item.detail),
            _cell(item.sink or "-"),
            _code(f"{item.file_path}:{item.line}"),
            _code(_short_key(item.function_id)) if item.function_id else "-",
        )
        for kind, item in shown
    ]

    unattributed_shown, _ = _limit(report.unattributed, min(max_rows, 25))
    unattributed_rows = [
        (
            _cell(item.kind.value),
            _cell(item.detail),
            _cell(item.sink or "-"),
            _code(f"{item.file_path}:{item.line}"),
        )
        for item in unattributed_shown
    ]

    sections = [
        (
            "Side effects are externally visible operations recorded by the parsers: "
            "filesystem writes, network calls, email, process execution, session writes and "
            "output. Detail text keeps a literal target when the source states one and stays "
            "action-only when the target is dynamic."
        ),
        "## Effects by kind",
    ]
    pie = side_effect_pie(report)
    if pie:
        sections.append(pie)
    sections.extend(
        [
            _table(
                ("Kind", "Effects", "Sinks", "Files", "Functions"),
                kind_rows,
                empty="_No side effects are stored._",
            ),
            "## Effect sites",
            _table(
                ("Kind", "Detail", "Sink", "Location", "Function"),
                site_rows,
                empty="_No side effects are stored._",
            ),
            _truncation_note(len(shown), len(records), what="effect sites"),
            "## Effects outside any function",
            (
                "File-level effects, including JSP scriptlets, are recorded with no function "
                "attribution."
            ),
            _table(
                ("Kind", "Detail", "Sink", "Location"),
                unattributed_rows,
                empty="_Every stored effect belongs to a function._",
            ),
            _truncation_note(
                len(unattributed_shown),
                len(report.unattributed),
                what="unattributed effects",
            ),
        ]
    )
    return _page(
        key="side-effects",
        title="Side effects",
        nav_title="Side effects",
        body="\n\n".join(section for section in sections if section),
    )


def _hotspots_page(metrics: MetricsReport) -> MarkdownPage:
    distribution_rows = [
        _distribution_row("Length (LOC)", metrics.length),
        _distribution_row("Cyclomatic complexity", metrics.complexity),
        _distribution_row("Maximum nesting", metrics.nesting),
    ]
    sections = [
        (
            "These rankings come from the metric row stored for each function: source length, "
            "cyclomatic complexity and maximum nesting depth."
        ),
        "## Distributions",
        _table(
            ("Metric", "Count", "Total", "Min", "Max", "Mean", "Median", "P95"),
            distribution_rows,
            empty="_No functions are stored._",
        ),
        "## Longest functions",
        _function_table(metrics.longest, "loc", empty="_No functions are stored._"),
        "## Most complex functions",
        _function_table(metrics.most_complex, "complexity", empty="_No functions are stored._"),
        "## Deepest nesting",
        _function_table(metrics.deepest, "nesting", empty="_No functions are stored._"),
    ]
    return _page(
        key="hotspots",
        title="Hotspots",
        nav_title="Hotspots",
        body="\n\n".join(section for section in sections if section),
    )


def _files_page(
    inventory: Inventory,
    files: Sequence[FileRecord],
    classes: Sequence[ClassRecord],
    functions: Sequence[FunctionRecord],
    *,
    max_rows: int,
) -> MarkdownPage:
    class_counts = Counter(item.file_path for item in classes)
    function_counts = Counter(item.file_path for item in functions)
    shown, _ = _limit(files, max_rows)
    rows = [
        (
            _code(item.path),
            _cell(item.language.value),
            _number(item.loc),
            _number(item.size_bytes),
            _number(class_counts[item.path]),
            _number(function_counts[item.path]),
            "ok" if item.parse_ok else "error",
        )
        for item in shown
    ]
    errors = [item for item in files if not item.parse_ok]
    error_shown, _ = _limit(errors, min(max_rows, 25))
    error_rows = [(_code(item.path), _cell(item.parse_error or "-")) for item in error_shown]

    sections = [
        (
            "Every file that passed ingestion and was stored in the IR appears here. Paths are "
            "repository-relative; declaration counts come from the stored class and function "
            "rows."
        ),
        "## Totals",
        _table(
            ("Fact", "Count"),
            [
                ("Files", _number(inventory.files)),
                ("Lines of code", _number(inventory.loc)),
                ("Bytes", _number(inventory.size_bytes)),
                ("Classes", _number(inventory.classes)),
                ("Functions and methods", _number(inventory.functions)),
                ("Parse errors", _number(inventory.parse_errors)),
                ("Generated files", _number(inventory.generated_files)),
                ("Vendored files", _number(inventory.vendored_files)),
            ],
        ),
        "## Files",
        _table(
            (
                "Path",
                "Language",
                "Lines",
                "Bytes",
                "Classes",
                "Functions",
                "Parsed",
            ),
            rows,
            empty="_No files have been scanned._",
        ),
        _truncation_note(len(shown), len(files), what="files"),
        "## Parse errors",
        _table(
            ("Path", "Error"),
            error_rows,
            empty="_Every stored file parsed without errors._",
        ),
        _truncation_note(len(error_shown), len(errors), what="parse errors"),
    ]
    return _page(
        key="files",
        title="Files",
        nav_title="Files",
        body="\n\n".join(section for section in sections if section),
    )


# ---------------------------------------------------------------------------
# Rendering and formatting
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("legacylens.publisher", "templates"),
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render(template_name: str, **context: object) -> str:
    template = _environment().get_template(template_name)
    return template.render(**context).rstrip() + "\n"


def _page(*, key: str, title: str, nav_title: str, body: str) -> MarkdownPage:
    return MarkdownPage(
        key=key,
        filename=f"{key}.md",
        title=title,
        nav_title=nav_title,
        description=_PAGE_DESCRIPTIONS[key],
        markdown=_render("page.md.j2", title=title, body=body.rstrip()),
    )


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    empty: str = "_No data._",
) -> str:
    materialised = [list(row) for row in rows]
    if not materialised:
        return empty
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in materialised)
    return "\n".join(lines)


def _bullets(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _cell(value: object, *, limit: int = 120) -> str:
    return _one_line(value, limit=limit).replace("|", "\\|")


def _code(value: object, *, limit: int = 120) -> str:
    text = _one_line(value, limit=limit).replace("`", "'").replace("|", "\\|")
    return f"`{text}`"


def _one_line(value: object, *, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _number(value: int) -> str:
    return f"{value:,}"


def _percent(part: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{part * 100 / total:.0f}%"


def _limit(rows: Sequence[_Item], limit: int) -> tuple[list[_Item], bool]:
    shown = list(rows[: max(0, limit)])
    return shown, len(rows) > len(shown)


def _truncation_note(shown: int, total: int, *, what: str) -> str:
    if total <= shown:
        return ""
    return f"Only the first {shown:,} of {total:,} {what} are shown."


def _diagram_note(cut: bool, max_nodes: int, max_edges: int) -> str:
    budget = (
        f"Diagram budget: at most {max_nodes} nodes and {max_edges} edges, ranked by total degree."
    )
    if cut:
        budget += " The diagram was truncated to fit that budget."
    return budget


def _table_budget_note(cut: bool, max_tables: int) -> str:
    note = f"Diagram budget: at most {max_tables} tables."
    if cut:
        note += " The diagram was truncated to fit that budget."
    return note


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _graph_summary_row(label: str, value: object) -> list[str]:
    data = _mapping(value)
    return [
        _cell(label),
        _number(_int(data.get("nodes"))),
        _number(_int(data.get("edges"))),
        _number(_int(data.get("resolved_edges"))),
        _number(_int(data.get("unresolved_edges"))),
        _number(len(_items(data.get("cycles")))),
    ]


def _distribution_row(label: str, distribution: Distribution) -> list[str]:
    return [
        _cell(label),
        _number(distribution.count),
        _number(distribution.total),
        _number(distribution.minimum),
        _number(distribution.maximum),
        f"{distribution.mean:.2f}",
        f"{distribution.median:.2f}",
        _number(distribution.p95),
    ]


def _function_table(items: Sequence[FunctionMetric], focus: str, *, empty: str) -> str:
    headers = {
        "loc": ("Function", "LOC", "Complexity", "Nesting", "Location"),
        "complexity": ("Function", "Complexity", "LOC", "Nesting", "Location"),
        "nesting": ("Function", "Nesting", "Complexity", "LOC", "Location"),
    }[focus]
    rows = []
    for item in items:
        values = {
            "loc": _number(item.loc),
            "complexity": _number(item.cyclomatic_complexity),
            "nesting": _number(item.max_nesting),
        }
        order = {
            "loc": ("loc", "complexity", "nesting"),
            "complexity": ("complexity", "loc", "nesting"),
            "nesting": ("nesting", "complexity", "loc"),
        }[focus]
        rows.append(
            (
                _code(item.qualified_name),
                *(values[key] for key in order),
                _code(f"{item.file_path}:{item.start_line}"),
            )
        )
    return _table(headers, rows, empty=empty)


def _handler_text(detail: EntryPointDetail) -> str:
    if detail.handlers:
        return ", ".join(_short_key(item) for item in detail.handlers)
    if detail.entry.handler_ref:
        return detail.entry.handler_ref
    return "-"


def _auth_text(requires_auth: bool | None, hint: str | None) -> str:
    if requires_auth is True:
        text = "required"
    elif requires_auth is False:
        text = "not observed"
    else:
        text = "unknown"
    return f"{text} ({hint})" if hint else text


def _all_inputs(report: EntryPointsReport) -> Iterable[RequestInputRecord]:
    seen: set[object] = set()
    for detail in report.entries:
        for item in detail.inputs:
            if item not in seen:
                seen.add(item)
                yield item
    for item in report.unassigned_inputs:
        if item not in seen:
            seen.add(item)
            yield item


def _short_key(key: str) -> str:
    text = str(key)
    if "#" in text:
        text = text.split("#", 1)[1]
    return text
