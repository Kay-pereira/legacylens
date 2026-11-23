"""Claim verification: citation grammar, IR/source resolution and label policy."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from legacylens.ir import (
    ClaimKind,
    ClaimOrigin,
    ClaimRecord,
    ClassRecord,
    Confidence,
    DataAccessRecord,
    EntryKind,
    EntryPointRecord,
    FactStore,
    FileFacts,
    FileRecord,
    FunctionRecord,
    HttpMethod,
    Language,
    Span,
    SqlOperation,
)
from legacylens.verification import (
    CitationError,
    CitationKind,
    parse_citation,
    verify_claim,
    verify_claims,
)

USER = "src/User.php"
FUNCTION_KEY = "fn:src/User.php#User::find"
CLASS_KEY = "class:src/User.php#User"


def _user_facts() -> FileFacts:
    user = ClassRecord(
        file_path=USER,
        name="User",
        qualified_name="User",
        span=Span(3, 20),
    )
    find = FunctionRecord(
        file_path=USER,
        name="find",
        qualified_name="User::find",
        signature="public static function find(int $id): ?array",
        span=Span(5, 12),
        class_id=user.id,
    )
    return FileFacts(
        file=FileRecord(
            path=USER,
            language=Language.PHP,
            content_hash="sha256:user",
            size_bytes=512,
            loc=20,
        ),
        classes=(user,),
        functions=(find,),
        data_access=(
            DataAccessRecord(
                file_path=USER,
                function_id=find.id,
                operation=SqlOperation.SELECT,
                table_name="users",
                sql="SELECT id FROM users WHERE id = :id",
                line=6,
                driver="PDO",
                confidence=Confidence.VERIFIED,
            ),
            DataAccessRecord(
                file_path=USER,
                function_id=find.id,
                operation=SqlOperation.INSERT,
                table_name="users",
                sql="INSERT INTO users (email) VALUES (:email)",
                line=7,
                driver="PDO",
                confidence=Confidence.INFERRED,
            ),
            DataAccessRecord(
                file_path=USER,
                function_id=find.id,
                operation=SqlOperation.SELECT,
                table_name="audit",
                sql="SELECT * FROM audit",
                line=9,
                confidence=Confidence.INFERRED,
            ),
            DataAccessRecord(
                file_path=USER,
                function_id=find.id,
                operation=SqlOperation.SELECT,
                table_name="ghost",
                sql="SELECT * FROM ghost",
                line=10,
                confidence=Confidence.UNSUPPORTED,
            ),
        ),
        entry_points=(
            EntryPointRecord(
                file_path=USER,
                kind=EntryKind.ROUTE,
                path="/users/{id}",
                line=5,
                function_id=find.id,
                http_method=HttpMethod.GET,
                framework="legacy-router",
                requires_auth=True,
                confidence=Confidence.VERIFIED,
            ),
            EntryPointRecord(
                file_path=USER,
                kind=EntryKind.CLI,
                path="bin/report.php",
                line=12,
                confidence=Confidence.INFERRED,
            ),
        ),
    )


@pytest.fixture
def store() -> Iterator[FactStore]:
    with FactStore() as opened:
        opened.replace_file(_user_facts())
        yield opened


def _claim(
    citations: tuple[str, ...],
    *,
    subject: str = FUNCTION_KEY,
    status: Confidence = Confidence.INFERRED,
    kind: ClaimKind = ClaimKind.SUMMARY,
    text: str = "Finds a user by id.",
) -> ClaimRecord:
    return ClaimRecord(
        subject=subject,
        kind=kind,
        text=text,
        status=status,
        citations=citations,
        origin=ClaimOrigin.LLM,
        model="test-model",
        prompt_hash="prompt-1",
    )


# ---------------------------------------------------------------------------
# Citation parsing
# ---------------------------------------------------------------------------


def test_parse_file_line_and_file_keys() -> None:
    line = parse_citation("src/User.php:14")
    bare = parse_citation("src/User.php")
    colon_path = parse_citation("weird:name.php:14")

    assert line.kind is CitationKind.FILE_LINE
    assert line.path == "src/User.php"
    assert line.line == 14
    assert bare.kind is CitationKind.FILE
    assert bare.path == "src/User.php"
    assert colon_path.kind is CitationKind.FILE_LINE
    assert colon_path.path == "weird:name.php"
    assert colon_path.line == 14


def test_parse_symbol_table_and_entry_citations() -> None:
    function = parse_citation(FUNCTION_KEY)
    class_key = parse_citation(CLASS_KEY)
    table = parse_citation("table:app.users")
    route = parse_citation("entry:route:GET:/users/{id}")
    cli = parse_citation("entry:cli:-:bin/report.php")
    colon_entry = parse_citation("entry:route:GET:/users/export:csv")

    assert function.kind is CitationKind.FUNCTION
    assert function.path == USER
    assert function.qualified_name == "User::find"
    assert class_key.kind is CitationKind.CLASS
    assert table.kind is CitationKind.TABLE
    assert table.table_name == "app.users"
    assert route.kind is CitationKind.ENTRY
    assert route.entry_kind is EntryKind.ROUTE
    assert route.http_method is HttpMethod.GET
    assert route.entry_path == "/users/{id}"
    assert cli.entry_kind is EntryKind.CLI
    assert cli.http_method is None
    assert colon_entry.entry_path == "/users/export:csv"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "src/User.php:0",
        "fn:src/User.php",
        "fn:#User::find",
        "fn:src/User.php#",
        "fn:src/User.php#User#find",
        "class:src/User.php",
        "table:",
        "entry:route:GET",
        "entry:route:GET:",
        "entry:nope:GET:/x",
        "entry:route:get:/x",
        "src/User.php:1\nsrc/Other.php:2",
        "src/User#.php",
    ],
)
def test_parse_rejects_malformed_citations(text: str) -> None:
    with pytest.raises(CitationError):
        parse_citation(text)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_file_line_inside_and_outside_the_ir(store: FactStore) -> None:
    inside = verify_claim(store, _claim(("src/User.php:20",)))
    outside = verify_claim(store, _claim(("src/User.php:21",)))
    missing = verify_claim(store, _claim(("src/Missing.php:1",)))

    assert inside.status is Confidence.VERIFIED
    assert inside.citations[0].resolved
    assert inside.citations[0].source_checked is False
    assert outside.status is Confidence.UNSUPPORTED
    assert "beyond" in outside.citations[0].reason
    assert missing.status is Confidence.UNSUPPORTED
    assert "not in the IR" in missing.citations[0].reason


def test_bare_file_function_and_class_keys(store: FactStore) -> None:
    for citation in ("src/User.php", FUNCTION_KEY, CLASS_KEY):
        result = verify_claim(store, _claim((citation,)))
        assert result.status is Confidence.VERIFIED, citation

    for citation in (
        "src/Missing.php",
        "fn:src/User.php#User::missing",
        "class:src/User.php#Missing",
    ):
        result = verify_claim(store, _claim((citation,)))
        assert result.status is Confidence.UNSUPPORTED, citation


def test_table_citations_inherit_the_strongest_row_label(store: FactStore) -> None:
    verified = verify_claim(store, _claim(("table:users",), subject="table:users"))
    inferred = verify_claim(store, _claim(("table:audit",), subject="table:audit"))
    unsupported = verify_claim(store, _claim(("table:ghost",), subject="table:ghost"))
    missing = verify_claim(store, _claim(("table:missing",), subject="table:missing"))

    assert verified.status is Confidence.VERIFIED
    assert inferred.status is Confidence.INFERRED
    assert unsupported.status is Confidence.UNSUPPORTED
    assert missing.status is Confidence.UNSUPPORTED


def test_entry_citations_match_kind_method_and_path(store: FactStore) -> None:
    route = verify_claim(store, _claim(("entry:route:GET:/users/{id}",)))
    cli = verify_claim(store, _claim(("entry:cli:-:bin/report.php",)))
    wrong_method = verify_claim(store, _claim(("entry:route:DELETE:/users/{id}",)))
    wrong_path = verify_claim(store, _claim(("entry:route:GET:/missing",)))

    assert route.status is Confidence.VERIFIED
    assert cli.status is Confidence.INFERRED
    assert wrong_method.status is Confidence.UNSUPPORTED
    assert wrong_path.status is Confidence.UNSUPPORTED


# ---------------------------------------------------------------------------
# Claim label policy
# ---------------------------------------------------------------------------


def test_all_verified_evidence_promotes_an_llm_claim(store: FactStore) -> None:
    claim = _claim(
        (
            "src/User.php:5",
            "src/User.php",
            FUNCTION_KEY,
            CLASS_KEY,
            "table:users",
            "entry:route:GET:/users/{id}",
        )
    )

    result = verify_claim(store, claim)

    assert result.previous_status is Confidence.INFERRED
    assert result.status is Confidence.VERIFIED
    assert result.changed
    assert result.publishable


def test_inferred_evidence_keeps_a_claim_inferred(store: FactStore) -> None:
    result = verify_claim(store, _claim(("src/User.php:5", "table:audit")))

    assert result.status is Confidence.INFERRED


def test_partial_resolution_is_inferred(store: FactStore) -> None:
    result = verify_claim(store, _claim(("src/User.php:5", "src/User.php:99")))

    assert result.status is Confidence.INFERRED
    assert any("do not resolve" in reason for reason in result.reasons)


def test_unresolved_and_malformed_evidence_is_unsupported(store: FactStore) -> None:
    no_citations = verify_claim(store, _claim(()))
    unresolved = verify_claim(store, _claim(("src/User.php:99",)))
    malformed = verify_claim(store, _claim(("fn:src/User.php",)))

    assert no_citations.status is Confidence.UNSUPPORTED
    assert unresolved.status is Confidence.UNSUPPORTED
    assert malformed.status is Confidence.UNSUPPORTED
    assert any("malformed" in reason for reason in malformed.reasons)


def test_unresolved_subject_makes_the_claim_unsupported(store: FactStore) -> None:
    result = verify_claim(
        store,
        _claim(("src/User.php:5",), subject="fn:src/User.php#User::missing"),
    )

    assert result.status is Confidence.UNSUPPORTED
    assert any("subject" in reason for reason in result.reasons)


def test_project_subject_is_virtual(store: FactStore) -> None:
    result = verify_claim(store, _claim(("src/User.php:5",), subject="project"))

    assert result.status is Confidence.VERIFIED
    assert result.subject_resolution is None


# ---------------------------------------------------------------------------
# Source checks
# ---------------------------------------------------------------------------


def test_source_root_confirms_the_physical_line(store: FactStore, tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "src" / "User.php"
    source.parent.mkdir(parents=True)
    source.write_text("<?php\n\nclass User\n{}\n", encoding="utf-8")

    inside = verify_claim(store, _claim(("src/User.php:4",)), source_root=root)
    outside = verify_claim(store, _claim(("src/User.php:5",)), source_root=root)

    assert inside.status is Confidence.VERIFIED
    assert inside.citations[0].source_checked is True
    assert outside.status is Confidence.UNSUPPORTED
    assert "source" in outside.citations[0].reason


def test_source_root_requires_the_file_and_stays_inside_the_root(
    store: FactStore, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    missing = verify_claim(store, _claim(("src/User.php:1",)), source_root=root)

    with FactStore() as escaped_store:
        escaped_store.replace_file(
            FileFacts(
                file=FileRecord(
                    path="../escape.php",
                    language=Language.PHP,
                    content_hash="sha256:escape",
                    size_bytes=10,
                    loc=1,
                )
            )
        )
        (tmp_path / "escape.php").write_text("<?php\n", encoding="utf-8")
        escaped = verify_claim(
            escaped_store,
            _claim(("../escape.php:1",), subject="../escape.php"),
            source_root=root,
        )

    assert missing.status is Confidence.UNSUPPORTED
    assert "missing" in missing.citations[0].reason
    assert escaped.status is Confidence.UNSUPPORTED
    assert "escapes" in escaped.citations[0].reason


def test_source_root_must_be_a_directory(store: FactStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source_root"):
        verify_claim(store, _claim(("src/User.php:1",)), source_root=tmp_path / "missing")


# ---------------------------------------------------------------------------
# Report and persistence
# ---------------------------------------------------------------------------


def test_report_counts_rates_and_json_output(store: FactStore) -> None:
    store.add_claims(
        [
            _claim(("src/User.php:5",), text="Verified claim."),
            _claim(("table:audit",), text="Inferred claim."),
            _claim(("src/User.php:99",), text="Unsupported claim."),
        ]
    )

    report = verify_claims(store, persist=False)

    assert report.total == 3
    assert report.counts == {
        Confidence.VERIFIED: 1,
        Confidence.INFERRED: 1,
        Confidence.UNSUPPORTED: 1,
    }
    assert report.resolved == 2
    assert report.verification_rate == pytest.approx(2 / 3)
    assert report.verified_rate == pytest.approx(1 / 3)
    assert len(report.publishable) == 2
    payload = report.as_dict()
    assert payload["counts"] == {"verified": 1, "inferred": 1, "unsupported": 1}
    json.dumps(payload)


def test_verify_claims_persists_only_changed_subjects(store: FactStore) -> None:
    changed_subject = FUNCTION_KEY
    stable_subject = "table:audit"
    store.add_claims(
        [
            _claim(("src/User.php:5",), subject=changed_subject, text="Promoted."),
            _claim(("table:audit",), subject=changed_subject, text="Stays inferred."),
            _claim(("table:audit",), subject=stable_subject, text="Untouched."),
        ]
    )
    stable_before = store.list_claims(subject=stable_subject)[0]

    report = verify_claims(store)

    assert report.persisted_subjects == (changed_subject,)
    assert [item.status for item in store.list_claims(subject=changed_subject)] == [
        Confidence.VERIFIED,
        Confidence.INFERRED,
    ]
    stable_after = store.list_claims(subject=stable_subject)[0]
    assert stable_after.id == stable_before.id
    assert stable_after.status is Confidence.INFERRED
    assert len(store.list_claims()) == 3

    again = verify_claims(store)
    assert again.persisted_subjects == ()


def test_verify_claims_without_persist_leaves_the_store_unchanged(store: FactStore) -> None:
    store.add_claim(_claim(("src/User.php:5",)))
    before = store.list_claims()

    verify_claims(store, persist=False)

    assert store.list_claims() == before
