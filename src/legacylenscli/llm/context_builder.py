"""Budgeted, citation-first prompt context assembled from the IR.

The builder never lets the model invent structure: every fact in the prompt comes from the
store (or source re-read through a stored repository-relative path), and every line carries
a citation the model is allowed to use. Sections are added in priority order within a
character budget; when the budget runs out the bundle is marked ``truncated``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from legacylenscli.analysis import (
    build_business_rules,
    build_crud,
    build_dead_code,
    build_entrypoints,
    build_graphs,
    build_inventory,
    build_metrics,
    build_side_effect_report,
)
from legacylenscli.ir import (
    ClassRecord,
    EdgeKind,
    FactStore,
    FileRecord,
    FunctionRecord,
    entry_citation,
)

__all__ = [
    "ContextBundle",
    "ContextTarget",
    "build_context",
]

TargetKind = Literal["function", "class", "file", "project"]


@dataclass(frozen=True, slots=True)
class ContextTarget:
    """A resolved object the prompt is about."""

    kind: TargetKind
    key: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "key": self.key}


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Rendered context plus the citations the model may use."""

    target: ContextTarget
    text: str
    citations: tuple[str, ...]
    estimated_tokens: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target.as_dict(),
            "text": self.text,
            "citations": list(self.citations),
            "estimated_tokens": self.estimated_tokens,
            "truncated": self.truncated,
        }


def build_context(
    store: FactStore,
    target: str | ContextTarget,
    *,
    source_root: str | Path | None = None,
    budget_chars: int = 12_000,
    max_source_lines: int = 120,
    max_related: int = 8,
    include_analysis: bool = True,
) -> ContextBundle:
    """Build a deterministic context bundle for a function, class, file or project."""

    if budget_chars < 1:
        raise ValueError("budget_chars must be >= 1")
    if max_source_lines < 0:
        raise ValueError("max_source_lines must be >= 0")
    if max_related < 0:
        raise ValueError("max_related must be >= 0")

    resolved = target if isinstance(target, ContextTarget) else _resolve_target(store, target)
    root = Path(source_root) if source_root is not None else None
    builder = _Builder(budget_chars)

    if resolved.kind == "function":
        function = store.get_function(resolved.key)
        if function is None:
            raise ValueError(f"function not found: {resolved.key}")
        _function_context(
            builder,
            store,
            function,
            root,
            max_source_lines=max_source_lines,
            max_related=max_related,
            include_analysis=include_analysis,
        )
    elif resolved.kind == "class":
        class_record = store.get_class(resolved.key)
        if class_record is None:
            raise ValueError(f"class not found: {resolved.key}")
        _class_context(
            builder,
            store,
            class_record,
            root,
            max_source_lines=max_source_lines,
            max_related=max_related,
        )
    elif resolved.kind == "file":
        file = store.get_file(resolved.key)
        if file is None:
            raise ValueError(f"file not found: {resolved.key}")
        _file_context(
            builder,
            store,
            file,
            root,
            max_source_lines=max_source_lines,
            max_related=max_related,
        )
    else:
        _project_context(
            builder,
            store,
            root,
            max_related=max_related,
            include_analysis=include_analysis,
        )

    text = builder.render()
    return ContextBundle(
        target=resolved,
        text=text,
        citations=tuple(builder.citations),
        estimated_tokens=len(text) // 4,
        truncated=builder.truncated,
    )


def _resolve_target(store: FactStore, value: str) -> ContextTarget:
    if value in {"project", "*"}:
        return ContextTarget("project", "project")
    if value.startswith("fn:"):
        return ContextTarget("function", value)
    if value.startswith("class:"):
        return ContextTarget("class", value)
    if store.get_function(value) is not None:
        return ContextTarget("function", value)
    if store.get_class(value) is not None:
        return ContextTarget("class", value)
    if store.get_file(value) is not None:
        return ContextTarget("file", value)
    raise ValueError(f"context target not found: {value}")


def _function_context(
    builder: _Builder,
    store: FactStore,
    function: FunctionRecord,
    source_root: Path | None,
    *,
    max_source_lines: int,
    max_related: int,
    include_analysis: bool,
) -> None:
    builder.add(
        f"Target function: {function.id}",
        [
            (f"qualified name: {function.qualified_name}", (function.id,)),
            (
                f"declaration: {function.file_path}:{function.span.start_line}"
                f"-{function.span.end_line}",
                (f"{function.file_path}:{function.span.start_line}",),
            ),
            (f"signature: {function.signature}", (f"{function.file_path}:{function.span.start_line}",)),
            (
                f"visibility: {function.visibility.value}; static={function.is_static}; "
                f"abstract={function.is_abstract}",
                (function.id,),
            ),
            (
                f"metrics: loc={function.loc}, complexity={function.cyclomatic_complexity}, "
                f"nesting={function.max_nesting}",
                (function.id,),
            ),
        ],
    )
    if function.class_id is not None:
        owner = store.get_class(function.class_id)
        if owner is not None:
            builder.add(
                "Owner class",
                [
                    (
                        f"{owner.qualified_name} ({owner.kind.value}) at "
                        f"{owner.file_path}:{owner.span.start_line}",
                        (owner.id, f"{owner.file_path}:{owner.span.start_line}"),
                    )
                ],
            )

    _source_section(
        builder,
        source_root,
        function.file_path,
        function.span.start_line,
        function.span.end_line,
        max_source_lines=max_source_lines,
    )

    if max_related:
        graphs = build_graphs(store)
        functions = {item.id: item for item in store.list_functions()}
        outgoing = sorted(
            node
            for node in graphs.calls.successors(function.id)
            if not node.startswith("unresolved:") and node in functions
        )
        incoming = sorted(
            node
            for node in graphs.calls.predecessors(function.id)
            if not node.startswith("unresolved:") and node in functions
        )
        builder.add(
            "Resolved calls",
            [
                (
                    f"calls {target.qualified_name} "
                    f"({target.file_path}:{target.span.start_line})",
                    (target.id, f"{target.file_path}:{target.span.start_line}"),
                )
                for target in (functions[key] for key in outgoing[:max_related])
            ],
        )
        builder.add(
            "Callers",
            [
                (
                    f"called by {caller.qualified_name} "
                    f"({caller.file_path}:{caller.span.start_line})",
                    (caller.id, f"{caller.file_path}:{caller.span.start_line}"),
                )
                for caller in (functions[key] for key in incoming[:max_related])
            ],
        )

    access = store.list_data_access(function_id=function.id)
    builder.add(
        "Data access",
        [
            (
                f"{item.operation.value} {item.table_name or '<unknown>'} "
                f"at {item.file_path}:{item.line}",
                (f"{item.file_path}:{item.line}",),
            )
            for item in sorted(access, key=lambda row: (row.file_path, row.line, row.sql))
        ],
    )

    effects = store.list_side_effects(function_id=function.id)
    builder.add(
        "Side effects",
        [
            (
                f"{item.kind.value}: {item.detail} "
                f"({item.sink or 'unknown sink'}) at {item.file_path}:{item.line}",
                (f"{item.file_path}:{item.line}",),
            )
            for item in sorted(effects, key=lambda row: (row.file_path, row.line, row.detail))
        ],
    )

    entries = build_entrypoints(store)
    reached = [
        detail
        for detail in entries.entries
        if detail.entry.function_id == function.id or function.id in detail.handlers
    ]
    builder.add(
        "Entry points",
        [
            (
                f"{detail.entry.kind.value} {detail.entry.path} at "
                f"{detail.entry.file_path}:{detail.entry.line}",
                (
                    entry_citation(
                        detail.entry.kind,
                        detail.entry.http_method,
                        detail.entry.path,
                    ),
                    f"{detail.entry.file_path}:{detail.entry.line}",
                ),
            )
            for detail in reached[:max_related]
        ],
    )

    if include_analysis:
        rules = build_business_rules(store, source_root=source_root)
        related_rules = [
            item for item in rules.candidates if item.function_id == function.id
        ]
        builder.add(
            "Business-rule candidates",
            [
                (
                    f"{item.kind.value} ({item.source.value}): {item.evidence}",
                    item.citations,
                )
                for item in related_rules[:max_related]
            ],
        )

        dead = build_dead_code(store)
        candidate = next(
            (item for item in dead.candidates if item.function_id == function.id),
            None,
        )
        builder.add(
            "Reachability",
            [
                (
                    "dead-code candidate: no path from a detected entry point",
                    (function.id,),
                )
                if candidate is not None
                else ("reachable from a detected entry point", (function.id,))
            ],
        )


def _class_context(
    builder: _Builder,
    store: FactStore,
    class_record: ClassRecord,
    source_root: Path | None,
    *,
    max_source_lines: int,
    max_related: int,
) -> None:
    builder.add(
        f"Target class: {class_record.id}",
        [
            (f"qualified name: {class_record.qualified_name}", (class_record.id,)),
            (
                f"kind: {class_record.kind.value}; extends={class_record.extends or '-'}; "
                f"implements={', '.join(class_record.implements) or '-'}",
                (class_record.id,),
            ),
            (
                f"declaration: {class_record.file_path}:{class_record.span.start_line}"
                f"-{class_record.span.end_line}",
                (f"{class_record.file_path}:{class_record.span.start_line}",),
            ),
        ],
    )
    _source_section(
        builder,
        source_root,
        class_record.file_path,
        class_record.span.start_line,
        class_record.span.end_line,
        max_source_lines=max_source_lines,
    )
    methods = sorted(
        store.list_functions(class_id=class_record.id),
        key=lambda item: (item.span.start_line, item.qualified_name),
    )
    builder.add(
        "Methods",
        [
            (
                f"{item.qualified_name} at {item.file_path}:{item.span.start_line} "
                f"(loc={item.loc}, complexity={item.cyclomatic_complexity})",
                (item.id, f"{item.file_path}:{item.span.start_line}"),
            )
            for item in methods[:max_related]
        ],
    )


def _file_context(
    builder: _Builder,
    store: FactStore,
    file: FileRecord,
    source_root: Path | None,
    *,
    max_source_lines: int,
    max_related: int,
) -> None:
    builder.add(
        f"Target file: {file.path}",
        [
            (f"language: {file.language.value}", (file.path,)),
            (
                f"lines: {file.loc}; generated={file.is_generated}; vendored={file.is_vendored}",
                (file.path,),
            ),
            (f"package: {file.package or '-'}; module: {file.module or '-'}", (file.path,)),
        ],
    )
    _source_section(
        builder,
        source_root,
        file.path,
        1,
        file.loc,
        max_source_lines=max_source_lines,
    )
    classes = sorted(store.list_classes(file_path=file.path), key=lambda item: item.qualified_name)
    builder.add(
        "Classes",
        [
            (
                f"{item.qualified_name} ({item.kind.value}) at {file.path}:"
                f"{item.span.start_line}",
                (item.id, f"{file.path}:{item.span.start_line}"),
            )
            for item in classes[:max_related]
        ],
    )
    functions = sorted(
        store.list_functions(file_path=file.path),
        key=lambda item: (item.span.start_line, item.qualified_name),
    )
    builder.add(
        "Functions",
        [
            (
                f"{item.qualified_name} at {file.path}:{item.span.start_line} "
                f"(loc={item.loc}, complexity={item.cyclomatic_complexity})",
                (item.id, f"{file.path}:{item.span.start_line}"),
            )
            for item in functions[:max_related]
        ],
    )
    _file_facts(builder, store, file.path, max_related=max_related)


def _project_context(
    builder: _Builder,
    store: FactStore,
    source_root: Path | None,
    *,
    max_related: int,
    include_analysis: bool,
) -> None:
    inventory = build_inventory(store)
    builder.add(
        "Project inventory",
        [
            (
                f"files={inventory.files}, loc={inventory.loc}, classes={inventory.classes}, "
                f"functions={inventory.functions}, parse_errors={inventory.parse_errors}",
                (),
            ),
            (
                "languages: "
                + ", ".join(f"{item.language.value}={item.files}" for item in inventory.languages),
                (),
            ),
        ],
    )
    metrics = build_metrics(store, top_n=max_related)
    builder.add(
        "Metric distributions",
        [
            (f"length: {metrics.length.as_dict()}", ()),
            (f"complexity: {metrics.complexity.as_dict()}", ()),
            (f"nesting: {metrics.nesting.as_dict()}", ()),
        ],
    )
    builder.add(
        "Most complex functions",
        [
            (
                f"{item.qualified_name} at {item.file_path}:{item.start_line} "
                f"(complexity={item.cyclomatic_complexity}, loc={item.loc})",
                (item.function_id, f"{item.file_path}:{item.start_line}"),
            )
            for item in metrics.most_complex[:max_related]
        ],
    )

    entries = build_entrypoints(store)
    builder.add(
        "Entry points",
        [
            (
                f"{detail.entry.kind.value} {detail.entry.path} at "
                f"{detail.entry.file_path}:{detail.entry.line}",
                (
                    entry_citation(
                        detail.entry.kind,
                        detail.entry.http_method,
                        detail.entry.path,
                    ),
                    f"{detail.entry.file_path}:{detail.entry.line}",
                ),
            )
            for detail in entries.entries[:max_related]
        ],
    )

    crud = build_crud(store)
    builder.add(
        "CRUD coverage",
        [
            (
                f"{table.table_name}: crud={table.crud or '-'} missing={','.join(table.missing) or '-'}",
                (f"table:{table.table_name}",),
            )
            for table in crud.tables[:max_related]
        ],
    )

    effects = build_side_effect_report(store)
    builder.add(
        "Side effects",
        [
            (f"{group.kind.value}: {len(group.records)}", ())
            for group in effects.groups
        ],
    )

    if include_analysis:
        dead = build_dead_code(store)
        builder.add(
            "Dead code",
            [
                (
                    f"{item.qualified_name} at {item.file_path}:{item.start_line}",
                    (item.function_id, f"{item.file_path}:{item.start_line}"),
                )
                for item in dead.candidates[:max_related]
            ],
        )
        rules = build_business_rules(store, source_root=source_root)
        builder.add(
            "Business-rule candidates",
            [
                (
                    f"{item.kind.value} ({item.source.value}) at "
                    f"{item.file_path}:{item.line}: {item.evidence}",
                    item.citations,
                )
                for item in rules.candidates[:max_related]
            ],
        )


def _file_facts(builder: _Builder, store: FactStore, file_path: str, *, max_related: int) -> None:
    access = store.list_data_access(file_path=file_path)
    builder.add(
        "Data access",
        [
            (
                f"{item.operation.value} {item.table_name or '<unknown>'} at "
                f"{item.file_path}:{item.line}",
                (f"{item.file_path}:{item.line}",),
            )
            for item in sorted(access, key=lambda row: (row.line, row.sql))[:max_related]
        ],
    )
    effects = store.list_side_effects(file_path=file_path)
    builder.add(
        "Side effects",
        [
            (
                f"{item.kind.value}: {item.detail} at {item.file_path}:{item.line}",
                (f"{item.file_path}:{item.line}",),
            )
            for item in sorted(effects, key=lambda row: (row.line, row.detail))[:max_related]
        ],
    )
    calls = [
        edge
        for edge in store.list_edges(file_path=file_path, kind=EdgeKind.CALL)
        if edge.resolved
    ]
    builder.add(
        "Calls",
        [
            (
                f"{edge.src} -> {edge.dst} at {edge.file_path}:{edge.line}",
                (f"{edge.file_path}:{edge.line}", edge.src, edge.dst),
            )
            for edge in sorted(calls, key=lambda row: (row.line, row.src, row.dst))[:max_related]
        ],
    )


def _source_section(
    builder: _Builder,
    source_root: Path | None,
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    max_source_lines: int,
) -> None:
    if source_root is None or max_source_lines == 0:
        return
    try:
        text = (source_root / file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines()
    if not lines:
        return
    first = max(1, start_line)
    last = min(len(lines), max(first, end_line), first + max_source_lines - 1)
    builder.add(
        f"Source: {file_path}:{first}-{last}",
        [
            (
                f"{line_number:>5} | {lines[line_number - 1]}",
                (f"{file_path}:{line_number}",),
            )
            for line_number in range(first, last + 1)
        ],
    )


class _Builder:
    """Append sections in priority order without exceeding a character budget."""

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self._lines: list[str] = []
        self._used = 0
        self.citations: list[str] = []
        self._seen_citations: set[str] = set()
        self.truncated = False

    def add(self, title: str, entries: list[tuple[str, tuple[str, ...]]]) -> None:
        if not entries:
            return
        heading = f"{title}\n"
        if self._used + len(heading) + len(entries[0][0]) + 3 > self._budget:
            self.truncated = True
            return
        self._lines.append(heading.rstrip("\n"))
        self._used += len(heading)
        for text, citations in entries:
            line = f"  {text}\n"
            if self._used + len(line) > self._budget:
                self.truncated = True
                break
            self._lines.append(line.rstrip("\n"))
            self._used += len(line)
            self._add_citations(citations)

    def _add_citations(self, citations: tuple[str, ...]) -> None:
        for citation in citations:
            if citation and citation not in self._seen_citations:
                self._seen_citations.add(citation)
                self.citations.append(citation)

    def render(self) -> str:
        return "\n".join(self._lines)
