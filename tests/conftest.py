"""Shared fixtures for the Q&A tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from legacylens.ir import (
    ClaimKind,
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
    RequestInputKind,
    RequestInputRecord,
    SideEffectKind,
    SideEffectRecord,
    Span,
    SqlOperation,
)

USER = "src/User.php"
REPORT = "bin/report.php"


def _user_facts() -> FileFacts:
    user = ClassRecord(
        file_path=USER,
        name="User",
        qualified_name="User",
        span=Span(3, 30),
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
            content_hash="sha256:qa-user",
            size_bytes=900,
            loc=40,
        ),
        classes=(user,),
        functions=(find,),
        data_access=(
            DataAccessRecord(
                file_path=USER,
                function_id=find.id,
                operation=SqlOperation.SELECT,
                table_name="users",
                sql="SELECT id, email FROM users WHERE id = :id",
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
                auth_hint="session guard",
            ),
        ),
        request_inputs=(
            RequestInputRecord(
                file_path=USER,
                kind=RequestInputKind.QUERY,
                name="id",
                line=5,
                function_id=find.id,
                framework="legacy-router",
            ),
        ),
        side_effects=(
            SideEffectRecord(
                file_path=USER,
                kind=SideEffectKind.FILESYSTEM,
                detail="writes report.csv",
                line=11,
                function_id=find.id,
            ),
        ),
    )


def _report_facts() -> FileFacts:
    report = FunctionRecord(
        file_path=REPORT,
        name="report",
        qualified_name="report",
        signature="function report(): void",
        span=Span(2, 10),
    )
    return FileFacts(
        file=FileRecord(
            path=REPORT,
            language=Language.PHP,
            content_hash="sha256:qa-report",
            size_bytes=300,
            loc=15,
        ),
        functions=(report,),
        data_access=(
            DataAccessRecord(
                file_path=REPORT,
                function_id=report.id,
                operation=SqlOperation.SELECT,
                table_name="audit",
                sql="SELECT * FROM audit",
                line=4,
                driver="PDO",
                confidence=Confidence.INFERRED,
            ),
        ),
        entry_points=(
            EntryPointRecord(
                file_path=REPORT,
                kind=EntryKind.CLI,
                path=REPORT,
                line=2,
                function_id=report.id,
            ),
        ),
        side_effects=(
            SideEffectRecord(
                file_path=REPORT,
                kind=SideEffectKind.EMAIL,
                detail="sends nightly digest",
                line=6,
                function_id=report.id,
            ),
        ),
    )


@pytest.fixture
def qa_store() -> Iterator[FactStore]:
    """A small, fully populated store used by retrieval, answer and server tests."""

    with FactStore(check_same_thread=False) as store:
        store.replace_file(_user_facts())
        store.replace_file(_report_facts())
        store.add_claims(
            [
                ClaimRecord(
                    subject="table:users",
                    kind=ClaimKind.TABLE,
                    text="The users table is read and written by User::find.",
                    status=Confidence.VERIFIED,
                    citations=("table:users", USER),
                ),
                ClaimRecord(
                    subject="fn:bin/report.php#report",
                    kind=ClaimKind.BEHAVIOUR,
                    text="The report script reads the audit table and sends a digest.",
                    status=Confidence.INFERRED,
                    citations=("fn:bin/report.php#report", "table:audit"),
                ),
                ClaimRecord(
                    subject="table:ghost",
                    kind=ClaimKind.TABLE,
                    text="This unsupported claim must not enter the index.",
                    status=Confidence.UNSUPPORTED,
                    citations=("table:ghost",),
                ),
            ]
        )
        yield store
