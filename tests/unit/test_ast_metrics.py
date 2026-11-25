"""AST metrics: complexity and nesting for PHP and Java function bodies."""

from __future__ import annotations

import pytest

from legacylenscli.ir import FileRecord, Language
from legacylenscli.parsers import parse_source
from legacylenscli.parsers.ast_metrics import AstMetrics, measure_function


def _functions(source: bytes, language: Language) -> dict[str, tuple[int, int]]:
    record = FileRecord(
        path="src/sample" + (".php" if language is Language.PHP else ".java"),
        language=language,
        content_hash="sha256:" + "0" * 64,
        size_bytes=len(source),
    )
    facts = parse_source(source, record)
    assert facts.file.parse_ok
    return {item.name: (item.cyclomatic_complexity, item.max_nesting) for item in facts.functions}


def test_php_complexity_and_nesting() -> None:
    source = b"""<?php
function complex($items) {
    if ($a && $b) { return 1; } elseif ($c || $d) { return 2; }
    foreach ($items as $item) {
        while ($item) { $item--; }
    }
    switch ($x) { case 1: break; case 2: break; default: break; }
    try { g(); } catch (Exception $e) { h(); }
    return match($x) { 1, 2 => 'a', default => 'b' };
}
function ladder($a, $b) {
    if ($a) { return 1; } else if ($b) { return 2; } else { return 3; }
}
"""
    assert _functions(source, Language.PHP) == {
        "complex": (11, 2),
        "ladder": (3, 1),
    }


def test_java_complexity_and_nesting() -> None:
    source = b"""class C {
  int complex(java.util.List<Integer> items) {
    if (a && b) { return 1; }
    for (int x : items) { while (x > 0) { x--; } }
    switch (x) { case 1: break; case 2: break; default: break; }
    try { g(); } catch (Exception e) { h(); }
    return a ? 1 : 2;
  }
  int ladder(boolean a, boolean b) {
    if (a) { return 1; } else if (b) { return 2; } else { return 3; }
  }
}
"""
    assert _functions(source, Language.JAVA) == {
        "complex": (9, 2),
        "ladder": (3, 1),
    }


def test_bodyless_declaration_is_straight_line_code() -> None:
    facts = parse_source(
        b"interface Api { void call(); }\n",
        FileRecord(
            path="src/Api.java",
            language=Language.JAVA,
            content_hash="sha256:" + "0" * 64,
            size_bytes=32,
        ),
    )
    function = facts.functions[0]
    assert function.cyclomatic_complexity == 1
    assert function.max_nesting == 0


def test_measure_function_without_body_uses_defaults() -> None:
    assert measure_function(None, Language.PHP) == AstMetrics(
        cyclomatic_complexity=1, max_nesting=0
    )


def test_measure_function_rejects_unprofiled_languages() -> None:
    with pytest.raises(ValueError, match="jsp"):
        measure_function(None, Language.JSP)
