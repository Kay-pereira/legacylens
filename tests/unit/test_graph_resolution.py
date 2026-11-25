"""Project-level edge resolution and NetworkX relationship graphs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacylenscli.analysis import build_graphs
from legacylenscli.ir import FactStore, FileRecord, Language
from legacylenscli.parsers import parse_source


def _ingest(
    store: FactStore,
    path: str,
    language: Language,
    source: bytes,
) -> None:
    record = FileRecord(
        path=path,
        language=language,
        content_hash="sha256:" + "0" * 64,
        size_bytes=len(source),
    )
    facts = parse_source(source, record)
    assert facts.file.parse_ok, f"{path} did not parse"
    store.replace_file(facts)


PHP_APP = b"""<?php
require_once __DIR__ . '/b.php';
use Contract;

class Alpha extends Beta implements Contract {
    use HelperTrait;

    public function run(): void
    {
        parent::work();
        helper_fn();
        $svc->go();
    }
}
"""

PHP_LIBRARY = b"""<?php
interface Contract {}

trait HelperTrait {}

class Beta implements Contract {
    use HelperTrait;

    public function work(): void {}
}

function helper_fn(): void {}
"""


def test_php_cross_file_edges_resolve_and_stay_explicit(
    tmp_path: Path,
) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        _ingest(store, "a.php", Language.PHP, PHP_APP)
        _ingest(store, "b.php", Language.PHP, PHP_LIBRARY)
        graphs = build_graphs(store)

    # The require and the import both point at b.php and aggregate into one edge.
    includes = graphs.includes
    assert includes.has_edge("a.php", "b.php")
    data = includes["a.php"]["b.php"]
    assert data["count"] == 2
    assert data["resolved_count"] == 2
    assert data["kinds"] == ("import", "require")
    assert data["lines"] == (2, 3)

    inheritance = graphs.inheritance
    assert inheritance.has_edge("class:a.php#Alpha", "class:b.php#Beta")
    assert inheritance.has_edge("class:a.php#Alpha", "class:b.php#Contract")
    assert inheritance.has_edge("class:a.php#Alpha", "class:b.php#HelperTrait")
    assert all(inheritance[src][dst]["resolved"] for src, dst in inheritance.edges)

    calls = graphs.calls
    source = "fn:a.php#Alpha::run"
    assert calls.has_edge(source, "fn:b.php#Beta::work")
    assert calls.has_edge(source, "fn:b.php#helper_fn")

    # An unknown receiver is never guessed: the call stays unresolved.
    dynamic = calls[source]["unresolved:call:$svc->go"]
    assert not dynamic["resolved"]
    assert any(item.reference == "$svc->go" for item in graphs.unresolved)

    summary = graphs.summary(top_n=5)
    assert summary["includes"]["edges"] == 1
    assert summary["includes"]["resolved_edges"] == 2
    assert summary["includes"]["unresolved_edges"] == 0
    assert summary["includes"]["top_fan_out"][0] == {
        "node": "a.php",
        "fan_in": 0,
        "fan_out": 1,
    }
    assert summary["includes"]["top_fan_in"][0] == {
        "node": "b.php",
        "fan_in": 1,
        "fan_out": 0,
    }

    payload = graphs.as_dict(top_n=5)
    assert set(payload) == {"includes", "inheritance", "calls", "unresolved"}
    json.dumps(payload)


def test_ambiguous_and_dangling_targets_are_not_guessed(tmp_path: Path) -> None:
    duplicate = b"<?php class Dup { public static function ping(): void {} }\n"
    caller = b"""<?php
require_once __DIR__ . '/dup1.php';
require_once __DIR__ . '/dup2.php';
require_once 'nowhere.php';

Dup::ping();
"""
    with FactStore(tmp_path / "facts.db") as store:
        _ingest(store, "caller.php", Language.PHP, caller)
        _ingest(store, "dup1.php", Language.PHP, duplicate)
        _ingest(store, "dup2.php", Language.PHP, duplicate)
        graphs = build_graphs(store)

    # Two project classes share the short name, so the call target is ambiguous.
    assert graphs.calls.has_edge("caller.php", "unresolved:call:Dup::ping")
    assert any(item.reference == "Dup::ping" for item in graphs.unresolved)

    # A static require of a file that was never ingested is resolved but dangling.
    assert graphs.includes["caller.php"]["nowhere.php"]["dangling"] is True
    summary = graphs.summary(top_n=5)
    assert summary["includes"]["dangling_edges"] == 1
    assert summary["includes"]["unresolved_edges"] == 0


def test_java_cross_file_imports_inheritance_and_calls(tmp_path: Path) -> None:
    user = b"""package com.example;

public class User {
    private int id;

    public int getId() {
        return id;
    }
}
"""
    admin = b"""package com.example;

public class Admin extends User {
    public int promote() {
        return super.getId();
    }
}
"""
    repository = b"""package com.example;

import com.example.User;

public class Repository {
    private final User user;

    public Repository(User user) {
        this.user = user;
    }

    public int find() {
        return user.getId();
    }
}
"""
    with FactStore(tmp_path / "facts.db") as store:
        prefix = "src/main/java/com/example/"
        _ingest(store, f"{prefix}User.java", Language.JAVA, user)
        _ingest(store, f"{prefix}Admin.java", Language.JAVA, admin)
        _ingest(store, f"{prefix}Repository.java", Language.JAVA, repository)
        graphs = build_graphs(store)

    repository_path = f"{prefix}Repository.java"
    user_path = f"{prefix}User.java"
    admin_path = f"{prefix}Admin.java"

    assert graphs.includes.has_edge(repository_path, user_path)
    assert graphs.includes[repository_path][user_path]["resolved_count"] == 1

    assert graphs.inheritance.has_edge(
        f"class:{admin_path}#com.example.Admin",
        f"class:{user_path}#com.example.User",
    )

    find = f"fn:{repository_path}#com.example.Repository::find"
    get_id = f"fn:{user_path}#com.example.User::getId"
    assert graphs.calls.has_edge(find, get_id)
    assert graphs.calls.has_edge(
        f"fn:{admin_path}#com.example.Admin::promote",
        get_id,
    )


def test_java_imports_match_only_the_exact_qualified_name(tmp_path: Path) -> None:
    exact = b"""package com.example;

public class User {
    public int getId() {
        return 0;
    }
}
"""
    unrelated = b"""package other;

public class User {
    public int getId() {
        return 1;
    }
}
"""
    consumer = b"""package com.example;

import com.example.User;

public class Consumer {
    public User load(User user) {
        user.getId();
        return user;
    }
}
"""
    with FactStore(tmp_path / "facts.db") as store:
        prefix = "src/main/java/"
        _ingest(store, f"{prefix}com/example/User.java", Language.JAVA, exact)
        _ingest(store, f"{prefix}other/User.java", Language.JAVA, unrelated)
        _ingest(store, f"{prefix}com/example/Consumer.java", Language.JAVA, consumer)
        graphs = build_graphs(store)

    consumer_path = f"{prefix}com/example/Consumer.java"
    exact_path = f"{prefix}com/example/User.java"
    unrelated_path = f"{prefix}other/User.java"

    assert graphs.includes.has_edge(consumer_path, exact_path)
    assert not graphs.includes.has_edge(consumer_path, unrelated_path)

    # The short receiver type ``User`` is ambiguous across the project.
    assert graphs.calls.has_edge(
        f"fn:{consumer_path}#com.example.Consumer::load",
        "unresolved:call:User::getId",
    )


def test_include_cycles_are_reported(tmp_path: Path) -> None:
    source_a = b"<?php require_once __DIR__ . '/b.php';\n"
    source_b = b"<?php require_once __DIR__ . '/a.php';\n"
    with FactStore(tmp_path / "facts.db") as store:
        _ingest(store, "a.php", Language.PHP, source_a)
        _ingest(store, "b.php", Language.PHP, source_b)
        graphs = build_graphs(store)

    assert graphs.includes.has_edge("a.php", "b.php")
    assert graphs.includes.has_edge("b.php", "a.php")
    assert graphs.summary(top_n=0)["includes"]["cycles"] == [["a.php", "b.php"]]


def test_summary_rejects_a_negative_top_n(tmp_path: Path) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        graphs = build_graphs(store)
    with pytest.raises(ValueError):
        graphs.summary(top_n=-1)
