"""Entry points and the request inputs that reach them.

Parsers record entry points with a syntactic ``handler_ref`` (a class, a ``Type::method``
pair or a JSP path). This module resolves those references against the whole IR, links
servlets, filters and Struts actions to their handler methods, and attaches the request
inputs read by each entry point.

Attribution is deliberately conservative:

* an entry point gets the file-level inputs of its own file, plus the inputs of files it
  includes (for page scripts and JSPs);
* a servlet/filter/action gets the inputs of its resolved handler methods and of functions
  reachable through resolved call edges, up to ``max_call_depth``;
* unresolved handler references and inputs outside any entry point are reported explicitly
  instead of being guessed or dropped.

Inputs are never rewritten: the report references the stored records, so ``raw`` and
confidence stay exactly as the parser produced them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from legacylens.ir.models import (
    ClassRecord,
    EdgeKind,
    EdgeRecord,
    EntryKind,
    EntryPointRecord,
    FileRecord,
    FunctionRecord,
    RequestInputRecord,
)
from legacylens.ir.store import FactStore
from legacylens.parsers.web import web_path

__all__ = [
    "EntryPointDetail",
    "EntryPointsReport",
    "build_entrypoints",
]

_CALL_KINDS = frozenset({EdgeKind.CALL, EdgeKind.INSTANTIATE})
_INCLUDE_KINDS = frozenset({EdgeKind.INCLUDE, EdgeKind.REQUIRE})
_HANDLER_METHODS: dict[EntryKind, tuple[str, ...]] = {
    EntryKind.SERVLET: (
        "doGet",
        "doPost",
        "doPut",
        "doDelete",
        "doHead",
        "doOptions",
        "doTrace",
        "service",
    ),
    EntryKind.FILTER: ("doFilter",),
    EntryKind.ACTION: ("execute",),
}


@dataclass(frozen=True, slots=True)
class EntryPointDetail:
    """One entry point, its resolved handler methods and the inputs that reach it."""

    entry: EntryPointRecord
    handlers: tuple[str, ...]
    inputs: tuple[RequestInputRecord, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            **_entry_dict(self.entry),
            "handlers": list(self.handlers),
            "inputs": [_input_dict(item) for item in self.inputs],
        }


@dataclass(frozen=True, slots=True)
class EntryPointsReport:
    """Resolved entry points plus every request input no entry point accounts for."""

    entries: tuple[EntryPointDetail, ...]
    unassigned_inputs: tuple[RequestInputRecord, ...]
    input_total: int

    def as_dict(self) -> dict[str, object]:
        return {
            "entry_points": len(self.entries),
            "request_inputs": self.input_total,
            "entry_counts": _counts(item.entry.kind.value for item in self.entries),
            "input_counts": _counts(item.kind.value for item in self._all_inputs()),
            "entries": [item.as_dict() for item in self.entries],
            "unassigned_inputs": [_input_dict(item) for item in self.unassigned_inputs],
        }

    def _all_inputs(self) -> Iterable[RequestInputRecord]:
        seen: set[RequestInputRecord] = set()
        for detail in self.entries:
            for item in detail.inputs:
                if item not in seen:
                    seen.add(item)
                    yield item
        for item in self.unassigned_inputs:
            if item not in seen:
                seen.add(item)
                yield item


def build_entrypoints(
    store: FactStore,
    *,
    max_call_depth: int = 3,
    max_include_depth: int = 4,
) -> EntryPointsReport:
    """Resolve stored entry points and attach the request inputs that reach them."""

    if max_call_depth < 0:
        raise ValueError("max_call_depth must be >= 0")
    if max_include_depth < 0:
        raise ValueError("max_include_depth must be >= 0")

    entries = store.list_entry_points()
    functions = store.list_functions()
    classes = store.list_classes()
    edges = store.list_edges()
    inputs = store.list_request_inputs()

    index = _Index(classes, functions, edges, store.list_files())
    inputs_by_function: dict[str, list[RequestInputRecord]] = defaultdict(list)
    inputs_by_file: dict[str, list[RequestInputRecord]] = defaultdict(list)
    for item in inputs:
        inputs_by_file[item.file_path].append(item)
        if item.function_id is not None:
            inputs_by_function[item.function_id].append(item)

    details: list[EntryPointDetail] = []
    assigned: set[RequestInputRecord] = set()
    for entry in sorted(entries, key=_entry_sort_key):
        handlers = index.resolve_handlers(entry)
        collected: set[RequestInputRecord] = {
            item for item in inputs_by_file.get(entry.file_path, ()) if item.function_id is None
        }
        if handlers:
            reachable = index.reachable_functions(
                handlers,
                max_depth=max_call_depth,
            )
            for function_id in reachable:
                collected.update(inputs_by_function.get(function_id, ()))
        else:
            roots = [entry.file_path]
            jsp_file = index.resolve_jsp_file(entry)
            if jsp_file is not None:
                roots.append(jsp_file)
            files = index.reachable_files(roots, max_depth=max_include_depth)
            for file_path in files:
                collected.update(
                    item for item in inputs_by_file.get(file_path, ()) if item.function_id is None
                )
            for function_id in index.reachable_functions(files, max_depth=max_call_depth):
                collected.update(inputs_by_function.get(function_id, ()))

        ordered = tuple(sorted(collected, key=_input_sort_key))
        assigned.update(ordered)
        details.append(
            EntryPointDetail(
                entry=entry,
                handlers=tuple(sorted(handlers)),
                inputs=ordered,
            )
        )

    unassigned = tuple(
        sorted(
            (item for item in inputs if item not in assigned),
            key=_input_sort_key,
        )
    )
    return EntryPointsReport(
        entries=tuple(details),
        unassigned_inputs=unassigned,
        input_total=len(inputs),
    )


class _Index:
    """Whole-project lookups used to resolve handler and include references."""

    def __init__(
        self,
        classes: Iterable[ClassRecord],
        functions: Iterable[FunctionRecord],
        edges: Iterable[EdgeRecord],
        files: Iterable[FileRecord],
    ) -> None:
        self.classes = tuple(classes)
        self.functions = tuple(functions)
        self.file_paths = tuple(item.path for item in files)
        self._classes_by_id = {item.id: item for item in self.classes}
        self._classes_by_qualified: dict[str, list[ClassRecord]] = defaultdict(list)
        self._classes_by_short: dict[str, list[ClassRecord]] = defaultdict(list)
        self._functions_by_qualified: dict[str, list[FunctionRecord]] = defaultdict(list)
        self._functions_by_name: dict[str, list[FunctionRecord]] = defaultdict(list)
        self._functions_by_owner: dict[tuple[str, str], list[FunctionRecord]] = defaultdict(list)
        self._calls_from: dict[str, list[str]] = defaultdict(list)
        self._extends_from: dict[str, list[str]] = defaultdict(list)
        self._includes_from: dict[str, list[str]] = defaultdict(list)
        known_files = set(self.file_paths)

        for item in self.classes:
            self._classes_by_qualified[item.qualified_name].append(item)
            self._classes_by_short[item.name].append(item)
        for item in self.functions:
            self._functions_by_qualified[item.qualified_name].append(item)
            self._functions_by_name[item.name].append(item)
            if item.class_id is not None:
                self._functions_by_owner[(item.class_id, item.name)].append(item)

        # Parser-level edges only resolve references inside the declaring file. Finish
        # project-level call/inheritance/include resolution here so handler inputs can
        # follow helpers and base classes declared elsewhere.
        for edge in edges:
            target = edge.dst if edge.resolved else self._resolve_edge_target(edge)
            if target is None:
                continue
            if edge.kind in _CALL_KINDS:
                self._calls_from[edge.src].append(target)
            elif edge.kind is EdgeKind.EXTENDS:
                self._extends_from[edge.src].append(target)
            elif edge.kind in _INCLUDE_KINDS and target in known_files:
                self._includes_from[edge.file_path].append(target)

    def _resolve_edge_target(self, edge: EdgeRecord) -> str | None:
        if edge.kind in _CALL_KINDS:
            function = self._lookup_function(edge.dst)
            return None if function is None else function.id
        if edge.kind is EdgeKind.EXTENDS:
            class_record = self._find_class(edge.dst)
            return None if class_record is None else class_record.id
        if edge.kind in _INCLUDE_KINDS:
            return self._resolve_file(edge.dst)
        return None

    def resolve_handlers(self, entry: EntryPointRecord) -> set[str]:
        if entry.function_id is not None:
            return {entry.function_id}
        reference = entry.handler_ref
        if not reference:
            return set()
        if "::" in reference:
            return {item.id for item in self._find_functions(reference)}
        if entry.kind is EntryKind.JSP:
            return set()
        methods = _HANDLER_METHODS.get(entry.kind)
        if methods is None:
            return set()
        result: set[str] = set()
        for class_record in self._class_lineage(reference):
            for method in methods:
                result.update(
                    item.id for item in self._functions_by_owner.get((class_record.id, method), ())
                )
            if result:
                break
        return result

    def resolve_jsp_file(self, entry: EntryPointRecord) -> str | None:
        if entry.kind is not EntryKind.JSP or not entry.handler_ref:
            return None
        reference = entry.handler_ref.strip()
        candidates = [
            path
            for path in self.file_paths
            if path == reference.lstrip("/") or web_path(path) == reference
        ]
        return candidates[0] if len(candidates) == 1 else None

    def reachable_functions(self, roots: Iterable[str], *, max_depth: int) -> set[str]:
        return _walk(roots, self._calls_from, max_depth=max_depth)

    def reachable_files(self, roots: Iterable[str], *, max_depth: int) -> set[str]:
        return _walk(roots, self._includes_from, max_depth=max_depth)

    def _find_functions(self, reference: str) -> list[FunctionRecord]:
        value = " ".join(reference.strip().split()).lstrip("\\")
        exact = self._functions_by_qualified.get(value)
        if exact:
            return list(exact)
        owner, _, member = value.rpartition("::")
        if not owner:
            return []
        return [
            item
            for class_record in self._class_lineage(owner)
            for item in self._functions_by_owner.get((class_record.id, member), ())
        ]

    def _lookup_function(self, reference: str) -> FunctionRecord | None:
        """Resolve one call reference without walking inheritance."""

        value = " ".join(reference.strip().split()).lstrip("\\")
        exact = self._functions_by_qualified.get(value, ())
        if len(exact) == 1:
            return exact[0]
        owner, separator, member = value.replace("\\", ".").rpartition("::")
        if separator:
            class_record = self._find_class(owner)
            if class_record is None:
                return None
            matches = self._functions_by_owner.get((class_record.id, member), ())
            return matches[0] if len(matches) == 1 else None
        short = value.replace("\\", ".").rsplit(".", 1)[-1]
        matches = self._functions_by_name.get(short, ())
        return matches[0] if len(matches) == 1 else None

    def _resolve_file(self, reference: str) -> str | None:
        clean = reference.strip().lstrip("/")
        if not clean:
            return None
        if clean in self.file_paths:
            return clean
        suffix = "/" + clean
        candidates = [path for path in self.file_paths if path.endswith(suffix)]
        return candidates[0] if len(candidates) == 1 else None

    def _class_lineage(self, reference: str) -> tuple[ClassRecord, ...]:
        start = self._find_class(reference)
        if start is None:
            return ()
        result: list[ClassRecord] = []
        seen: set[str] = set()
        queue = [start]
        while queue:
            item = queue.pop(0)
            if item.id in seen:
                continue
            seen.add(item.id)
            result.append(item)
            queue.extend(
                self._classes_by_id[parent_id]
                for parent_id in self._extends_from.get(item.id, ())
                if parent_id in self._classes_by_id
            )
        return tuple(result)

    def _find_class(self, reference: str) -> ClassRecord | None:
        value = " ".join(reference.strip().split()).lstrip("\\")
        exact = self._classes_by_qualified.get(value)
        if exact and len(exact) == 1:
            return exact[0]
        short = value.rsplit(".", 1)[-1]
        candidates = self._classes_by_short.get(short, ())
        return candidates[0] if len(candidates) == 1 else None


def _walk(
    roots: Iterable[str],
    adjacency: Mapping[str, list[str]],
    *,
    max_depth: int,
) -> set[str]:
    visited: set[str] = set()
    frontier = [item for item in dict.fromkeys(roots) if item]
    depth = 0
    while frontier:
        next_frontier: list[str] = []
        for node in frontier:
            if node in visited:
                continue
            visited.add(node)
            if depth < max_depth:
                next_frontier.extend(adjacency.get(node, ()))
        frontier = next_frontier
        depth += 1
    return visited


def _entry_sort_key(entry: EntryPointRecord) -> tuple[str, int, str, str]:
    return (entry.file_path, entry.line, entry.kind.value, entry.path)


def _input_sort_key(item: RequestInputRecord) -> tuple[str, int, str, str, str]:
    return (
        item.file_path,
        item.line,
        item.kind.value,
        item.name,
        item.raw or "",
    )


def _entry_dict(entry: EntryPointRecord) -> dict[str, object]:
    return {
        "kind": entry.kind.value,
        "path": entry.path,
        "file_path": entry.file_path,
        "line": entry.line,
        "http_method": None if entry.http_method is None else entry.http_method.value,
        "framework": entry.framework,
        "handler_ref": entry.handler_ref,
        "function_id": entry.function_id,
        "requires_auth": entry.requires_auth,
        "auth_hint": entry.auth_hint,
        "confidence": entry.confidence.value,
    }


def _input_dict(item: RequestInputRecord) -> dict[str, object]:
    return {
        "kind": item.kind.value,
        "name": item.name,
        "file_path": item.file_path,
        "line": item.line,
        "function_id": item.function_id,
        "framework": item.framework,
        "raw": item.raw,
        "confidence": item.confidence.value,
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for value in values:
        result[value] += 1
    return {key: result[key] for key in sorted(result)}
