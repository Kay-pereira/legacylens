"""Parser front-ends and the language registry.

Callers hand a :class:`~legacylenscli.ir.models.FileRecord` and the file's bytes to
:func:`parse_source`; the registry selects the front-end for the record's language.
Adding Java, JSP or XML means registering another :class:`Parser` subclass here.
"""

from __future__ import annotations

from legacylenscli.ir.models import FileFacts, FileRecord, Language

from .base import ParseError, Parser, describe_tree_error, signature_of, span_of, text_of
from .java import JavaParser, JspParser, XmlConfigParser
from .php import PhpParser

__all__ = [
    "JavaParser",
    "JspParser",
    "ParseError",
    "Parser",
    "PhpParser",
    "XmlConfigParser",
    "describe_tree_error",
    "parse_source",
    "parser_for",
    "signature_of",
    "span_of",
    "text_of",
]

_PARSERS: dict[Language, type[Parser]] = {
    Language.PHP: PhpParser,
    Language.JAVA: JavaParser,
    Language.JSP: JspParser,
    Language.XML: XmlConfigParser,
}


def parser_for(language: Language | str) -> Parser:
    """Return a fresh parser for ``language``.

    Instances are created per call because Tree-sitter parsers hold mutable state and are
    not thread-safe.
    """

    try:
        parser_type = _PARSERS[Language(language)]
    except ValueError as exc:
        raise LookupError(f"no parser registered for language {language!r}") from exc
    return parser_type()


def parse_source(source: bytes, file: FileRecord) -> FileFacts:
    """Parse one file's bytes into IR facts using the parser for its language."""

    try:
        parser_type = _PARSERS[file.language]
    except KeyError as exc:
        raise ParseError(
            f"no parser registered for language {file.language.value!r} ({file.path!r})"
        ) from exc
    return parser_type().parse(source, file)
