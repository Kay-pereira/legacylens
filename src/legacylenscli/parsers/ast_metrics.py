"""AST-derived function metrics shared by the language front-ends.

Cyclomatic complexity is computed as ``1 + decision points``:

* ``if`` / ``elseif`` clauses, loops and ``catch`` clauses;
* ternary expressions;
* non-default ``switch`` cases and ``match`` arms;
* short-circuit/logical operators (``&&``, ``||``, PHP ``and``/``or``/``xor``/``??``).

``max_nesting`` is the deepest chain of control-flow constructs inside the function body.
``else if`` chains are flattened: the nested ``if`` does not increase depth, so an
``if / else if / else`` ladder reports depth 1 rather than 2.

These are deliberately simple, explainable metrics. They are not a compiler-grade
McCabe implementation, and they never inspect the function signature (parameters,
annotations and return types do not affect complexity).
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from legacylenscli.ir.models import Language

__all__ = ["AstMetrics", "measure_function"]

_COMMENT_TYPES = frozenset({"comment", "line_comment", "block_comment"})

_DECISION_TYPES: dict[Language, frozenset[str]] = {
    Language.PHP: frozenset(
        {
            "if_statement",
            "else_if_clause",
            "for_statement",
            "foreach_statement",
            "while_statement",
            "do_statement",
            "catch_clause",
            "conditional_expression",
            "match_conditional_expression",
        }
    ),
    Language.JAVA: frozenset(
        {
            "if_statement",
            "for_statement",
            "enhanced_for_statement",
            "while_statement",
            "do_statement",
            "catch_clause",
            "ternary_expression",
        }
    ),
}
_CASE_TYPES: dict[Language, frozenset[str]] = {
    Language.PHP: frozenset({"case_statement"}),
    Language.JAVA: frozenset({"switch_label"}),
}
_LOGICAL_OPERATORS: dict[Language, frozenset[str]] = {
    Language.PHP: frozenset({"&&", "||", "and", "or", "xor", "??"}),
    Language.JAVA: frozenset({"&&", "||"}),
}
_NESTING_TYPES: dict[Language, frozenset[str]] = {
    Language.PHP: frozenset(
        {
            "if_statement",
            "for_statement",
            "foreach_statement",
            "while_statement",
            "do_statement",
            "switch_statement",
            "try_statement",
        }
    ),
    Language.JAVA: frozenset(
        {
            "if_statement",
            "for_statement",
            "enhanced_for_statement",
            "while_statement",
            "do_statement",
            "switch_expression",
            "try_statement",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class AstMetrics:
    """Metrics derived from one function body."""

    cyclomatic_complexity: int = 1
    max_nesting: int = 0

    def __post_init__(self) -> None:
        if self.cyclomatic_complexity < 1:
            raise ValueError("cyclomatic_complexity must be >= 1")
        if self.max_nesting < 0:
            raise ValueError("max_nesting must be >= 0")


def measure_function(body: Node | None, language: Language) -> AstMetrics:
    """Measure a function body; a body-less declaration is straight-line code."""

    if language not in _DECISION_TYPES:
        raise ValueError(f"no AST metric profile for language {language.value!r}")
    if body is None:
        return AstMetrics()

    decisions = _DECISION_TYPES[language]
    case_types = _CASE_TYPES[language]
    logical_operators = _LOGICAL_OPERATORS[language]
    nesting_types = _NESTING_TYPES[language]

    complexity = 1
    max_nesting = 0

    def visit(node: Node, depth: int) -> None:
        nonlocal complexity, max_nesting

        if node.type in nesting_types and not _is_else_if(node):
            depth += 1
            max_nesting = max(max_nesting, depth)

        if node.type in decisions:
            complexity += 1
        if node.type in case_types and not _is_default(node):
            complexity += 1
        if node.type == "binary_expression":
            complexity += sum(1 for child in node.children if child.type in logical_operators)

        for child in node.named_children:
            visit(child, depth)

    visit(body, 0)
    return AstMetrics(cyclomatic_complexity=complexity, max_nesting=max_nesting)


def _is_default(node: Node) -> bool:
    return any(child.type == "default" for child in node.children)


def _is_else_if(node: Node) -> bool:
    """Whether ``node`` is the ``if`` of an ``else if`` chain rather than nested code."""

    if node.type != "if_statement":
        return False
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in _COMMENT_TYPES:
        sibling = sibling.prev_sibling
    return sibling is not None and sibling.type == "else"
