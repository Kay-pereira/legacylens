"""Parser-emitted relationship edges for PHP and Java."""

from __future__ import annotations

from legacylenscli.ir import Confidence, EdgeKind, FileFacts, FileRecord, Language
from legacylenscli.parsers import parse_source


def _record(path: str, language: Language) -> FileRecord:
    return FileRecord(
        path=path,
        language=language,
        content_hash="sha256:" + "0" * 64,
        size_bytes=0,
    )


def _edges(facts: FileFacts, kind: EdgeKind) -> list:
    return [item for item in facts.edges if item.kind is kind]


def _by_raw(facts: FileFacts, raw: str):
    matches = [item for item in facts.edges if item.raw == raw]
    assert len(matches) == 1, f"expected one edge for {raw!r}, got {matches!r}"
    return matches[0]


PHP_SOURCE = b"""<?php
namespace App\\Domain;

use App\\Contracts\\Repo;
use App\\Support\\{Helper, Util};
require_once __DIR__ . '/inc/db.php';
include $dynamic;

interface Repo extends Countable {
    public function find(int $id): ?array;
}

trait Helper {
    public function help(): void {}
}

class Base {
    public function boot(): void {}
}

class User extends Base implements Repo {
    use Helper, Util;
    private Repo $repo;

    public function helper(): void {}

    public function find(int $id): ?array {
        $this->helper();
        self::boot();
        parent::boot();
        User::boot();
        $this->repo->find($id);
        $fn($id);
        $obj->$method();
        db_connect();
        \\Legacy\\Db::connect();
        return null;
    }
}
"""


def test_php_imports_includes_and_inheritance_edges() -> None:
    facts = parse_source(PHP_SOURCE, _record("src/User.php", Language.PHP))

    assert facts.file.parse_ok
    imports = {item.dst for item in _edges(facts, EdgeKind.IMPORT)}
    assert imports == {
        "App\\Contracts\\Repo",
        "App\\Support\\Helper",
        "App\\Support\\Util",
    }
    assert all(not item.resolved for item in _edges(facts, EdgeKind.IMPORT))

    require = _by_raw(facts, "require_once __DIR__ . '/inc/db.php'")
    assert require.kind is EdgeKind.REQUIRE
    assert require.src == "src/User.php"
    assert require.dst == "src/inc/db.php"
    assert require.resolved
    assert require.confidence is Confidence.VERIFIED

    dynamic_include = _by_raw(facts, "include $dynamic")
    assert dynamic_include.kind is EdgeKind.INCLUDE
    assert dynamic_include.dst == "$dynamic"
    assert not dynamic_include.resolved
    assert dynamic_include.confidence is Confidence.UNSUPPORTED

    user = next(item for item in facts.classes if item.name == "User")
    repository = next(item for item in facts.classes if item.name == "Repo")
    helper = next(item for item in facts.classes if item.name == "Helper")
    assert user.extends == "Base"
    assert user.implements == ("Repo",)

    extends = [
        item for item in facts.edges if item.kind is EdgeKind.EXTENDS and item.src == user.id
    ]
    assert len(extends) == 1
    assert extends[0].dst == "class:src/User.php#App\\Domain\\Base"
    assert extends[0].resolved

    implements = [
        item for item in facts.edges if item.kind is EdgeKind.IMPLEMENTS and item.src == user.id
    ]
    assert [(item.dst, item.resolved) for item in implements] == [
        ("class:src/User.php#App\\Domain\\Repo", True)
    ]

    traits = {
        item.dst: item.resolved
        for item in facts.edges
        if item.kind is EdgeKind.USES_TRAIT and item.src == user.id
    }
    assert traits == {
        "class:src/User.php#App\\Domain\\Helper": True,
        "Util": False,
    }
    assert repository.kind.value == "interface"
    assert helper.kind.value == "trait"


def test_php_calls_resolve_locally_and_keep_dynamic_cases_explicit() -> None:
    facts = parse_source(PHP_SOURCE, _record("src/User.php", Language.PHP))
    calls = _edges(facts, EdgeKind.CALL)
    find = next(
        item for item in facts.functions if item.qualified_name == "App\\Domain\\User::find"
    )
    base_boot = next(
        item for item in facts.functions if item.qualified_name == "App\\Domain\\Base::boot"
    )
    repo_find = next(
        item for item in facts.functions if item.qualified_name == "App\\Domain\\Repo::find"
    )

    this_helper = _by_raw(facts, "$this->helper()")
    assert this_helper.src == find.id
    assert this_helper.dst.endswith("#App\\Domain\\User::helper")
    assert this_helper.resolved

    this_repo = _by_raw(facts, "$this->repo->find($id)")
    assert this_repo.dst == repo_find.id
    assert this_repo.resolved

    parent_boot = _by_raw(facts, "parent::boot()")
    assert parent_boot.dst == base_boot.id
    assert parent_boot.resolved

    self_boot = _by_raw(facts, "self::boot()")
    assert self_boot.dst == "App\\Domain\\User::boot"
    assert not self_boot.resolved

    user_boot = _by_raw(facts, "User::boot()")
    assert user_boot.dst == "User::boot"
    assert not user_boot.resolved

    variable_call = _by_raw(facts, "$fn($id)")
    assert variable_call.dst == "$fn"
    assert not variable_call.resolved
    assert variable_call.confidence is Confidence.UNSUPPORTED

    variable_method = _by_raw(facts, "$obj->$method()")
    assert variable_method.dst == "$obj->$method"
    assert not variable_method.resolved
    assert variable_method.confidence is Confidence.UNSUPPORTED

    bare = _by_raw(facts, "db_connect()")
    assert bare.dst == "db_connect"
    assert not bare.resolved
    assert bare.confidence is Confidence.INFERRED

    qualified = _by_raw(facts, "\\Legacy\\Db::connect()")
    assert qualified.dst == "\\Legacy\\Db::connect"
    assert not qualified.resolved

    # Every call is attributable to the declaring method, never to a bare file.
    assert {item.src for item in calls if item.line > 20} == {find.id}


JAVA_SOURCE = b"""package com.example.app;

import com.example.User;
import java.util.List;
import static java.util.Collections.emptyList;
import com.other.*;

public class Service extends BaseService implements Runnable {
    private final UserRepository repository = new UserRepository();

    public void helper() {}

    public void bare() {}

    public void run(User user) {
        this.helper();
        super.helper();
        repository.findById(1);
        user.getEmail();
        Integer.parseInt("1");
        local.call();
        bare();
    }
}
"""


def test_java_imports_inheritance_and_calls() -> None:
    facts = parse_source(JAVA_SOURCE, _record("src/Service.java", Language.JAVA))

    assert facts.file.parse_ok
    imports = {item.dst for item in _edges(facts, EdgeKind.IMPORT)}
    assert imports == {
        "com.example.User",
        "java.util.List",
        "static java.util.Collections.emptyList",
        "com.other.*",
    }

    service = next(item for item in facts.classes if item.name == "Service")
    assert service.extends == "BaseService"
    assert service.implements == ("Runnable",)
    extends = _by_raw(facts, "BaseService")
    assert extends.kind is EdgeKind.EXTENDS
    assert not extends.resolved
    implements = _by_raw(facts, "Runnable")
    assert implements.kind is EdgeKind.IMPLEMENTS
    assert not implements.resolved

    this_helper = _by_raw(facts, "this.helper()")
    assert this_helper.dst.endswith("#com.example.app.Service::helper")
    assert this_helper.resolved

    super_helper = _by_raw(facts, "super.helper()")
    assert super_helper.dst == "BaseService::helper"
    assert not super_helper.resolved

    repository = _by_raw(facts, "repository.findById(1)")
    assert repository.dst == "UserRepository::findById"
    assert not repository.resolved

    user = _by_raw(facts, "user.getEmail()")
    assert user.dst == "User::getEmail"
    assert not user.resolved

    boxed = _by_raw(facts, 'Integer.parseInt("1")')
    assert boxed.dst == "Integer::parseInt"
    assert not boxed.resolved

    dynamic = _by_raw(facts, "local.call()")
    assert dynamic.dst == "local.call"
    assert not dynamic.resolved
    assert dynamic.confidence is Confidence.UNSUPPORTED

    bare = _by_raw(facts, "bare()")
    assert bare.dst.endswith("#com.example.app.Service::bare")
    assert bare.resolved


def test_java_overload_sources_use_the_disambiguated_function_key() -> None:
    source = b"""class Calc {
  int add(int a) { helper(); return add(a, 0); }
  int add(int a, int b) { return a + b; }
  void helper() {}
}
"""
    facts = parse_source(source, _record("src/Calc.java", Language.JAVA))
    functions = {item.qualified_name: item for item in facts.functions}
    assert set(functions) == {
        "Calc::add(int)",
        "Calc::add(int,int)",
        "Calc::helper",
    }

    helper_call = _by_raw(facts, "helper()")
    assert helper_call.src == functions["Calc::add(int)"].id
    assert helper_call.resolved

    overload_call = _by_raw(facts, "add(a, 0)")
    assert overload_call.src == functions["Calc::add(int)"].id
    assert not overload_call.resolved
