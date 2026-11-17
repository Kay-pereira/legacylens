"""Tree-sitter Java front-end.

Package-qualified class-like declarations, named methods and constructors, plus
relationship edges. The IR needs no Java-specific fields, which is the point of this
slice.

Edges are resolved against the declaring file only: a superclass or method declared in
the same file resolves, while imports, cross-file calls and calls through unknown
receivers stay unresolved with a stable reference in ``dst`` and the exact expression in
``raw``. The project-level pass in :mod:`legacylens.analysis.graph` resolves what it can
against the whole IR.

Java allows method overloading, but function keys are unique within a file. When a class
declares several methods with the same name, this parser disambiguates them with the
declared parameter types (``Calc::add(int,int)``) so no declaration is silently dropped.

Deliberately out of scope for now: annotation-type declarations, structured annotations,
entry points, SQL extraction and side effects.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace

import tree_sitter_java
from tree_sitter import Language as TreeSitterLanguage
from tree_sitter import Node
from tree_sitter import Parser as TreeSitterParser

from legacylens.ir.models import (
    ClassKind,
    ClassRecord,
    Confidence,
    EdgeKind,
    EdgeRecord,
    FileFacts,
    FileRecord,
    FunctionRecord,
    Language,
    Visibility,
)

from ..ast_metrics import measure_function
from ..base import ParseError, Parser, signature_of, span_of, text_of, with_parse_error
from ..edges import make_edge, resolve_local_edges, trim_type

__all__ = ["JavaParser"]

JAVA_GRAMMAR = TreeSitterLanguage(tree_sitter_java.language())

_CLASS_KINDS = {
    "class_declaration": ClassKind.CLASS,
    "interface_declaration": ClassKind.INTERFACE,
    "enum_declaration": ClassKind.ENUM,
    "record_declaration": ClassKind.RECORD,
}
_CONTAINER_TYPES = frozenset(
    {
        "class_body",
        "interface_body",
        "enum_body",
        "enum_body_declarations",
    }
)
_MODIFIER_KEYWORDS = frozenset(
    {
        "public",
        "protected",
        "private",
        "static",
        "abstract",
        "final",
        "native",
        "synchronized",
        "strictfp",
        "default",
        "transient",
        "volatile",
        "sealed",
        "non-sealed",
    }
)
_VISIBILITIES = {item.value: item for item in Visibility}
_FUNCTION_TYPES = frozenset({"method_declaration", "constructor_declaration"})
_TYPE_NODE_TYPES = frozenset(
    {
        "annotated_type",
        "array_type",
        "boolean_type",
        "floating_point_type",
        "generic_type",
        "integral_type",
        "scoped_type_identifier",
        "type_identifier",
        "void_type",
    }
)
_CALL_SKIP_TYPES = frozenset(_CLASS_KINDS)


class JavaParser(Parser):
    """Extract declarations and relationship edges from Java source using Tree-sitter.

    A parser instance owns one Tree-sitter parser and is not thread-safe; create one per
    worker.
    """

    language = Language.JAVA

    def __init__(self) -> None:
        self._parser = TreeSitterParser(JAVA_GRAMMAR)

    def parse(self, source: bytes, file: FileRecord) -> FileFacts:
        if not self.supports(file):
            raise ParseError(f"JavaParser cannot parse {file.language.value} file {file.path!r}")

        tree = self._parser.parse(source)
        classes: list[ClassRecord] = []
        functions: list[FunctionRecord] = []
        edges: list[EdgeRecord] = []
        overload_keys: dict[int, str] = {}
        pending_sources: dict[str, FunctionRecord] = {}
        self._collect(
            source,
            tree.root_node,
            file.path,
            prefix=_package_name(source, tree.root_node),
            owner=None,
            classes=classes,
            class_ids=set(),
            functions=functions,
            function_slots=set(),
            overload_keys=overload_keys,
            edges=edges,
            pending_sources=pending_sources,
            class_types={},
        )
        functions, final_functions = _disambiguate_overloads(functions, overload_keys)
        edges = [
            replace(edge, src=final_functions[id(pending_sources[edge.src])].id)
            if edge.src in pending_sources
            else edge
            for edge in edges
        ]
        return FileFacts(
            file=with_parse_error(file, tree.root_node),
            classes=tuple(classes),
            functions=tuple(functions),
            edges=resolve_local_edges(edges, classes, functions),
        )

    def _collect(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        *,
        prefix: str,
        owner: ClassRecord | None,
        classes: list[ClassRecord],
        class_ids: set[str],
        functions: list[FunctionRecord],
        function_slots: set[str],
        overload_keys: dict[int, str],
        edges: list[EdgeRecord],
        pending_sources: dict[str, FunctionRecord],
        class_types: dict[str, str],
    ) -> None:
        """Walk one declaration container, carrying the package/type prefix."""

        for child in node.named_children:
            node_type = child.type

            if node_type == "import_declaration":
                self._import_edge(source, child, file_path, edges)
                continue

            if node_type in _CLASS_KINDS:
                heritage = _heritage(source, child)
                class_record = self._class_record(source, child, file_path, prefix, heritage)
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
                        prefix=class_record.qualified_name,
                        owner=class_record,
                        classes=classes,
                        class_ids=class_ids,
                        functions=functions,
                        function_slots=function_slots,
                        overload_keys=overload_keys,
                        edges=edges,
                        pending_sources=pending_sources,
                        class_types=self._field_types(source, body),
                    )
                continue

            if node_type in _FUNCTION_TYPES:
                if owner is not None:
                    function = self._append_function(
                        source,
                        child,
                        file_path,
                        owner=owner,
                        functions=functions,
                        function_slots=function_slots,
                        overload_keys=overload_keys,
                    )
                    if function is not None:
                        token = f"pending:{len(pending_sources)}"
                        pending_sources[token] = function
                        self._collect_calls(
                            source,
                            child.child_by_field_name("body"),
                            file_path,
                            src=token,
                            owner=owner,
                            type_env=self._method_types(source, child, class_types),
                            edges=edges,
                        )
                continue

            if node_type == "field_declaration":
                if owner is not None:
                    self._collect_calls(
                        source,
                        child,
                        file_path,
                        src=file_path,
                        owner=owner,
                        type_env=class_types,
                        edges=edges,
                    )
                continue

            if node_type in _CONTAINER_TYPES:
                self._collect(
                    source,
                    child,
                    file_path,
                    prefix=prefix,
                    owner=owner,
                    classes=classes,
                    class_ids=class_ids,
                    functions=functions,
                    function_slots=function_slots,
                    overload_keys=overload_keys,
                    edges=edges,
                    pending_sources=pending_sources,
                    class_types=class_types,
                )

    def _append_function(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        *,
        owner: ClassRecord,
        functions: list[FunctionRecord],
        function_slots: set[str],
        overload_keys: dict[int, str],
    ) -> FunctionRecord | None:
        function = self._function_record(source, node, file_path, owner=owner)
        if function is None:
            return None
        parameter_key = _parameter_key(source, node)
        # Overloads share the plain Type::name key until the post-pass runs, so dedupe
        # on name plus parameter types instead of the derived IR key.
        slot = f"{function.qualified_name}({parameter_key})"
        if slot in function_slots:
            return None
        functions.append(function)
        function_slots.add(slot)
        overload_keys[id(function)] = parameter_key
        return function

    def _class_record(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        prefix: str,
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
            qualified_name=_join_qualified(prefix, name),
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
        owner: ClassRecord,
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

        modifiers = _modifiers(source, node)
        explicit = next(
            (_VISIBILITIES[modifier] for modifier in modifiers if modifier in _VISIBILITIES),
            None,
        )
        is_interface = owner.kind is ClassKind.INTERFACE
        visibility = explicit or (Visibility.PUBLIC if is_interface else Visibility.PACKAGE)
        has_body = node.child_by_field_name("body") is not None
        is_abstract = "abstract" in modifiers or (
            is_interface and not has_body and not {"default", "static", "private"} & set(modifiers)
        )
        metrics = measure_function(node.child_by_field_name("body"), self.language)
        return FunctionRecord(
            file_path=file_path,
            name=name,
            qualified_name=f"{owner.qualified_name}::{name}",
            signature=signature,
            span=span_of(node),
            class_id=owner.id,
            visibility=visibility,
            is_static="static" in modifiers,
            is_abstract=is_abstract,
            cyclomatic_complexity=metrics.cyclomatic_complexity,
            max_nesting=metrics.max_nesting,
            modifiers=modifiers,
        )

    # -- edge extraction ---------------------------------------------------

    def _import_edge(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        edges: list[EdgeRecord],
    ) -> None:
        reference = _import_reference(source, node)
        if not reference:
            return
        edges.append(
            make_edge(
                src=file_path,
                reference=reference,
                kind=EdgeKind.IMPORT,
                file_path=file_path,
                line=node.start_point.row + 1,
                raw=text_of(source, node),
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

    def _collect_calls(
        self,
        source: bytes,
        node: Node | None,
        file_path: str,
        *,
        src: str,
        owner: ClassRecord | None,
        type_env: dict[str, str],
        edges: list[EdgeRecord],
    ) -> None:
        if node is None:
            return
        if node.type == "method_invocation":
            self._method_call_edge(source, node, file_path, src, owner, type_env, edges)
        elif node.type in _CALL_SKIP_TYPES:
            return
        for child in node.named_children:
            self._collect_calls(
                source,
                child,
                file_path,
                src=src,
                owner=owner,
                type_env=type_env,
                edges=edges,
            )

    def _method_call_edge(
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
        method = text_of(source, name_node)
        receiver, dynamic = _receiver_type(source, object_node, owner, type_env)
        if receiver is None:
            reference = _callee_text(source, object_node, method)
        else:
            reference = f"{receiver}::{method}"
        edges.append(
            make_edge(
                src=src,
                reference=reference,
                kind=EdgeKind.CALL,
                file_path=file_path,
                line=node.start_point.row + 1,
                raw=text_of(source, node),
                confidence=Confidence.UNSUPPORTED if dynamic else Confidence.INFERRED,
            )
        )

    # -- type environments -------------------------------------------------

    def _field_types(self, source: bytes, body: Node) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in body.named_children:
            if child.type != "field_declaration":
                continue
            type_node = child.child_by_field_name("type")
            if type_node is None:
                continue
            type_name = trim_type(text_of(source, type_node))
            if not type_name:
                continue
            for declarator in child.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is not None:
                    result[text_of(source, name_node)] = type_name
        return result

    def _method_types(
        self,
        source: bytes,
        node: Node,
        class_types: dict[str, str],
    ) -> dict[str, str]:
        environment = dict(class_types)
        parameters = node.child_by_field_name("parameters")
        if parameters is not None:
            for parameter in parameters.named_children:
                type_name, name = _parameter_type_name(source, parameter)
                if type_name and name:
                    environment[name] = type_name
        body = node.child_by_field_name("body")
        for declaration in _nodes_of_type(body, "local_variable_declaration"):
            type_node = declaration.child_by_field_name("type")
            if type_node is None:
                continue
            type_name = trim_type(text_of(source, type_node))
            if not type_name or type_name == "var":
                continue
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is not None:
                    environment[text_of(source, name_node)] = type_name
        for binding_type in (
            "enhanced_for_statement",
            "resource",
            "catch_formal_parameter",
        ):
            for binding in _nodes_of_type(body, binding_type):
                type_name, name = _parameter_type_name(source, binding)
                if type_name and name:
                    environment[name] = type_name
        return environment


@dataclass(frozen=True, slots=True)
class _Heritage:
    """Raw Java inheritance references, with generic arguments removed."""

    bases: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()

    @property
    def extends(self) -> str | None:
        return self.bases[0] if self.bases else None


def _heritage(source: bytes, node: Node) -> _Heritage:
    bases: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    for child in node.children:
        if child.type == "superclass":
            type_node = next(
                (item for item in child.named_children if item.type != "extends"),
                None,
            )
            if type_node is not None:
                bases = (trim_type(text_of(source, type_node)),)
        elif child.type in {"super_interfaces", "extends_interfaces"}:
            names = tuple(
                trim_type(text_of(source, item))
                for item in _type_list_children(child)
                if trim_type(text_of(source, item))
            )
            if child.type == "extends_interfaces":
                bases = names
            else:
                interfaces = names
    return _Heritage(bases=bases, interfaces=interfaces)


def _type_list_children(node: Node) -> Iterator[Node]:
    type_list = next(
        (child for child in node.named_children if child.type == "type_list"),
        node,
    )
    for child in type_list.named_children:
        if child.type != "type_list":
            yield child


def _import_reference(source: bytes, node: Node) -> str:
    is_static = any(child.type == "static" for child in node.children)
    has_asterisk = any(child.type == "asterisk" for child in node.children)
    identifier = next(
        (child for child in node.named_children if child.type == "scoped_identifier"),
        None,
    )
    if identifier is None:
        return ""
    reference = text_of(source, identifier)
    if has_asterisk:
        reference = f"{reference}.*"
    return f"static {reference}" if is_static else reference


def _parameter_type_name(source: bytes, node: Node) -> tuple[str | None, str | None]:
    type_node = node.child_by_field_name("type")
    name_node = node.child_by_field_name("name")
    if type_node is None:
        type_node = next(
            (child for child in node.named_children if child.type in _TYPE_NODE_TYPES),
            None,
        )
    if name_node is None:
        name_node = next(
            (child for child in reversed(node.named_children) if child.type == "identifier"),
            None,
        )
    type_name = trim_type(text_of(source, type_node)) if type_node is not None else ""
    name = text_of(source, name_node) if name_node is not None else ""
    return (type_name or None), (name or None)


def _receiver_type(
    source: bytes,
    node: Node | None,
    owner: ClassRecord | None,
    type_env: dict[str, str],
) -> tuple[str | None, bool]:
    """Return ``(receiver type, dynamic)`` for a method invocation."""

    if node is None:
        return (owner.qualified_name if owner is not None else None), False
    if node.type == "this":
        return (owner.qualified_name if owner is not None else None), False
    if node.type == "super":
        parent = owner.extends if owner is not None else None
        return parent, parent is None
    if node.type == "identifier":
        text = text_of(source, node)
        known = type_env.get(text)
        if known is not None:
            return known, False
        if text[:1].isupper():
            return text, False
        return None, True
    if node.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
        return trim_type(text_of(source, node)) or None, False
    if node.type == "field_access":
        field_node = node.child_by_field_name("field")
        if field_node is not None:
            known = type_env.get(text_of(source, field_node))
            if known is not None:
                return known, False
        return None, True
    if node.type == "object_creation_expression":
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return trim_type(text_of(source, type_node)) or None, False
    return None, True


def _callee_text(source: bytes, object_node: Node | None, method: str) -> str:
    if object_node is None:
        return method
    return f"{text_of(source, object_node)}.{method}"


def _nodes_of_type(root: Node | None, node_type: str) -> Iterator[Node]:
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if node.type == node_type:
            yield node
        stack.extend(node.named_children)


def _package_name(source: bytes, root: Node) -> str:
    """Return the declared package, or an empty string for the default package."""

    for child in root.named_children:
        if child.type == "package_declaration":
            for named in child.named_children:
                if named.type in {"identifier", "scoped_identifier"}:
                    return " ".join(text_of(source, named).split())
    return ""


def _modifiers(source: bytes, node: Node) -> tuple[str, ...]:
    """Return keyword modifiers in source order; annotations are not modifiers here."""

    for child in node.children:
        if child.type == "modifiers":
            return tuple(
                text_of(source, modifier)
                for modifier in child.children
                if modifier.type in _MODIFIER_KEYWORDS
            )
    return ()


def _parameter_key(source: bytes, node: Node) -> str:
    """Return a compact parameter-type key used only to disambiguate overloads."""

    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        return ""
    types: list[str] = []
    for parameter in parameters.named_children:
        type_node = parameter.child_by_field_name("type")
        raw = text_of(source, type_node) if type_node is not None else text_of(source, parameter)
        types.append(" ".join(raw.split()))
    return ",".join(types)


def _disambiguate_overloads(
    functions: list[FunctionRecord],
    overload_keys: dict[int, str],
) -> tuple[list[FunctionRecord], dict[int, FunctionRecord]]:
    """Append parameter types to keys that would otherwise collide.

    Only overload sets are changed, so ordinary methods keep the plain
    ``Type::method`` form from the IR spec. The returned mapping lets callers rewrite
    edge sources that were collected before final keys existed.
    """

    counts = Counter(function.qualified_name for function in functions)
    seen: set[str] = set()
    result: list[FunctionRecord] = []
    mapping: dict[int, FunctionRecord] = {}
    for function in functions:
        qualified_name = function.qualified_name
        if counts[qualified_name] > 1:
            qualified_name = f"{qualified_name}({overload_keys.get(id(function), '')})"
        if qualified_name in seen:
            qualified_name = f"{qualified_name}@{function.span.start_line}"
        seen.add(qualified_name)
        final = (
            function
            if qualified_name == function.qualified_name
            else replace(function, qualified_name=qualified_name)
        )
        mapping[id(function)] = final
        result.append(final)
    return result, mapping


def _join_qualified(prefix: str, name: str) -> str:
    """Join Java package/type segments with dots."""

    return f"{prefix}.{name}" if prefix else name
