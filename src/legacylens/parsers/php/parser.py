"""Tree-sitter PHP front-end.

Thin slice: file-level facts plus class-like declarations and named functions/methods
with their source spans. Namespaces are resolved so qualified names match the IR spec
(``App\\Domain\\User``, ``App\\Domain\\User::find``).

Deliberately out of scope for now: parameters, return types, inheritance, imports,
include/require edges, calls, entry points, SQL and side effects. Adding those should
extend this file without changing the parser contract in :mod:`legacylens.parsers.base`.
"""

from __future__ import annotations

import tree_sitter_php
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

from ..base import ParseError, Parser, signature_of, span_of, text_of, with_parse_error

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


class PhpParser(Parser):
    """Extract classes and functions from PHP source using Tree-sitter.

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
        )
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
        namespace: str,
        owner: ClassRecord | None,
        classes: list[ClassRecord],
        class_ids: set[str],
        functions: list[FunctionRecord],
        function_ids: set[str],
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
                    )
                else:
                    namespace = scoped
                continue

            if node_type in _CLASS_KINDS:
                class_record = self._class_record(source, child, file_path, namespace)
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
                        namespace=namespace,
                        owner=class_record,
                        classes=classes,
                        class_ids=class_ids,
                        functions=functions,
                        function_ids=function_ids,
                    )
                continue

            if node_type == "method_declaration":
                if owner is not None:
                    self._append_function(
                        source,
                        child,
                        file_path,
                        namespace=namespace,
                        owner=owner,
                        functions=functions,
                        function_ids=function_ids,
                    )
                continue

            if node_type == "function_definition":
                self._append_function(
                    source,
                    child,
                    file_path,
                    namespace=namespace,
                    owner=None,
                    functions=functions,
                    function_ids=function_ids,
                )
                continue

            if node_type in _SKIP_SUBTREES:
                continue

            self._collect(
                source,
                child,
                file_path,
                namespace=namespace,
                owner=owner,
                classes=classes,
                class_ids=class_ids,
                functions=functions,
                function_ids=function_ids,
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
    ) -> None:
        function = self._function_record(source, node, file_path, namespace=namespace, owner=owner)
        if function is None or function.id in function_ids:
            return
        functions.append(function)
        function_ids.add(function.id)

    def _class_record(
        self,
        source: bytes,
        node: Node,
        file_path: str,
        namespace: str,
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
            modifiers=modifiers,
        )


def _join_namespace(namespace: str, name: str) -> str:
    """Join PHP namespace segments with the ``\\`` separator used by the IR spec."""

    if not namespace:
        return name
    if not name:
        return namespace
    return f"{namespace}\\{name}"
