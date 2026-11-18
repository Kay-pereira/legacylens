"""Scanner: traversal, language detection, filters, diagnostics and store hand-off."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from legacylens.ingestion import (
    DEFAULT_EXCLUDES,
    ScanError,
    ScannedFile,
    ScanOptions,
    SkippedFile,
    SkipReason,
    detect_language,
    is_probably_binary,
    iter_scan,
    scan_repository,
)
from legacylens.ir import FactStore, FileFacts, Language

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _write(root: Path, relative_path: str, content: bytes) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _tree(root: Path) -> ScanOptions:
    _write(root, "src/User.php", b"<?php class User {}")
    _write(root, "src/Model.java", b"class Model {}")
    _write(root, "src/legacy.inc", b"<?php echo 1;")
    _write(root, "src/notes.txt", b"not source")
    _write(root, "web/index.jsp", b"<html></html>")
    _write(root, "web/web.xml", b"<web-app/>")
    _write(root, "vendor/lib.php", b"<?php // vendored")
    _write(root, "big.php", b"<?php\n" + b"x" * 100)
    _write(root, "binary.php", b"<?php\x00binary")
    _write(root, "bad#name.php", b"<?php echo 1;")
    return ScanOptions(max_file_bytes=64)


@pytest.mark.parametrize(
    ("path", "head", "expected"),
    [
        ("src/User.php", b"", Language.PHP),
        ("src/APP.PHP", b"", Language.PHP),
        ("src/view.JSP", b"", Language.JSP),
        ("src/flow.tld", b"", Language.XML),
        ("WEB-INF/web.xml", b"", Language.XML),
        ("src/Service.java", b"", Language.JAVA),
        ("src/legacy.inc", b"<?php echo 1;", Language.PHP),
        ("src/legacy.inc", b"#!/usr/bin/env php\n", Language.PHP),
        ("src/legacy.inc", b"plain data", None),
        ("bin/report", b"#!/usr/bin/env php\n", Language.PHP),
        ("README.md", b"# docs", None),
    ],
)
def test_detect_language(path: str, head: bytes, expected: Language | None) -> None:
    assert detect_language(path, head) is expected


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"", False),
        (b"plain text\n", False),
        (b"a\tb\nc\r\n", False),
        (b"a\x00b", True),
        (b"\xff\xfeh\x00i\x00", True),
        (b"\x01" * 64, True),
    ],
)
def test_is_probably_binary(head: bytes, expected: bool) -> None:
    assert is_probably_binary(head) is expected


def test_scan_repository_accepts_supported_files_and_reports_rejections(
    tmp_path: Path,
) -> None:
    options = _tree(tmp_path)
    result = scan_repository(tmp_path, options)

    assert [item.path for item in result.files] == [
        "src/Model.java",
        "src/User.php",
        "src/legacy.inc",
        "web/index.jsp",
        "web/web.xml",
    ]
    assert [item.path for item in result.skipped] == [
        "bad#name.php",
        "big.php",
        "binary.php",
        "src/notes.txt",
    ]
    assert [item.reason for item in result.skipped] == [
        SkipReason.INVALID_PATH,
        SkipReason.TOO_LARGE,
        SkipReason.BINARY,
        SkipReason.UNSUPPORTED,
    ]
    assert result.errors == ()
    assert result.by_language == {
        Language.JAVA: 1,
        Language.PHP: 2,
        Language.JSP: 1,
        Language.XML: 1,
    }
    assert result.total_bytes == sum(item.record.size_bytes for item in result.files)


def test_include_patterns_silence_non_matching_files(tmp_path: Path) -> None:
    _tree(tmp_path)
    result = scan_repository(
        tmp_path,
        ScanOptions(include=("web/*",), max_file_bytes=64),
    )
    assert [item.path for item in result.files] == ["web/index.jsp", "web/web.xml"]
    assert result.skipped == ()


def test_exclude_patterns_prune_subtrees(tmp_path: Path) -> None:
    _tree(tmp_path)
    result = scan_repository(
        tmp_path,
        ScanOptions(exclude=(*DEFAULT_EXCLUDES, "web"), max_file_bytes=64),
    )
    assert [item.path for item in result.files] == [
        "src/Model.java",
        "src/User.php",
        "src/legacy.inc",
    ]


def test_scan_is_deterministic(tmp_path: Path) -> None:
    options = _tree(tmp_path)
    first = scan_repository(tmp_path, options)
    second = scan_repository(tmp_path, options)
    assert first == second


def test_iter_scan_yields_the_same_events_as_the_collected_result(tmp_path: Path) -> None:
    options = _tree(tmp_path)
    result = scan_repository(tmp_path, options)
    events = list(iter_scan(tmp_path, options))

    assert [item for item in events if isinstance(item, ScannedFile)] == list(result.files)
    assert [item for item in events if isinstance(item, SkippedFile)] == list(result.skipped)
    assert [item for item in events if isinstance(item, ScanError)] == list(result.errors)


def test_discovered_symlink_is_reported_and_not_accepted(tmp_path: Path) -> None:
    target = _write(tmp_path, "target.php", b"<?php echo 1;")
    link = tmp_path / "link.php"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks")

    result = scan_repository(tmp_path)
    assert [item.path for item in result.files] == ["target.php"]
    assert [(item.path, item.reason) for item in result.skipped] == [
        ("link.php", SkipReason.SYMLINK)
    ]


def test_scan_requires_a_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        scan_repository(tmp_path / "missing")


def test_scan_options_validate_limits() -> None:
    with pytest.raises(ValueError, match="max_file_bytes"):
        ScanOptions(max_file_bytes=-1)
    with pytest.raises(ValueError, match="extractor_version"):
        ScanOptions(extractor_version="")


def test_fixture_tree_language_counts() -> None:
    result = scan_repository(FIXTURES)
    assert result.by_language == {
        Language.JAVA: 8,
        Language.JSP: 5,
        Language.PHP: 7,
        Language.XML: 5,
    }
    assert result.skipped == ()
    assert result.errors == ()
    assert len(result.files) == 25


def test_scanned_record_can_be_written_to_the_fact_store(tmp_path: Path) -> None:
    scanned = scan_repository(FIXTURES).files[0]
    with FactStore(tmp_path / "facts.db") as store:
        store.replace_file(FileFacts(file=scanned.record))
        assert store.get_file(scanned.record.path) == scanned.record


def test_reason_counts_are_stable_for_benchmarks(tmp_path: Path) -> None:
    _tree(tmp_path)
    result = scan_repository(tmp_path, ScanOptions(max_file_bytes=64))
    reasons = Counter(item.reason for item in result.skipped)
    assert reasons[SkipReason.UNSUPPORTED] == 1
    assert reasons[SkipReason.BINARY] == 1
    assert reasons[SkipReason.TOO_LARGE] == 1
    assert reasons[SkipReason.INVALID_PATH] == 1
