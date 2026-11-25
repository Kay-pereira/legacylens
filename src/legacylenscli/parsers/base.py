"""Parser interface: a source file in, IR facts out.

Every language front-end implements :class:`Parser`. A parser receives the bytes of one
file and the :class:`~legacylenscli.ir.models.FileRecord` produced by ingestion, and returns
a complete :class:`~legacylenscli.ir.models.FileFacts` aggregate. Parsers never touch the
store themselves; the pipeline writes the aggregate atomically.

The helpers here keep Tree-sitter handling consistent across languages. They are thin on
purpose: a parser should spend its code on language semantics, not on byte/line
conversions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import ClassVar

from tree_sitter import Node

from legacylenscli.ir.models import FileFacts, FileRecord, Language, Span

__all__ = [
    "ParseError",
    "Parser",
    "describe_tree_error",
    "signature_of",
    "span_of",
    "text_of",
]


class ParseError(ValueError):
    """A file could not be parsed by the selected front-end.

    Recoverable syntax problems belong in ``FileRecord.parse_error``; this exception is
    for programming errors such as handing a Java file to the PHP parser.
    """


class Parser(ABC):
    """Front-end contract for one source language."""

    language: ClassVar[Language]

    @abstractmethod
    def parse(self, source: bytes, file: FileRecord) -> FileFacts:
        """Return all facts extracted from ``source`` for ``file``."""

    def supports(self, file: FileRecord) -> bool:
        """Whether this parser handles ``file``'s language."""

        return file.language is self.language


def text_of(source: bytes, node: Node) -> str:
    """Return the UTF-8 text of ``node``, replacing invalid bytes.

    Legacy PHP is not always valid UTF-8. Facts must still be produced, so undecodable
    bytes become replacement characters instead of failing the whole file.
    """

    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def span_of(node: Node) -> Span:
    """Convert a Tree-sitter node's 0-based rows to a 1-based inclusive :class:`Span`."""

    return Span(
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        start_column=node.start_point.column,
        end_column=node.end_point.column,
    )


def signature_of(source: bytes, node: Node) -> str:
    """Return a declaration header with whitespace collapsed onto one line.

    The header stops at the node's ``body`` field when the grammar exposes one, and at a
    trailing semicolon for abstract/interface declarations. ``signature`` is required by
    :class:`~legacylenscli.ir.models.FunctionRecord`, so an empty result is never returned
    for a real declaration.
    """

    body = node.child_by_field_name("body")
    end_byte = body.start_byte if body is not None else node.end_byte
    raw = source[node.start_byte : end_byte].decode("utf-8", errors="replace")
    signature = " ".join(raw.split())
    return signature.rstrip(";").rstrip()


def describe_tree_error(root: Node) -> str | None:
    """Return a short message for the first syntax error under ``root``, if any."""

    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_missing:
            return f"missing {node.type} at line {node.start_point.row + 1}"
        if node.is_error:
            return f"syntax error at line {node.start_point.row + 1}"
        stack.extend(reversed(node.children))
    return None


def with_parse_error(file: FileRecord, root: Node) -> FileRecord:
    """Return ``file`` with ``parse_error`` set when the syntax tree contains errors.

    Tree-sitter produces partial trees for damaged legacy files. Recording the problem on
    the file keeps downstream confidence honest while still preserving the declarations
    that were recovered.
    """

    message = describe_tree_error(root)
    if message is None or not file.parse_ok:
        return file
    return replace(file, parse_error=message)
