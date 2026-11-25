"""SQLite fact store: atomic replacement, typed reads, claims and cache."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from legacylenscli.ir import (
    SCHEMA_VERSION,
    CachedCompletion,
    ClaimKind,
    ClaimOrigin,
    ClaimRecord,
    ClassRecord,
    Confidence,
    DataAccessRecord,
    EdgeKind,
    EdgeRecord,
    EntryKind,
    EntryPointRecord,
    FactStore,
    FileFacts,
    FileRecord,
    FunctionRecord,
    HttpMethod,
    Language,
    Parameter,
    SchemaVersionError,
    SideEffectKind,
    SideEffectRecord,
    Span,
    SqlOperation,
    StoreSummary,
    Visibility,
)


@pytest.fixture
def store(tmp_path: Path) -> FactStore:
    with FactStore(tmp_path / "legacylens.db") as opened:
        yield opened


def _sample_facts() -> FileFacts:
    path = "src/User.php"
    user = ClassRecord(
        file_path=path,
        name="User",
        qualified_name="User",
        span=Span(3, 20),
        implements=("JsonSerializable",),
        modifiers=("final",),
        annotations=("Entity",),
    )
    find = FunctionRecord(
        file_path=path,
        name="find",
        qualified_name="User::find",
        signature="public static function find(int $id): ?array",
        span=Span(5, 12),
        class_id=user.id,
        parameters=(Parameter(name="$id", type="int"),),
        return_type="?array",
        visibility=Visibility.PUBLIC,
        is_static=True,
        cyclomatic_complexity=2,
        max_nesting=1,
        modifiers=("static",),
        annotations=("Route",),
    )
    promote = FunctionRecord(
        file_path=path,
        name="promote",
        qualified_name="User::promote",
        signature="public static function promote(int $id): void",
        span=Span(14, 19),
        class_id=user.id,
        parameters=(Parameter(name="$id", type="int"),),
        return_type="void",
        visibility=Visibility.PUBLIC,
        is_static=True,
    )
    return FileFacts(
        file=FileRecord(
            path=path,
            language=Language.PHP,
            content_hash="sha256:abc",
            size_bytes=512,
            loc=20,
            package="App\\Domain",
            module="users",
        ),
        classes=(user,),
        functions=(find, promote),
        edges=(
            EdgeRecord(
                src=path,
                dst="src/db.php",
                kind=EdgeKind.REQUIRE,
                file_path=path,
                line=2,
            ),
            EdgeRecord(
                src=find.id,
                dst=promote.id,
                kind=EdgeKind.CALL,
                file_path=path,
                line=10,
                confidence=Confidence.INFERRED,
            ),
            EdgeRecord(
                src=find.id,
                dst="\\Legacy\\Db::connect",
                kind=EdgeKind.CALL,
                file_path=path,
                line=11,
                resolved=False,
                confidence=Confidence.INFERRED,
                raw="\\Legacy\\Db::connect($id)",
            ),
        ),
        data_access=(
            DataAccessRecord(
                file_path=path,
                function_id=find.id,
                operation=SqlOperation.SELECT,
                table_name="users",
                sql="SELECT id, email FROM users WHERE id = :id",
                line=8,
                driver="PDO",
                parameters=(":id",),
            ),
            DataAccessRecord(
                file_path=path,
                function_id=promote.id,
                operation=SqlOperation.UPDATE,
                table_name="users",
                sql="UPDATE users SET role = :role WHERE id = :id",
                line=17,
                driver="PDO",
                parameters=(":role", ":id"),
                confidence=Confidence.INFERRED,
            ),
        ),
        entry_points=(
            EntryPointRecord(
                file_path=path,
                kind=EntryKind.ROUTE,
                path="/users/{id}",
                line=5,
                function_id=find.id,
                http_method=HttpMethod.GET,
                framework="legacy-router",
                requires_auth=False,
                auth_hint="no auth middleware observed",
            ),
        ),
        side_effects=(
            SideEffectRecord(
                file_path=path,
                function_id=find.id,
                kind=SideEffectKind.DATABASE,
                detail="reads the users table",
                sink="PDO::prepare",
                line=8,
            ),
        ),
    )


def test_replace_file_round_trips_every_entity(store: FactStore) -> None:
    facts = _sample_facts()
    store.replace_file(facts)

    assert store.get_file(facts.file.path) == facts.file
    assert store.list_classes(file_path=facts.file.path) == list(facts.classes)
    assert store.list_functions(file_path=facts.file.path) == list(facts.functions)
    assert store.list_edges(file_path=facts.file.path) == list(facts.edges)
    assert store.list_data_access(file_path=facts.file.path) == list(facts.data_access)
    assert store.list_entry_points(file_path=facts.file.path) == list(facts.entry_points)
    assert store.list_side_effects(file_path=facts.file.path) == list(facts.side_effects)

    assert store.find_class("User", file_path=facts.file.path) == facts.classes[0]
    assert store.find_function("User::find", file_path=facts.file.path) == facts.functions[0]
    assert store.get_function(facts.functions[0].id) == facts.functions[0]
    assert store.get_class(facts.classes[0].id) == facts.classes[0]


def test_replacing_a_file_removes_stale_facts(store: FactStore) -> None:
    facts = _sample_facts()
    store.replace_file(facts)

    trimmed = FileFacts(
        file=facts.file,
        classes=(facts.classes[0],),
        functions=(facts.functions[0],),
        edges=(facts.edges[0],),
    )
    store.replace_file(trimmed)

    assert store.list_functions(file_path=facts.file.path) == [facts.functions[0]]
    assert store.list_edges(file_path=facts.file.path) == [facts.edges[0]]
    assert store.list_data_access(file_path=facts.file.path) == []
    assert store.list_entry_points(file_path=facts.file.path) == []
    assert store.list_side_effects(file_path=facts.file.path) == []
    assert store.summary() == StoreSummary(files=1, classes=1, functions=1, edges=1)


def test_delete_file_cascades_children(store: FactStore) -> None:
    facts = _sample_facts()
    store.replace_file(facts)

    assert store.delete_file(facts.file.path) is True
    assert store.delete_file(facts.file.path) is False
    assert store.summary() == StoreSummary()


def test_unresolved_edges_are_kept_verbatim(store: FactStore) -> None:
    facts = _sample_facts()
    store.replace_file(facts)

    unresolved = store.list_edges(resolved=False)
    assert len(unresolved) == 1
    assert unresolved[0].dst == "\\Legacy\\Db::connect"
    assert unresolved[0].raw == "\\Legacy\\Db::connect($id)"
    assert store.summary().unresolved_edges == 1


def test_confidence_counts_are_zero_filled(store: FactStore) -> None:
    facts = _sample_facts()
    store.replace_file(facts)

    counts = store.confidence_counts()
    assert counts["edges"][Confidence.VERIFIED] == 1
    assert counts["edges"][Confidence.INFERRED] == 2
    assert counts["data_access"][Confidence.VERIFIED] == 1
    assert counts["data_access"][Confidence.INFERRED] == 1
    assert counts["claims"][Confidence.UNSUPPORTED] == 0


def test_file_filters_skip_generated_and_vendored_sources(store: FactStore) -> None:
    for path, generated, vendored in (
        ("src/User.php", False, False),
        ("src/generated/Model.php", True, False),
        ("vendor/lib.php", False, True),
    ):
        store.replace_file(
            FileFacts(
                file=FileRecord(
                    path=path,
                    language=Language.PHP,
                    content_hash=path,
                    size_bytes=1,
                    is_generated=generated,
                    is_vendored=vendored,
                )
            )
        )

    assert [item.path for item in store.list_files()] == [
        "src/User.php",
        "src/generated/Model.php",
        "vendor/lib.php",
    ]
    assert [item.path for item in store.list_files(generated=False)] == [
        "src/User.php",
        "vendor/lib.php",
    ]
    assert [item.path for item in store.list_files(vendored=False, generated=False)] == [
        "src/User.php"
    ]


def test_claims_are_filtered_and_replaced(store: FactStore) -> None:
    first = ClaimRecord(
        subject="fn:src/User.php#User::find",
        kind=ClaimKind.SUMMARY,
        text="Returns a user by id.",
        status=Confidence.INFERRED,
        citations=("src/User.php:5", "fn:src/User.php#User::find"),
        origin=ClaimOrigin.LLM,
        model="gpt-4o-mini",
        prompt_hash="prompt-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    other = ClaimRecord(
        subject="table:users",
        kind=ClaimKind.TABLE,
        text="Stores application users.",
        status=Confidence.VERIFIED,
        citations=("table:users",),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    first_id = store.add_claim(first)
    second_id = store.add_claims([other])[0]

    assert store.get_claim(first_id) == replace(first, id=first_id)
    assert len(store.list_claims()) == 2
    assert store.list_claims(subject=first.subject) == [replace(first, id=first_id)]
    assert store.list_claims(kind=ClaimKind.TABLE) == [replace(other, id=second_id)]
    assert len(store.list_claims(status=Confidence.INFERRED)) == 1
    assert len(store.list_claims(origin=ClaimOrigin.STATIC)) == 1

    replacement = ClaimRecord(
        subject=first.subject,
        kind=ClaimKind.SUMMARY,
        text="Looks up a user row.",
        status=Confidence.VERIFIED,
        citations=("src/User.php:5",),
    )
    assert store.replace_claims([replacement], subject=first.subject) == 1
    claims = store.list_claims(subject=first.subject)
    assert len(claims) == 1
    assert claims[0].text == "Looks up a user row."
    assert store.clear_claims(origin=ClaimOrigin.STATIC) == 2
    assert store.summary().claims == 0


def test_claims_require_matching_subject_on_replace(store: FactStore) -> None:
    claim = ClaimRecord(
        subject="table:users",
        kind=ClaimKind.TABLE,
        text="Stores users.",
    )
    with pytest.raises(ValueError, match="match the given subject"):
        store.replace_claims([claim], subject="table:orders")


def test_cache_round_trip_tracks_hits_and_usage(store: FactStore) -> None:
    entry = CachedCompletion(
        cache_key="sha256:key",
        response="The function returns a user.",
        provider="openai",
        model="gpt-4o-mini",
        prompt_hash="prompt-1",
        prompt_tokens=100,
        completion_tokens=20,
        cost_usd=0.0001,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_used_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    store.put_cached_completion(entry)

    first = store.get_cached_completion("sha256:key")
    assert first is not None
    assert first.response == entry.response
    assert first.hits == 1

    second = store.get_cached_completion("sha256:key")
    assert second is not None and second.hits == 2

    untouched = store.get_cached_completion("sha256:key", touch=False)
    assert untouched is not None and untouched.hits == 2

    stats = store.cache_stats()
    assert stats.entries == 1
    assert stats.hits == 2
    assert stats.prompt_tokens == 100
    assert stats.completion_tokens == 20
    assert stats.cost_usd == pytest.approx(0.0001)

    store.put_cached_completion(entry)
    refreshed = store.get_cached_completion("sha256:key", touch=False)
    assert refreshed is not None and refreshed.hits == 2

    assert store.delete_cached_completion("sha256:key") is True
    assert store.cache_stats().entries == 0


def test_schema_version_is_recorded_and_enforced(tmp_path: Path) -> None:
    path = tmp_path / "versioned.db"
    with FactStore(path) as opened:
        assert opened.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(SchemaVersionError, match="schema version 999"):
        FactStore(path)


def test_store_rejects_writes_after_close(tmp_path: Path) -> None:
    store = FactStore(tmp_path / "closed.db")
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.summary()
