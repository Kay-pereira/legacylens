"""Dead-code and business-rule heuristics over the IR and PHP/Java source."""

from __future__ import annotations

import json
from pathlib import Path

from legacylens.analysis import (
    RuleKind,
    RuleSource,
    build_business_rules,
    build_dead_code,
)
from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import (
    ClassRecord,
    EntryKind,
    EntryPointRecord,
    FactStore,
    FileFacts,
    FileRecord,
    FunctionRecord,
    Language,
    Span,
    Visibility,
)
from legacylens.parsers import parse_source


def _store_from(root: Path, db: Path) -> FactStore:
    scan = scan_repository(root, ScanOptions())
    store = FactStore(db)
    for scanned in scan.files:
        store.replace_file(parse_source(scanned.absolute_path.read_bytes(), scanned.record))
    return store


def _write_tree(root: Path) -> None:
    (root / "inc").mkdir(parents=True)
    (root / "index.php").write_text(
        """<?php
require_once 'inc/helpers.php';

echo live_helper('value');
""",
        encoding="utf-8",
    )
    (root / "inc" / "helpers.php").write_text(
        """<?php

function live_helper(string $value): string
{
    return used_helper($value);
}

function used_helper(string $value): string
{
    return $value;
}

function dead_helper(): string
{
    return 'dead';
}

class Legacy
{
    public function public_api(): string
    {
        return 'api';
    }

    private function orphan(): string
    {
        return 'orphan';
    }
}
""",
        encoding="utf-8",
    )


def test_dead_code_follows_entry_point_calls_and_excludes_public_api(tmp_path: Path) -> None:
    _write_tree(tmp_path)

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_dead_code(store)

    assert report.entry_points == 1
    assert report.functions == 5
    assert report.reachable_functions == 2
    assert [item.name for item in report.candidates] == ["dead_helper", "orphan"]
    assert {item.function_id for item in report.candidates} == {
        "fn:inc/helpers.php#dead_helper",
        "fn:inc/helpers.php#Legacy::orphan",
    }
    assert dict(report.excluded) == {"public_api": 1}
    assert all(item.confidence.value == "inferred" for item in report.candidates)


def test_dead_code_is_skipped_when_no_entry_point_exists(tmp_path: Path) -> None:
    (tmp_path / "lib.php").write_text(
        "<?php\nfunction helper(): void {}\n",
        encoding="utf-8",
    )

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_dead_code(store)

    assert report.candidates == ()
    assert report.skipped_reason == "no entry points detected"


def test_unresolved_callers_suppress_dead_code_candidates(tmp_path: Path) -> None:
    (tmp_path / "index.php").write_text(
        "<?php\n$receiver->dead_helper();\n",
        encoding="utf-8",
    )
    (tmp_path / "helpers.php").write_text(
        "<?php\nfunction dead_helper(): void {}\n",
        encoding="utf-8",
    )

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_dead_code(store)

    assert report.candidates == ()
    assert dict(report.excluded) == {"unresolved_caller": 1}


def test_business_rules_come_from_conditions_and_calls(tmp_path: Path) -> None:
    (tmp_path / "index.php").write_text(
        """<?php
$user = $_GET['user'] ?? '';
if (!is_admin($user)) {
    echo 'denied';
}
$amount = (int) ($_GET['amount'] ?? 0);
if ($amount > 1000) {
    echo 'large';
}
$email = $_GET['email'] ?? '';
if ($email !== null && !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    echo 'bad email';
}
validate($email);
check_permission('editor');
""",
        encoding="utf-8",
    )

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        report = build_business_rules(store, source_root=tmp_path)

    by_key = {(item.kind, item.source, item.evidence): item for item in report.candidates}
    assert (RuleKind.AUTHORISATION, RuleSource.CONDITION, "!is_admin($user)") in by_key
    assert (RuleKind.THRESHOLD, RuleSource.CONDITION, "$amount > 1000") in by_key
    assert (RuleKind.VALIDATION, RuleSource.CALL, "validate($email)") in by_key
    assert (RuleKind.AUTHORISATION, RuleSource.CALL, "check_permission('editor')") in by_key
    assert any(
        item.kind is RuleKind.VALIDATION and "filter_var" in item.evidence
        for item in report.candidates
    )
    assert all(item.confidence.value == "inferred" for item in report.candidates)
    assert all(item.citations for item in report.candidates)

    data = report.as_dict()
    assert data["business_rules"] == len(report.candidates)
    assert data["counts"] == {"authorisation": 2, "threshold": 1, "validation": 2}
    assert json.loads(json.dumps(data)) == data


def test_business_rules_use_ir_annotations_and_entry_auth(tmp_path: Path) -> None:
    file = FileRecord(
        path="src/Pay.php",
        language=Language.PHP,
        content_hash="sha256:test",
        size_bytes=120,
        loc=12,
    )
    class_record = ClassRecord(
        file_path=file.path,
        name="Pay",
        qualified_name="Pay",
        span=Span(1, 12),
        annotations=("@PreAuthorize(\"hasRole('admin')\")",),
    )
    function = FunctionRecord(
        file_path=file.path,
        name="refund",
        qualified_name="Pay::refund",
        signature="function refund(string $account)",
        span=Span(2, 12),
        class_id=class_record.id,
        annotations=("@NotNull",),
        visibility=Visibility.PRIVATE,
    )
    entry = EntryPointRecord(
        file_path=file.path,
        kind=EntryKind.ROUTE,
        path="/pay",
        line=2,
        function_id=function.id,
        requires_auth=True,
        auth_hint="session guard",
    )

    with FactStore(tmp_path / "facts.db") as store:
        store.replace_file(
            FileFacts(
                file=file,
                classes=(class_record,),
                functions=(function,),
                entry_points=(entry,),
            )
        )
        report = build_business_rules(store)

    assert [
        (item.kind, item.source, item.summary, item.confidence.value)
        for item in report.candidates
    ] == [
        (RuleKind.AUTHORISATION, RuleSource.ANNOTATION, "Authorisation annotation: @PreAuthorize", "inferred"),
        (RuleKind.AUTHORISATION, RuleSource.ENTRY_POINT, "Entry point requires authentication (/pay)", "verified"),
        (RuleKind.VALIDATION, RuleSource.ANNOTATION, "Validation annotation: @NotNull", "inferred"),
    ]
    assert report.files_scanned == 0


def test_missing_source_files_are_reported(tmp_path: Path) -> None:
    (tmp_path / "index.php").write_text("<?php\necho 1;\n", encoding="utf-8")

    with _store_from(tmp_path, tmp_path / "facts.db") as store:
        (tmp_path / "index.php").unlink()
        report = build_business_rules(store, source_root=tmp_path)

    assert report.candidates == ()
    assert report.files_scanned == 0
    assert report.files_missing == 1
