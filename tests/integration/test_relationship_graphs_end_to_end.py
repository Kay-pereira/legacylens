"""End-to-end: scan and parse fixture apps, then build relationship graphs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylenscli.analysis import build_graphs
from legacylenscli.ingestion import DEFAULT_EXCLUDES, ScanOptions, scan_repository
from legacylenscli.ir import FactStore
from legacylenscli.parsers import parse_source

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_APP_EXCLUDES = (*DEFAULT_EXCLUDES, "legacy_web")

PHP_INDEX = "php_app/index.php"
PHP_USER = "php_app/src/User.php"
PHP_DB = "php_app/inc/db.php"

JAVA_USER = "java_app/src/main/java/com/example/User.java"
JAVA_REPOSITORY = "java_app/src/main/java/com/example/UserRepository.java"
JAVA_SERVLET = "java_app/src/main/java/com/example/UserServlet.java"


def test_relationship_graphs_over_fixture_apps(tmp_path: Path) -> None:
    scan = scan_repository(
        FIXTURES,
        ScanOptions(include=("*.php", "*.java"), exclude=_APP_EXCLUDES),
    )
    assert len(scan.files) == 6

    with FactStore(tmp_path / "facts.db") as store:
        for scanned in scan.files:
            facts = parse_source(scanned.absolute_path.read_bytes(), scanned.record)
            store.replace_file(facts)
        graphs = build_graphs(store)

    includes = graphs.includes
    assert includes.has_edge(PHP_INDEX, PHP_DB)
    assert includes.has_edge(PHP_INDEX, PHP_USER)
    assert includes[PHP_INDEX][PHP_DB]["resolved"]
    assert includes[PHP_INDEX][PHP_DB]["kinds"] == ("require",)

    calls = graphs.calls
    assert calls.has_edge(f"fn:{PHP_USER}#User::find", f"fn:{PHP_DB}#db_connect")
    assert calls.has_edge(f"fn:{PHP_USER}#User::promote", f"fn:{PHP_DB}#db_connect")
    assert calls.has_edge(
        f"fn:{JAVA_REPOSITORY}#com.example.UserRepository::findById",
        f"fn:{JAVA_USER}#com.example.User::getId",
    )
    servlet_do_get = f"fn:{JAVA_SERVLET}#com.example.UserServlet::doGet"
    assert calls.has_edge(
        servlet_do_get,
        f"fn:{JAVA_REPOSITORY}#com.example.UserRepository::findById",
    )
    assert calls.has_edge(servlet_do_get, f"fn:{JAVA_USER}#com.example.User::getEmail")

    # Dynamic and external references remain explicit instead of being guessed.
    assert any(item.reference == "$pdo->prepare" for item in graphs.unresolved)
    assert any(item.reference == "HttpServletRequest::getParameter" for item in graphs.unresolved)
    assert includes.has_edge(
        JAVA_SERVLET,
        "unresolved:import:javax.servlet.http.HttpServlet",
    )

    summary = graphs.summary(top_n=5)
    assert summary["calls"]["resolved_edges"] > 0
    assert summary["calls"]["unresolved_edges"] > 0
    json.dumps(graphs.as_dict(top_n=5))
