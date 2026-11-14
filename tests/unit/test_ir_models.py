"""IR model invariants: keys, enums, validation and serialisation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from legacylens.ir import (
    ClaimRecord,
    ClassRecord,
    Confidence,
    EdgeKind,
    EdgeRecord,
    EntryKind,
    FileFacts,
    FileRecord,
    FunctionRecord,
    HttpMethod,
    Language,
    Parameter,
    Span,
    SqlOperation,
    class_id,
    entry_citation,
    function_id,
    weakest_confidence,
)


def test_span_rejects_inverted_lines() -> None:
    with pytest.raises(ValueError, match="end_line"):
        Span(start_line=9, end_line=3)


def test_span_line_count_is_inclusive() -> None:
    assert Span(start_line=4, end_line=6).line_count == 3


def test_file_path_rejects_key_separator() -> None:
    with pytest.raises(ValueError, match="reserved"):
        FileRecord(
            path="src/odd#name.php",
            language=Language.PHP,
            content_hash="abc",
            size_bytes=1,
        )


def test_function_qualified_name_rejects_key_separator() -> None:
    with pytest.raises(ValueError, match="reserved"):
        FunctionRecord(
            file_path="src/a.php",
            name="run",
            qualified_name="User#run",
            signature="run()",
            span=Span(1, 2),
        )


def test_weakest_confidence_orders_labels() -> None:
    assert (
        weakest_confidence(Confidence.VERIFIED, Confidence.UNSUPPORTED, Confidence.INFERRED)
        is Confidence.UNSUPPORTED
    )
    assert weakest_confidence(Confidence.VERIFIED) is Confidence.VERIFIED
    with pytest.raises(ValueError):
        weakest_confidence()


def test_stable_keys_and_citation_grammar() -> None:
    assert class_id("src/User.php", "App\\User") == "class:src/User.php#App\\User"
    assert function_id("src/User.php", "User::find") == "fn:src/User.php#User::find"
    assert entry_citation(EntryKind.ROUTE, HttpMethod.GET, "/users/{id}") == (
        "entry:route:GET:/users/{id}"
    )
    assert entry_citation(EntryKind.CLI, None, "bin/report.php") == ("entry:cli:-:bin/report.php")


def test_parameter_round_trips() -> None:
    parameter = Parameter(name="$id", type="int", default="0", variadic=False, by_reference=True)
    assert Parameter.from_dict(parameter.as_dict()) == parameter


def test_sql_operation_crud_mapping() -> None:
    assert SqlOperation.SELECT.crud == "R"
    assert SqlOperation.INSERT.crud == "C"
    assert SqlOperation.REPLACE.crud == "C"
    assert SqlOperation.UPDATE.crud == "U"
    assert SqlOperation.DELETE.crud == "D"
    assert SqlOperation.DDL.crud is None


def test_file_facts_reject_cross_file_children() -> None:
    file = FileRecord(
        path="src/User.php",
        language=Language.PHP,
        content_hash="abc",
        size_bytes=10,
    )
    edge = EdgeRecord(
        src=file.path,
        dst="src/db.php",
        kind=EdgeKind.REQUIRE,
        file_path="src/other.php",
        line=1,
    )
    with pytest.raises(ValueError, match="cannot be stored"):
        FileFacts(file=file, edges=(edge,))


def test_file_facts_reject_function_for_unknown_class() -> None:
    file = FileRecord(
        path="src/User.php",
        language=Language.PHP,
        content_hash="abc",
        size_bytes=10,
    )
    function = FunctionRecord(
        file_path=file.path,
        name="find",
        qualified_name="User::find",
        signature="find(int $id)",
        span=Span(1, 2),
        class_id=class_id(file.path, "Missing"),
    )
    with pytest.raises(ValueError, match="unknown class"):
        FileFacts(file=file, functions=(function,))


def test_file_facts_reject_duplicate_function_keys() -> None:
    file = FileRecord(
        path="src/a.php",
        language=Language.PHP,
        content_hash="abc",
        size_bytes=10,
    )
    function = FunctionRecord(
        file_path=file.path,
        name="run",
        qualified_name="run",
        signature="run()",
        span=Span(1, 2),
    )
    with pytest.raises(ValueError, match="duplicate function"):
        FileFacts(file=file, functions=(function, function))


def test_claim_normalises_naive_timestamps_and_accepts_strings() -> None:
    claim = ClaimRecord(
        subject="fn:src/User.php#User::find",
        kind="summary",  # type: ignore[arg-type]
        text="Finds a user.",
        status="inferred",  # type: ignore[arg-type]
        created_at=datetime(2026, 1, 2, 3, 4, 5),
    )
    assert claim.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_function_record_loc_is_derived_from_span() -> None:
    class_record = ClassRecord(
        file_path="src/User.php",
        name="User",
        qualified_name="User",
        span=Span(3, 20),
    )
    function = FunctionRecord(
        file_path="src/User.php",
        name="find",
        qualified_name="User::find",
        signature="find(int $id)",
        span=Span(5, 12),
        class_id=class_record.id,
    )
    facts = FileFacts(
        file=FileRecord(
            path="src/User.php",
            language=Language.PHP,
            content_hash="abc",
            size_bytes=10,
        ),
        classes=(class_record,),
        functions=(function,),
    )
    assert facts.functions[0].loc == 8
