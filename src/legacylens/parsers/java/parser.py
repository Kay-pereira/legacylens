"""Tree-sitter Java front-end.

Thin slice matching the PHP parser: package-qualified class-like declarations plus named
methods and constructors, with source spans, visibility and static/abstract flags.

Deliberately out of scope for now: parameters, return types, inheritance, imports,
annotations as structured facts, annotation-type declarations, calls, entry points, SQL
and side effects. The IR needs no Java-specific fields, which is the point of this slice.

Java allows method overloading, but function keys are unique within a file. When a class
declares several methods with the same name, this parser disambiguates them with the
declared parameter types (``Calc::add(int,int)``) so no declaration is silently dropped.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import tree_sitter_java
from tree_sitter import Language as TreeSitterLanguage
from tree_sitter import Node
from tree_sitter import Parser as TreeSitterParser

from legacylens.ir.models import (
    ClassKind,
    ClassRecord,
    FileFacts,
    FileRecord,
    FunctionRecord,
    Language,
    Visibility,
)

from ..ast_metrics import measure_function
from ..base import ParseError, Parser, signature_of, span_of, text_of, with_parse_error

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


class JavaParser(Parser):
    """Extract classes and methods from Java source using Tree-sitter.

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
        overload_keys: dict[int, str] = {}
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
        )
        functions = _disambiguate_overloads(functions, overload_keys)
        return FileFacts(
            file=with_parse_error(file, tree.root_node),
            classes=tuple(classes),
            functions=tuple(functions),
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
    ) -> None:
        """Walk one declaration container, carrying the package/type prefix."""

        for child in node.named_children:
            node_type = child.type

            if node_type in _CLASS_KINDS:
                class_record = self._class_record(source, child, file_path, prefix)
                if class_record is None or class_record.id in class_ids:
                    continue
                classes.append(class_record)
                class_ids.add(class_record.id)
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
                    )
                continue

            if node_type in _FUNCTION_TYPES:
                if owner is not None:
                    self._append_function(
                        source,
                        child,
                        file_path,
                        owner=owner,
                        functions=functions,
                        function_slots=function_slots,
                        overload_keys=overload_keys,
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
    ) -> None:
        function = self._function_record(source, node, file_path, owner=owner)
        if function is None:
            return
        parameter_key = _parameter_key(source, node)
        # Overloads share the plain Type::name key until the post-pass runs, so dedupe
        # on name plus parameter types instead of the derived IR key.
        slot = f"{function.qualified_name}({parameter_key})"
        if slot in function_slots:
            return
        functions.append(function)
        function_slots.add(slot)
        overload_keys[id(function)] = parameter_key

    def _class_record(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        prefix: str,
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
) -> list[FunctionRecord]:
    """Append parameter types to keys that would otherwise collide.

    Only overload sets are changed, so ordinary methods keep the plain
    ``Type::method`` form from the IR spec.
    """

    counts = Counter(function.qualified_name for function in functions)
    seen: set[str] = set()
    result: list[FunctionRecord] = []
    for function in functions:
        qualified_name = function.qualified_name
        if counts[qualified_name] > 1:
            qualified_name = f"{qualified_name}({overload_keys.get(id(function), '')})"
        if qualified_name in seen:
            qualified_name = f"{qualified_name}@{function.span.start_line}"
        seen.add(qualified_name)
        result.append(
            function
            if qualified_name == function.qualified_name
            else replace(function, qualified_name=qualified_name)
        )
    return result


def _join_qualified(prefix: str, name: str) -> str:
    """Join Java package/type segments with dots."""

    return f"{prefix}.{name}" if prefix else name
