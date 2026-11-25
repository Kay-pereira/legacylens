"""Tree-sitter PHP front-end.

Thin slice: file-level facts, class-like declarations and named functions/methods with
source spans, plus relationship edges. Namespaces are resolved so qualified names match
the IR spec (``App\\Domain\\User``, ``App\\Domain\\User::find``).

Edges are resolved against the declaring file only: literal ``include``/``require``
paths normalise to project-relative paths, and calls/inheritance resolve when the target
is declared in the same file. Cross-file and dynamic references stay unresolved with a
stable reference in ``dst`` and the exact expression in ``raw``; the project-level pass
in :mod:`legacylenscli.analysis.graph` resolves them against the whole IR.

Deliberately out of scope for now: parameters and return types as IR fields, annotations,
entry points, SQL extraction and side effects.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass

import tree_sitter_php
from tree_sitter import Language as TreeSitterLanguage
from tree_sitter import Node
from tree_sitter import Parser as TreeSitterParser

from legacylenscli.ir.models import (
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
    Language,
    RequestInputKind,
    RequestInputRecord,
    SideEffectRecord,
    Visibility,
)

from ...analysis.side_effects import (
    SideEffectSite,
    build_side_effects,
    classify_php_function_call,
    classify_php_session_write,
    classify_php_shell_command,
)
from ...analysis.sql_extract import build_data_access, detect_driver, looks_like_sql
from ..ast_metrics import measure_function
from ..base import ParseError, Parser, signature_of, span_of, text_of, with_parse_error
from ..edges import make_edge, resolve_local_edges, trim_type
from ..web import DYNAMIC_INPUT, web_path

__all__ = ["PhpParser"]

PHP_GRAMMAR = TreeSitterLanguage(tree_sitter_php.language_php())

_CLASS_KINDS = {
    "class_declaration": ClassKind.CLASS,
    "interface_declaration": ClassKind.INTERFACE,
    "trait_declaration": ClassKind.TRAIT,
    "enum_declaration": ClassKind.ENUM,
}
_MODIFIER_TYPES = frozenset(
    {
        "visibility_modifier",
        "static_modifier",
        "abstract_modifier",
        "final_modifier",
        "readonly_modifier",
    }
)
_VISIBILITIES = {item.value: item for item in Visibility}
_SKIP_SUBTREES = frozenset({"anonymous_class", "anonymous_function", "arrow_function"})
_INCLUDE_KINDS = {
    "include_expression": EdgeKind.INCLUDE,
    "include_once_expression": EdgeKind.INCLUDE,
    "require_expression": EdgeKind.REQUIRE,
    "require_once_expression": EdgeKind.REQUIRE,
}
_CALL_SKIP_TYPES = frozenset(_CLASS_KINDS) | {"function_definition"}
_PAGE_STATEMENT_TYPES = frozenset(
    {
        "break_statement",
        "continue_statement",
        "do_statement",
        "echo_statement",
        "expression_statement",
        "for_statement",
        "foreach_statement",
        "global_declaration",
        "goto_statement",
        "if_statement",
        "return_statement",
        "switch_statement",
        "text",
        "text_interpolation",
        "try_statement",
        "unset_statement",
        "while_statement",
    }
)
_SUPERGLOBALS = {
    "$_GET": RequestInputKind.QUERY,
    "$_POST": RequestInputKind.FORM,
    "$_REQUEST": RequestInputKind.REQUEST,
    "$_COOKIE": RequestInputKind.COOKIE,
    "$_FILES": RequestInputKind.FILE,
}
_SERVER_INPUTS = {
    "CONTENT_TYPE": (RequestInputKind.HEADER, "content-type"),
    "CONTENT_LENGTH": (RequestInputKind.HEADER, "content-length"),
    "PHP_AUTH_USER": (RequestInputKind.HEADER, "authorization"),
    "PHP_AUTH_PW": (RequestInputKind.HEADER, "authorization"),
    "QUERY_STRING": (RequestInputKind.QUERY, DYNAMIC_INPUT),
    "PATH_INFO": (RequestInputKind.PATH, DYNAMIC_INPUT),
    "REQUEST_URI": (RequestInputKind.PATH, DYNAMIC_INPUT),
    "SCRIPT_NAME": (RequestInputKind.PATH, DYNAMIC_INPUT),
}
_FILTER_INPUTS = {
    "INPUT_GET": RequestInputKind.QUERY,
    "INPUT_POST": RequestInputKind.FORM,
    "INPUT_COOKIE": RequestInputKind.COOKIE,
    "INPUT_REQUEST": RequestInputKind.REQUEST,
}


class PhpParser(Parser):
    """Extract declarations and relationship edges from PHP source using Tree-sitter.

    A parser instance owns one Tree-sitter parser and is not thread-safe; create one per
    worker.
    """

    language = Language.PHP

    def __init__(self) -> None:
        self._parser = TreeSitterParser(PHP_GRAMMAR)

    def parse(self, source: bytes, file: FileRecord) -> FileFacts:
        if not self.supports(file):
            raise ParseError(f"PhpParser cannot parse {file.language.value} file {file.path!r}")

        tree = self._parser.parse(source)
        classes: list[ClassRecord] = []
        functions: list[FunctionRecord] = []
        edges: list[EdgeRecord] = []
        self._collect(
            source,
            tree.root_node,
            file.path,
            namespace="",
            owner=None,
            classes=classes,
            class_ids=set(),
            functions=functions,
            function_ids=set(),
            edges=edges,
            property_types={},
        )
        return FileFacts(
            file=with_parse_error(file, tree.root_node),
            classes=tuple(classes),
            functions=tuple(functions),
            edges=resolve_local_edges(edges, classes, functions),
            entry_points=self._entry_points(source, tree.root_node, file),
            request_inputs=self._request_inputs(source, tree.root_node, file.path, functions),
            data_access=self._data_access(source, tree.root_node, file.path, functions),
            side_effects=self._side_effects(source, tree.root_node, file.path, functions),
        )

    def _entry_points(
        self,
        source: bytes,
        root: Node,
        file: FileRecord,
    ) -> tuple[EntryPointRecord, ...]:
        lines = list(_page_statement_lines(source, root))
        if not lines:
            return ()
        is_cli = _has_shebang(source)
        return (
            EntryPointRecord(
                file_path=file.path,
                kind=EntryKind.CLI if is_cli else EntryKind.ROUTE,
                path=file.path if is_cli else web_path(file.path),
                line=min(lines),
                confidence=Confidence.INFERRED,
            ),
        )

    def _request_inputs(
        self,
        source: bytes,
        root: Node,
        file_path: str,
        functions: list[FunctionRecord],
    ) -> tuple[RequestInputRecord, ...]:
        spans = [
            (function.span.start_line, function.span.end_line, function.id)
            for function in functions
        ]
        records: list[RequestInputRecord] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "variable_name":
                record = _superglobal_input(source, node, file_path, spans)
            elif node.type == "function_call_expression":
                record = _filter_input(source, node, file_path, spans)
            else:
                record = None
            if record is not None:
                records.append(record)
            stack.extend(node.named_children)
        records.sort(key=lambda item: (item.line, item.kind.value, item.name, item.raw or ""))
        return tuple(records)

    def _data_access(
        self,
        source: bytes,
        root: Node,
        file_path: str,
        functions: list[FunctionRecord],
    ) -> tuple[DataAccessRecord, ...]:
        snippets = _sql_snippets(source, root)
        if not snippets:
            return ()
        return build_data_access(
            file_path=file_path,
            snippets=snippets,
            functions=functions,
            driver=detect_driver(Language.PHP, source.decode("utf-8", errors="replace")),
        )

    def _side_effects(
        self,
        source: bytes,
        root: Node,
        file_path: str,
        functions: list[FunctionRecord],
    ) -> tuple[SideEffectRecord, ...]:
        spans = [
            (function.span.start_line, function.span.end_line, function.id)
            for function in functions
        ]
        shadowed = _declared_functions(source, root)
        sites: list[SideEffectSite] = []
        stack = [root]
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type == "function_call_expression":
                site = _function_call_site(source, node, spans, shadowed)
                if site is not None:
                    sites.append(site)
            elif node_type == "shell_command_expression":
                line = node.start_point.row + 1
                sites.append(
                    SideEffectSite(
                        candidate=classify_php_shell_command(text_of(source, node)),
                        line=line,
                        function_id=_function_for_line(spans, line),
                    )
                )
            elif node_type in {"assignment_expression", "augmented_assignment_expression"}:
                site = _session_assignment(source, node, spans)
                if site is not None:
                    sites.append(site)
            elif node_type == "unset_statement":
                sites.extend(_session_unset(source, node, spans))
            stack.extend(node.named_children)
        return build_side_effects(file_path, sites)

    def _collect(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        *,
        namespace: str,
        owner: ClassRecord | None,
        classes: list[ClassRecord],
        class_ids: set[str],
        functions: list[FunctionRecord],
        function_ids: set[str],
        edges: list[EdgeRecord],
        property_types: dict[str, str],
    ) -> None:
        """Walk one scope, carrying the active namespace and owning class."""

        for child in node.named_children:
            node_type = child.type

            if node_type == "namespace_definition":
                name_node = child.child_by_field_name("name")
                name = text_of(source, name_node) if name_node is not None else ""
                scoped = _join_namespace(namespace, name)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect(
                        source,
                        body,
                        file_path,
                        namespace=scoped,
                        owner=None,
                        classes=classes,
                        class_ids=class_ids,
                        functions=functions,
                        function_ids=function_ids,
                        edges=edges,
                        property_types={},
                    )
                else:
                    namespace = scoped
                continue

            if node_type == "namespace_use_declaration":
                self._namespace_import_edges(source, child, file_path, edges)
                continue

            if node_type in _CLASS_KINDS:
                heritage = _heritage(source, child)
                class_record = self._class_record(source, child, file_path, namespace, heritage)
                if class_record is None or class_record.id in class_ids:
                    continue
                classes.append(class_record)
                class_ids.add(class_record.id)
                self._inheritance_edges(source, child, class_record, heritage, edges)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect(
                        source,
                        body,
                        file_path,
                        namespace=namespace,
                        owner=class_record,
                        classes=classes,
                        class_ids=class_ids,
                        functions=functions,
                        function_ids=function_ids,
                        edges=edges,
                        property_types=self._property_types(source, body),
                    )
                continue

            if node_type == "method_declaration":
                if owner is not None:
                    function = self._append_function(
                        source,
                        child,
                        file_path,
                        namespace=namespace,
                        owner=owner,
                        functions=functions,
                        function_ids=function_ids,
                    )
                    if function is not None:
                        self._collect_calls(
                            source,
                            child.child_by_field_name("body"),
                            file_path,
                            src=function.id,
                            owner=owner,
                            edges=edges,
                            type_env=self._method_types(source, child, property_types),
                        )
                continue

            if node_type == "function_definition":
                function = self._append_function(
                    source,
                    child,
                    file_path,
                    namespace=namespace,
                    owner=None,
                    functions=functions,
                    function_ids=function_ids,
                )
                if function is not None:
                    self._collect_calls(
                        source,
                        child.child_by_field_name("body"),
                        file_path,
                        src=function.id,
                        owner=owner,
                        edges=edges,
                        type_env=self._method_types(source, child, property_types),
                    )
                continue

            if node_type == "use_declaration":
                if owner is not None:
                    self._trait_edges(source, child, owner, edges)
                continue

            if node_type in _SKIP_SUBTREES:
                continue

            # Statements at file, namespace or class level (for example property
            # initialisers and top-level script code). Calls are attributed to the file
            # unless a function context was established above.
            self._collect_calls(
                source,
                child,
                file_path,
                src=file_path,
                owner=owner,
                edges=edges,
                type_env=property_types,
            )

    def _append_function(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        *,
        namespace: str,
        owner: ClassRecord | None,
        functions: list[FunctionRecord],
        function_ids: set[str],
    ) -> FunctionRecord | None:
        function = self._function_record(source, node, file_path, namespace=namespace, owner=owner)
        if function is None or function.id in function_ids:
            return None
        functions.append(function)
        function_ids.add(function.id)
        return function

    def _class_record(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        namespace: str,
        heritage: _Heritage,
    ) -> ClassRecord | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = text_of(source, name_node)
        if not name:
            return None
        return ClassRecord(
            file_path=file_path,
            name=name,
            qualified_name=_join_namespace(namespace, name),
            kind=_CLASS_KINDS[node.type],
            span=span_of(node),
            extends=heritage.extends,
            implements=heritage.interfaces,
        )

    def _function_record(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        *,
        namespace: str,
        owner: ClassRecord | None,
    ) -> FunctionRecord | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = text_of(source, name_node)
        if not name:
            return None

        signature = signature_of(source, node)
        if not signature:
            return None

        modifiers = tuple(
            text_of(source, child) for child in node.children if child.type in _MODIFIER_TYPES
        )
        visibility = next(
            (_VISIBILITIES[modifier] for modifier in modifiers if modifier in _VISIBILITIES),
            Visibility.PUBLIC,
        )
        qualified_name = (
            f"{owner.qualified_name}::{name}"
            if owner is not None
            else _join_namespace(namespace, name)
        )
        metrics = measure_function(node.child_by_field_name("body"), self.language)
        return FunctionRecord(
            file_path=file_path,
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            span=span_of(node),
            class_id=owner.id if owner is not None else None,
            visibility=visibility,
            is_static="static" in modifiers,
            is_abstract="abstract" in modifiers,
            cyclomatic_complexity=metrics.cyclomatic_complexity,
            max_nesting=metrics.max_nesting,
            modifiers=modifiers,
        )

    # -- edge extraction ---------------------------------------------------

    def _namespace_import_edges(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        edges: list[EdgeRecord],
    ) -> None:
        raw = text_of(source, node)
        for name in _namespace_use_names(source, node):
            edges.append(
                make_edge(
                    src=file_path,
                    reference=name,
                    kind=EdgeKind.IMPORT,
                    file_path=file_path,
                    line=node.start_point.row + 1,
                    raw=raw,
                )
            )

    def _inheritance_edges(
        self,
        source: bytes,
        node: Node,
        record: ClassRecord,
        heritage: _Heritage,
        edges: list[EdgeRecord],
    ) -> None:
        line = node.start_point.row + 1
        for name in heritage.bases:
            edges.append(
                make_edge(
                    src=record.id,
                    reference=name,
                    kind=EdgeKind.EXTENDS,
                    file_path=record.file_path,
                    line=line,
                    raw=name,
                )
            )
        for name in heritage.interfaces:
            edges.append(
                make_edge(
                    src=record.id,
                    reference=name,
                    kind=EdgeKind.IMPLEMENTS,
                    file_path=record.file_path,
                    line=line,
                    raw=name,
                )
            )

    def _trait_edges(
        self,
        source: bytes,
        node: Node,
        owner: ClassRecord,
        edges: list[EdgeRecord],
    ) -> None:
        for child in node.named_children:
            if child.type not in {"name", "qualified_name"}:
                continue
            edges.append(
                make_edge(
                    src=owner.id,
                    reference=text_of(source, child),
                    kind=EdgeKind.USES_TRAIT,
                    file_path=owner.file_path,
                    line=child.start_point.row + 1,
                    raw=text_of(source, child),
                )
            )

    def _collect_calls(
        self,
        source: bytes,
        node: Node | None,
        file_path: str,
        *,
        src: str,
        owner: ClassRecord | None,
        edges: list[EdgeRecord],
        type_env: dict[str, str],
    ) -> None:
        """Collect includes and calls under ``node``, attributing them to ``src``."""

        if node is None:
            return
        node_type = node.type

        if node_type in _INCLUDE_KINDS:
            self._include_edge(source, node, file_path, edges)
        elif node_type == "function_call_expression":
            self._function_call_edge(source, node, file_path, src, edges)
        elif node_type == "member_call_expression":
            self._member_call_edge(source, node, file_path, src, owner, type_env, edges)
        elif node_type == "scoped_call_expression":
            self._scoped_call_edge(source, node, file_path, src, owner, edges)
        elif node_type in _CALL_SKIP_TYPES:
            return

        for child in node.named_children:
            self._collect_calls(
                source,
                child,
                file_path,
                src=src,
                owner=owner,
                edges=edges,
                type_env=type_env,
            )

    def _include_edge(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        edges: list[EdgeRecord],
    ) -> None:
        expression = node.named_children[-1] if node.named_children else None
        raw = text_of(source, node)
        kind = _INCLUDE_KINDS[node.type]
        resolved_path = _include_path(source, expression, file_path)
        if resolved_path is None:
            reference = text_of(source, expression) if expression is not None else raw
            edges.append(
                make_edge(
                    src=file_path,
                    reference=reference,
                    kind=kind,
                    file_path=file_path,
                    line=node.start_point.row + 1,
                    raw=raw,
                    confidence=Confidence.UNSUPPORTED,
                )
            )
            return
        edges.append(
            make_edge(
                src=file_path,
                reference=resolved_path,
                kind=kind,
                file_path=file_path,
                line=node.start_point.row + 1,
                resolved=True,
                raw=raw,
                confidence=Confidence.VERIFIED,
            )
        )

    def _function_call_edge(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        src: str,
        edges: list[EdgeRecord],
    ) -> None:
        function_node = node.child_by_field_name("function")
        raw = text_of(source, node)
        if function_node is None:
            edges.append(
                make_edge(
                    src=src,
                    reference=raw,
                    kind=EdgeKind.CALL,
                    file_path=file_path,
                    line=node.start_point.row + 1,
                    raw=raw,
                    confidence=Confidence.UNSUPPORTED,
                )
            )
            return
        reference = text_of(source, function_node)
        static_name = function_node.type in {"name", "qualified_name"}
        edges.append(
            make_edge(
                src=src,
                reference=reference,
                kind=EdgeKind.CALL,
                file_path=file_path,
                line=node.start_point.row + 1,
                raw=raw,
                confidence=Confidence.INFERRED if static_name else Confidence.UNSUPPORTED,
            )
        )

    def _member_call_edge(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        src: str,
        owner: ClassRecord | None,
        type_env: dict[str, str],
        edges: list[EdgeRecord],
    ) -> None:
        name_node = node.child_by_field_name("name")
        object_node = node.child_by_field_name("object")
        if name_node is None:
            return
        raw = text_of(source, node)
        method = text_of(source, name_node)
        if name_node.type != "name":
            reference = _callee_text(source, object_node, method)
            confidence = Confidence.UNSUPPORTED
        else:
            receiver = _receiver_type(source, object_node, owner, type_env)
            if receiver is None:
                reference = _callee_text(source, object_node, method)
            else:
                reference = f"{receiver}::{method}"
            confidence = Confidence.INFERRED
        edges.append(
            make_edge(
                src=src,
                reference=reference,
                kind=EdgeKind.CALL,
                file_path=file_path,
                line=node.start_point.row + 1,
                raw=raw,
                confidence=confidence,
            )
        )

    def _scoped_call_edge(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        src: str,
        owner: ClassRecord | None,
        edges: list[EdgeRecord],
    ) -> None:
        name_node = node.child_by_field_name("name")
        scope_node = node.child_by_field_name("scope")
        if name_node is None:
            return
        raw = text_of(source, node)
        method = text_of(source, name_node)
        scope_type, dynamic = _scope_type(source, scope_node, owner)
        if name_node.type != "name" or scope_type is None:
            scope_text = text_of(source, scope_node) if scope_node is not None else ""
            reference = f"{scope_text}::{method}" if scope_text else method
            confidence = (
                Confidence.UNSUPPORTED
                if dynamic or name_node.type != "name"
                else Confidence.INFERRED
            )
        else:
            reference = f"{scope_type}::{method}"
            confidence = Confidence.INFERRED
        edges.append(
            make_edge(
                src=src,
                reference=reference,
                kind=EdgeKind.CALL,
                file_path=file_path,
                line=node.start_point.row + 1,
                raw=raw,
                confidence=confidence,
            )
        )

    # -- type environments -------------------------------------------------

    def _property_types(self, source: bytes, body: Node) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in body.named_children:
            if child.type != "property_declaration":
                continue
            type_node = child.child_by_field_name("type")
            if type_node is None:
                continue
            type_name = trim_type(text_of(source, type_node))
            if not type_name:
                continue
            for element in child.named_children:
                if element.type != "property_element":
                    continue
                name_node = next(
                    (item for item in element.named_children if item.type == "variable_name"),
                    None,
                )
                if name_node is not None:
                    result[text_of(source, name_node).lstrip("$")] = type_name
        return result

    def _method_types(
        self,
        source: bytes,
        node: Node,
        property_types: dict[str, str],
    ) -> dict[str, str]:
        environment = dict(property_types)
        parameters = node.child_by_field_name("parameters")
        if parameters is not None:
            for parameter in parameters.named_children:
                type_node = parameter.child_by_field_name("type")
                name_node = parameter.child_by_field_name("name")
                if type_node is None or name_node is None:
                    continue
                name = text_of(source, name_node).lstrip("$")
                type_name = trim_type(text_of(source, type_node))
                if name and type_name:
                    environment[name] = type_name
        body = node.child_by_field_name("body")
        for assignment in _nodes_of_type(body, "assignment_expression"):
            children = assignment.named_children
            if len(children) < 2:
                continue
            left, right = children[0], children[-1]
            if left.type != "variable_name":
                continue
            name = text_of(source, left).lstrip("$")
            type_name = _assigned_type(source, right, property_types, environment)
            if type_name:
                environment[name] = type_name
        return environment


@dataclass(frozen=True, slots=True)
class _Heritage:
    """Raw PHP inheritance references, as written."""

    bases: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()

    @property
    def extends(self) -> str | None:
        return self.bases[0] if self.bases else None


def _heritage(source: bytes, node: Node) -> _Heritage:
    bases: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    for child in node.children:
        if child.type == "base_clause":
            bases = tuple(_type_names(source, child))
        elif child.type == "class_interface_clause":
            interfaces = tuple(_type_names(source, child))
    return _Heritage(bases=bases, interfaces=interfaces)


def _type_names(source: bytes, node: Node) -> Iterator[str]:
    for child in node.named_children:
        if child.type in {"name", "qualified_name"}:
            yield text_of(source, child)


def _namespace_use_names(source: bytes, node: Node) -> tuple[str, ...]:
    group = next(
        (child for child in node.named_children if child.type == "namespace_use_group"),
        None,
    )
    if group is not None:
        prefix_node = next(
            (child for child in node.named_children if child.type == "namespace_name"),
            None,
        )
        prefix = text_of(source, prefix_node) if prefix_node is not None else ""
        return tuple(
            _join_namespace(prefix, _use_clause_name(source, clause))
            for clause in group.named_children
            if clause.type == "namespace_use_clause"
        )
    return tuple(
        _use_clause_name(source, child)
        for child in node.named_children
        if child.type == "namespace_use_clause"
    )


def _use_clause_name(source: bytes, node: Node) -> str:
    text = " ".join(text_of(source, node).split())
    index = text.lower().find(" as ")
    return text[:index].strip() if index >= 0 else text


def _callee_text(source: bytes, object_node: Node | None, method: str) -> str:
    if object_node is None:
        return method
    return f"{text_of(source, object_node)}->{method}"


def _receiver_type(
    source: bytes,
    node: Node | None,
    owner: ClassRecord | None,
    type_env: dict[str, str],
) -> str | None:
    if node is None:
        return owner.qualified_name if owner is not None else None
    if node.type == "variable_name":
        name = text_of(source, node).lstrip("$")
        if name == "this":
            return owner.qualified_name if owner is not None else None
        return type_env.get(name)
    if node.type == "member_access_expression":
        object_node = node.child_by_field_name("object")
        member_node = node.child_by_field_name("name")
        if (
            object_node is not None
            and member_node is not None
            and text_of(source, object_node).lstrip("$") == "this"
        ):
            return type_env.get(text_of(source, member_node))
        return None
    if node.type == "object_creation_expression":
        return _created_type(source, node)
    return None


def _scope_type(
    source: bytes,
    node: Node | None,
    owner: ClassRecord | None,
) -> tuple[str | None, bool]:
    """Return ``(type, dynamic)`` for a ``::`` scope."""

    if node is None:
        return None, True
    if node.type == "relative_scope":
        text = text_of(source, node)
        if text in {"self", "static"}:
            return (owner.qualified_name if owner is not None else None), False
        if text == "parent":
            parent = owner.extends if owner is not None else None
            return parent, False
        return None, True
    if node.type in {"name", "qualified_name"}:
        return text_of(source, node), False
    return None, True


def _created_type(source: bytes, node: Node) -> str | None:
    for child in node.children:
        if child.type in {"name", "qualified_name"}:
            return trim_type(text_of(source, child)) or None
    return None


def _assigned_type(
    source: bytes,
    node: Node,
    property_types: dict[str, str],
    environment: dict[str, str],
) -> str | None:
    if node.type == "object_creation_expression":
        return _created_type(source, node)
    if node.type == "member_access_expression":
        object_node = node.child_by_field_name("object")
        member_node = node.child_by_field_name("name")
        if (
            object_node is not None
            and member_node is not None
            and text_of(source, object_node).lstrip("$") == "this"
        ):
            return property_types.get(text_of(source, member_node))
        return None
    if node.type == "variable_name":
        return environment.get(text_of(source, node).lstrip("$"))
    return None


def _nodes_of_type(root: Node | None, node_type: str) -> Iterator[Node]:
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if node.type == node_type:
            yield node
        stack.extend(node.named_children)


def _include_path(source: bytes, node: Node | None, file_path: str) -> str | None:
    evaluated = _eval_path(source, node, file_path)
    if evaluated is None:
        return None
    value, anchored = evaluated
    if value.startswith("/"):
        return None
    if not anchored:
        value = posixpath.join(posixpath.dirname(file_path), value)
    normalised = posixpath.normpath(value)
    if normalised in {"", "."} or normalised.startswith(".."):
        return None
    return normalised


def _eval_path(
    source: bytes,
    node: Node | None,
    file_path: str,
) -> tuple[str, bool] | None:
    """Evaluate a static include expression to ``(path, anchored_at_project_root)``."""

    if node is None:
        return None
    if node.type == "parenthesized_expression":
        return (
            _eval_path(source, node.named_children[0], file_path) if node.named_children else None
        )
    if node.type in {"string", "encapsed_string"}:
        content = _string_content(source, node)
        return None if content is None else (content, False)
    if node.type == "name":
        text = text_of(source, node)
        if text == "__DIR__":
            return posixpath.dirname(file_path) or ".", True
        if text == "__FILE__":
            return file_path, True
        return None
    if node.type == "binary_expression":
        if not any(child.type == "." for child in node.children) or len(node.named_children) < 2:
            return None
        left = _eval_path(source, node.named_children[0], file_path)
        right = _eval_path(source, node.named_children[-1], file_path)
        if left is None or right is None:
            return None
        return _join_path(left[0], right[0]), left[1] or right[1]
    if node.type == "function_call_expression":
        function_node = node.child_by_field_name("function")
        if function_node is None or text_of(source, function_node) != "dirname":
            return None
        arguments = node.child_by_field_name("arguments")
        if arguments is None or not arguments.named_children:
            return None
        inner = _eval_path(source, arguments.named_children[0], file_path)
        if inner is None:
            return None
        return posixpath.dirname(inner[0].rstrip("/")) or ".", inner[1]
    return None


def _join_path(left: str, right: str) -> str:
    right = right.lstrip("/")
    if left in {"", "."}:
        return right
    if not right:
        return left
    return f"{left.rstrip('/')}/{right}"


def _string_content(source: bytes, node: Node) -> str | None:
    if any(child.type == "variable_name" for child in node.children):
        return None
    parts = [text_of(source, child) for child in node.children if child.type == "string_content"]
    if parts:
        return "".join(parts)
    raw = text_of(source, node)
    return "" if raw in {'""', "''"} else None


def _has_shebang(source: bytes) -> bool:
    return source.lstrip(b"\xef\xbb\xbf").startswith(b"#!")


def _page_statement_lines(source: bytes, root: Node) -> Iterator[int]:
    """Yield the lines of top-level executable statements in a PHP file.

    Declarations, namespace/import statements and include-only statements do not make a
    file a page. Inline HTML and any top-level expression, output or control flow do.
    """

    for child in root.named_children:
        if child.type == "namespace_definition":
            body = child.child_by_field_name("body")
            if body is not None:
                yield from _page_statement_lines(source, body)
            continue
        if child.type in {"php_tag", "namespace_use_declaration", "function_definition"}:
            continue
        if child.type in _CLASS_KINDS:
            continue
        if child.type == "expression_statement":
            expression = child.named_children[0] if child.named_children else None
            if expression is not None and expression.type in _INCLUDE_KINDS:
                continue
        if child.type in _PAGE_STATEMENT_TYPES:
            yield child.start_point.row + 1


def _superglobal_input(
    source: bytes,
    node: Node,
    file_path: str,
    spans: list[tuple[int, int, str]],
) -> RequestInputRecord | None:
    text = text_of(source, node)
    container = node
    index: Node | None = None
    parent = node.parent
    if parent is not None and parent.type == "subscript_expression":
        container = parent
        if len(parent.named_children) >= 2:
            index = parent.named_children[1]
        while container.parent is not None and container.parent.type == "subscript_expression":
            container = container.parent

    if text == "$_SERVER":
        if index is None:
            return None
        key = _literal_key(source, index)
        if key is None:
            return None
        upper = key.upper()
        if upper.startswith("HTTP_"):
            kind = RequestInputKind.HEADER
            name = upper[5:].lower().replace("_", "-")
        else:
            mapped = _SERVER_INPUTS.get(upper)
            if mapped is None:
                return None
            kind, name = mapped
    else:
        kind = _SUPERGLOBALS.get(text)
        if kind is None:
            return None
        name = _literal_key(source, index) if index is not None else None
        name = name or DYNAMIC_INPUT

    line = node.start_point.row + 1
    return RequestInputRecord(
        file_path=file_path,
        kind=kind,
        name=name,
        line=line,
        function_id=_function_for_line(spans, line),
        raw=text_of(source, container),
        confidence=Confidence.VERIFIED if name != DYNAMIC_INPUT else Confidence.INFERRED,
    )


def _filter_input(
    source: bytes,
    node: Node,
    file_path: str,
    spans: list[tuple[int, int, str]],
) -> RequestInputRecord | None:
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return None
    function = text_of(source, function_node).lstrip("\\").lower()
    if function not in {"filter_input", "filter_input_array"}:
        return None
    arguments = node.child_by_field_name("arguments")
    values = _argument_expressions(arguments)
    if not values:
        return None
    source_name = text_of(source, values[0]).lstrip("\\").upper()
    kind = _FILTER_INPUTS.get(source_name)
    if kind is None:
        return None
    name = DYNAMIC_INPUT
    if function == "filter_input" and len(values) > 1:
        name = _literal_key(source, values[1]) or DYNAMIC_INPUT
    line = node.start_point.row + 1
    return RequestInputRecord(
        file_path=file_path,
        kind=kind,
        name=name,
        line=line,
        function_id=_function_for_line(spans, line),
        raw=text_of(source, node),
        confidence=Confidence.VERIFIED if name != DYNAMIC_INPUT else Confidence.INFERRED,
    )


def _argument_expressions(node: Node | None) -> list[Node]:
    if node is None:
        return []
    result: list[Node] = []
    for argument in node.named_children:
        if argument.type == "argument":
            if argument.named_children:
                result.append(argument.named_children[0])
        else:
            result.append(argument)
    return result


def _literal_key(source: bytes, node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"string", "encapsed_string"}:
        content = _string_content(source, node)
        if content is None:
            return None
        value = content.strip()
        return value or None
    if node.type in {"integer", "float"}:
        return text_of(source, node)
    return None


def _function_for_line(
    spans: list[tuple[int, int, str]],
    line: int,
) -> str | None:
    candidates = [item for item in spans if item[0] <= line <= item[1]]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[1] - item[0], item[2]))[2]


# -- side effects -----------------------------------------------------------


def _declared_functions(source: bytes, root: Node) -> set[str]:
    """Return the lower-case names of functions declared in this file."""

    names: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = text_of(source, name_node).lstrip("\\").lower()
                if name:
                    names.add(name)
        stack.extend(node.named_children)
    return names


def _function_call_site(
    source: bytes,
    node: Node,
    spans: list[tuple[int, int, str]],
    shadowed: set[str],
) -> SideEffectSite | None:
    function_node = node.child_by_field_name("function")
    if function_node is None or function_node.type not in {"name", "qualified_name"}:
        return None
    name = text_of(source, function_node)
    if name.lstrip("\\").lower() in shadowed:
        return None
    arguments = [
        text_of(source, item)
        for item in _argument_expressions(node.child_by_field_name("arguments"))
    ]
    candidate = classify_php_function_call(name, arguments)
    if candidate is None:
        return None
    line = node.start_point.row + 1
    return SideEffectSite(
        candidate=candidate,
        line=line,
        function_id=_function_for_line(spans, line),
    )


def _session_assignment(
    source: bytes,
    node: Node,
    spans: list[tuple[int, int, str]],
) -> SideEffectSite | None:
    left = node.child_by_field_name("left")
    if left is None or not _is_session_target(source, left):
        return None
    line = node.start_point.row + 1
    return SideEffectSite(
        candidate=classify_php_session_write(text_of(source, left)),
        line=line,
        function_id=_function_for_line(spans, line),
    )


def _session_unset(
    source: bytes,
    node: Node,
    spans: list[tuple[int, int, str]],
) -> list[SideEffectSite]:
    sites: list[SideEffectSite] = []
    for child in node.named_children:
        if not _is_session_target(source, child):
            continue
        line = child.start_point.row + 1
        sites.append(
            SideEffectSite(
                candidate=classify_php_session_write(text_of(source, child), unset=True),
                line=line,
                function_id=_function_for_line(spans, line),
            )
        )
    return sites


def _is_session_target(source: bytes, node: Node) -> bool:
    """Return ``True`` when ``node`` is ``$_SESSION`` or a subscript of it."""

    current = node
    while current.type == "subscript_expression":
        if not current.named_children:
            return False
        current = current.named_children[0]
    return current.type == "variable_name" and text_of(source, current) == "$_SESSION"


def _join_namespace(namespace: str, name: str) -> str:
    """Join PHP namespace segments with the ``\\`` separator used by the IR spec."""

    if not namespace:
        return name
    if not name:
        return namespace
    return f"{namespace}\\{name}"


# -- SQL string detection ---------------------------------------------------


def _sql_snippets(source: bytes, root: Node) -> tuple[tuple[str, int], ...]:
    """Return ``(statement_text, line)`` for SQL-looking PHP strings.

    Concatenated strings are joined; interpolated fragments become ``<dynamic>`` so the
    statement stays explicit without inventing a value. String literals consumed by a
    concatenation are not reported twice.
    """

    snippets: list[tuple[str, int]] = []
    handled: set[tuple[int, int]] = set()
    _walk_sql(source, root, snippets, handled)
    snippets.sort(key=lambda item: (item[1], item[0]))
    return tuple(snippets)


def _walk_sql(
    source: bytes,
    node: Node,
    snippets: list[tuple[str, int]],
    handled: set[tuple[int, int]],
) -> None:
    key = (node.start_byte, node.end_byte)
    if key in handled:
        return
    if node.type == "binary_expression" and _php_operator(source, node) == ".":
        text, keys = _php_concat(source, node)
        if text and looks_like_sql(text):
            snippets.append((text, node.start_point.row + 1))
            handled.update(keys)
    elif node.type in {"string", "encapsed_string", "heredoc", "nowdoc"}:
        text = _php_literal(source, node)
        if text and looks_like_sql(text):
            snippets.append((text, node.start_point.row + 1))
        handled.add(key)
        return
    for child in node.named_children:
        _walk_sql(source, child, snippets, handled)


def _php_operator(source: bytes, node: Node) -> str | None:
    operator = node.child_by_field_name("operator")
    return None if operator is None else text_of(source, operator)


def _php_concat(source: bytes, node: Node) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    keys: list[tuple[int, int]] = []

    def visit(operand: Node | None) -> None:
        if operand is None:
            return
        keys.append((operand.start_byte, operand.end_byte))
        if operand.type == "parenthesized_expression":
            visit(operand.named_children[0] if operand.named_children else None)
            return
        if operand.type == "binary_expression" and _php_operator(source, operand) == ".":
            visit(operand.child_by_field_name("left"))
            visit(operand.child_by_field_name("right"))
            return
        if operand.type in {"string", "encapsed_string", "heredoc", "nowdoc"}:
            parts.append(_php_literal(source, operand))
            return
        parts.append(DYNAMIC_INPUT)

    visit(node.child_by_field_name("left"))
    visit(node.child_by_field_name("right"))
    return "".join(parts), keys


def _php_literal(source: bytes, node: Node) -> str:
    if node.type in {"heredoc", "nowdoc"}:
        body = next(
            (
                child
                for child in node.named_children
                if child.type in {"heredoc_body", "nowdoc_body"}
            ),
            None,
        )
        if body is None:
            return ""
        raw = text_of(source, body)
        static = (
            {"string_content", "escape_sequence"} if node.type == "heredoc" else {"nowdoc_string"}
        )
        content = _replace_dynamic(body, raw, static)
        return _php_unescape(content) if node.type == "heredoc" else content

    raw = text_of(source, node)
    if node.type == "encapsed_string":
        if len(raw) < 2:
            return ""
        content = _replace_dynamic(
            node,
            raw[1:-1],
            {"string_content", "escape_sequence"},
            content_offset=1,
        )
        return _php_unescape(content)

    quote_index = min(
        (index for index in (raw.find("'"), raw.find('"')) if index >= 0),
        default=-1,
    )
    if quote_index < 0 or len(raw) - quote_index < 2:
        return ""
    content = _replace_dynamic(
        node,
        raw[quote_index + 1 : -1],
        {"string_content", "escape_sequence"},
        content_offset=quote_index + 1,
    )
    if raw[quote_index] == "'":
        return _php_single_unescape(content)
    return _php_unescape(content)


def _replace_dynamic(
    node: Node,
    content: str,
    static_types: set[str],
    *,
    content_offset: int = 0,
) -> str:
    edits: list[tuple[int, int]] = []
    for child in node.named_children:
        if child.type in static_types:
            continue
        start = child.start_byte - node.start_byte - content_offset
        end = child.end_byte - node.start_byte - content_offset
        if 0 <= start < end <= len(content):
            edits.append((start, end))
    for start, end in sorted(edits, reverse=True):
        content = content[:start] + DYNAMIC_INPUT + content[end:]
    # Braced interpolation (``{$name}``) leaves the braces behind; collapse them so the
    # statement keeps a readable ``<dynamic>`` marker.
    return content.replace("{" + DYNAMIC_INPUT + "}", DYNAMIC_INPUT)


_PHP_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "e": "\x1b",
    "f": "\f",
    "\\": "\\",
    "$": "$",
    '"': '"',
}
_PHP_ESCAPE_PATTERN = re.compile(
    r"\\(n|r|t|v|e|f|\\|\$|\"|[0-7]{1,3}|x[0-9A-Fa-f]{1,2}|u\{[0-9A-Fa-f]+\})"
)


def _php_unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape[0].isdigit():
            return chr(int(escape, 8))
        if escape.startswith("x"):
            return chr(int(escape[1:], 16))
        if escape.startswith("u{"):
            return chr(int(escape[2:-1], 16))
        return _PHP_ESCAPES.get(escape, "\\" + escape)

    return _PHP_ESCAPE_PATTERN.sub(replace, text)


def _php_single_unescape(text: str) -> str:
    return text.replace("\\\\", "\\").replace("\\'", "'")
