"""Repository walking, language detection and file filtering.

The scanner is deterministic and streaming-first:

* ``iter_scan`` yields one event per file or recoverable error and retains no repository
  state beyond the current directory and file.
* ``scan_repository`` collects the same events into a :class:`ScanResult` for tests, CLI
  summaries and reports.

Only files that pass the size, binary and language checks are hashed. Excluded and
include-filtered paths are skipped silently; files that were considered but rejected are
reported with a :class:`SkipReason`.
"""

from __future__ import annotations

import fnmatch
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypeAlias

from legacylenscli.ir.models import FileRecord, Language

from .hashing import CHUNK_SIZE, EXTRACTOR_VERSION, digest_stream

__all__ = [
    "DEFAULT_EXCLUDES",
    "DEFAULT_MAX_FILE_BYTES",
    "HEAD_SIZE",
    "ScanError",
    "ScanEvent",
    "ScanOptions",
    "ScanResult",
    "ScannedFile",
    "SkipReason",
    "SkippedFile",
    "detect_language",
    "is_probably_binary",
    "iter_scan",
    "scan_repository",
]

HEAD_SIZE = 8192
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_EXCLUDES = (
    "vendor",
    "node_modules",
    ".git",
    "build",
    "target",
    "dist",
    ".venv",
    "__pycache__",
)

_PHP_EXTENSIONS = frozenset(
    {".php", ".php3", ".php4", ".php5", ".php7", ".php8", ".phtml", ".phps"}
)
_JSP_EXTENSIONS = frozenset({".jsp", ".jspx", ".jspf", ".tag", ".tagx"})
_XML_EXTENSIONS = frozenset({".xml", ".tld", ".xsd"})
_EXTENSION_LANGUAGES = {
    **{extension: Language.PHP for extension in _PHP_EXTENSIONS},
    ".java": Language.JAVA,
    **{extension: Language.JSP for extension in _JSP_EXTENSIONS},
    **{extension: Language.XML for extension in _XML_EXTENSIONS},
}


class SkipReason(StrEnum):
    """Why a discovered file was not turned into a :class:`FileRecord`."""

    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    SYMLINK = "symlink"
    UNREADABLE = "unreadable"
    INVALID_PATH = "invalid_path"


@dataclass(frozen=True, slots=True)
class ScanOptions:
    """Scanner configuration; defaults mirror ``[tool.legacylenscli]`` in ``pyproject.toml``."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    follow_symlinks: bool = False
    extractor_version: str = EXTRACTOR_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "include", tuple(self.include))
        object.__setattr__(self, "exclude", tuple(self.exclude))
        if self.max_file_bytes < 0:
            raise ValueError("max_file_bytes must be >= 0")
        if not self.extractor_version:
            raise ValueError("extractor_version must not be empty")


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """An accepted source file and its populated IR metadata."""

    absolute_path: Path
    record: FileRecord

    @property
    def path(self) -> str:
        return self.record.path


@dataclass(frozen=True, slots=True)
class SkippedFile:
    """A discovered file that was rejected before parsing."""

    path: str
    reason: SkipReason
    detail: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ScanError:
    """A recoverable directory-level error; the walk continues elsewhere."""

    path: str
    message: str


ScanEvent: TypeAlias = ScannedFile | SkippedFile | ScanError


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Collected scanner output; use ``iter_scan`` when O(1) memory matters."""

    root: Path
    files: tuple[ScannedFile, ...]
    skipped: tuple[SkippedFile, ...]
    errors: tuple[ScanError, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(item.record.size_bytes for item in self.files)

    @property
    def by_language(self) -> dict[Language, int]:
        counts = Counter(item.record.language for item in self.files)
        return {language: counts[language] for language in sorted(counts, key=lambda x: x.value)}


def detect_language(path: str | Path, head: bytes = b"") -> Language | None:
    """Classify a file from its suffix and, for ambiguous cases, its first bytes."""

    suffix = PurePosixPath(str(path)).suffix.lower()
    language = _EXTENSION_LANGUAGES.get(suffix)
    if language is not None:
        return language
    if suffix == ".inc":
        return Language.PHP if _looks_like_php(head) else None
    if not suffix and _looks_like_php(head):
        return Language.PHP
    return None


def is_probably_binary(head: bytes) -> bool:
    """Conservative binary check over a bounded head buffer."""

    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in head:
        return True
    controls = sum(1 for byte in head if byte < 32 and byte not in (9, 10, 12, 13))
    return controls / len(head) > 0.30


def iter_scan(
    root: str | Path,
    options: ScanOptions = ScanOptions(),
) -> Iterator[ScanEvent]:
    """Walk ``root`` deterministically, yielding files, skips and errors."""

    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"scan root is not a directory: {root_path}")

    visited = {os.path.realpath(root_path)}
    yield from _walk(root_path, (), options, visited)


def scan_repository(
    root: str | Path,
    options: ScanOptions = ScanOptions(),
) -> ScanResult:
    """Collect :func:`iter_scan` into a materialised :class:`ScanResult`."""

    root_path = Path(root).resolve()
    files: list[ScannedFile] = []
    skipped: list[SkippedFile] = []
    errors: list[ScanError] = []
    for event in iter_scan(root_path, options):
        if isinstance(event, ScannedFile):
            files.append(event)
        elif isinstance(event, SkippedFile):
            skipped.append(event)
        else:
            errors.append(event)
    return ScanResult(
        root=root_path,
        files=tuple(files),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def _walk(
    directory: Path,
    parts: tuple[str, ...],
    options: ScanOptions,
    visited: set[str],
) -> Iterator[ScanEvent]:
    relative_directory = "/".join(parts) or "."
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError as exc:
        yield ScanError(path=relative_directory, message=str(exc))
        return

    for entry in entries:
        relative_path = "/".join((*parts, entry.name))
        try:
            is_symlink = entry.is_symlink()
            if is_symlink and not options.follow_symlinks:
                if _excluded(relative_path, entry.name, options.exclude):
                    continue
                target_is_dir = entry.is_dir(follow_symlinks=True)
                if not target_is_dir and options.include:
                    if not _included(relative_path, entry.name, options.include):
                        continue
                yield SkippedFile(
                    path=relative_path,
                    reason=SkipReason.SYMLINK,
                    detail="directory symlink" if target_is_dir else "file symlink",
                )
                continue

            if entry.is_dir(follow_symlinks=options.follow_symlinks):
                if _excluded(relative_path, entry.name, options.exclude):
                    continue
                if not _valid_ir_path(relative_path):
                    yield SkippedFile(
                        path=relative_path,
                        reason=SkipReason.INVALID_PATH,
                        detail="directory path contains a reserved character",
                    )
                    continue
                if options.follow_symlinks:
                    real_path = os.path.realpath(entry.path)
                    if real_path in visited:
                        yield SkippedFile(
                            path=relative_path,
                            reason=SkipReason.SYMLINK,
                            detail="symlink cycle or already visited directory",
                        )
                        continue
                    visited.add(real_path)
                yield from _walk(
                    Path(entry.path),
                    (*parts, entry.name),
                    options,
                    visited,
                )
                continue

            if _excluded(relative_path, entry.name, options.exclude):
                continue
            if options.include and not _included(relative_path, entry.name, options.include):
                continue
            if not entry.is_file(follow_symlinks=options.follow_symlinks):
                continue
            if not _valid_ir_path(relative_path):
                yield SkippedFile(
                    path=relative_path,
                    reason=SkipReason.INVALID_PATH,
                    detail="path contains a reserved character",
                )
                continue
            yield from _scan_file(entry, relative_path, options)
        except OSError as exc:
            yield ScanError(path=relative_path, message=str(exc))


def _scan_file(
    entry: os.DirEntry[str],
    relative_path: str,
    options: ScanOptions,
) -> Iterator[ScanEvent]:
    try:
        file_stat = entry.stat(follow_symlinks=options.follow_symlinks)
    except OSError as exc:
        yield SkippedFile(
            path=relative_path,
            reason=SkipReason.UNREADABLE,
            detail=str(exc),
        )
        return

    if file_stat.st_size > options.max_file_bytes:
        yield SkippedFile(
            path=relative_path,
            reason=SkipReason.TOO_LARGE,
            detail=f"{file_stat.st_size} bytes exceeds {options.max_file_bytes}",
            size_bytes=file_stat.st_size,
        )
        return

    try:
        skip: SkippedFile | None = None
        language: Language | None = None
        digest = None
        with open(entry.path, "rb") as stream:
            head = stream.read(HEAD_SIZE)
            if is_probably_binary(head):
                skip = SkippedFile(
                    path=relative_path,
                    reason=SkipReason.BINARY,
                    size_bytes=file_stat.st_size,
                )
            else:
                language = detect_language(relative_path, head)
                if language is None:
                    skip = SkippedFile(
                        path=relative_path,
                        reason=SkipReason.UNSUPPORTED,
                        size_bytes=file_stat.st_size,
                    )
                else:
                    digest = digest_stream(
                        stream,
                        head=head,
                        extractor_version=options.extractor_version,
                        chunk_size=CHUNK_SIZE,
                    )
    except OSError as exc:
        yield SkippedFile(
            path=relative_path,
            reason=SkipReason.UNREADABLE,
            detail=str(exc),
            size_bytes=file_stat.st_size,
        )
        return

    if skip is not None:
        yield skip
        return

    assert digest is not None and language is not None
    if digest.size_bytes > options.max_file_bytes:
        yield SkippedFile(
            path=relative_path,
            reason=SkipReason.TOO_LARGE,
            detail="file grew beyond the size limit during scanning",
            size_bytes=digest.size_bytes,
        )
        return

    yield ScannedFile(
        absolute_path=Path(entry.path),
        record=FileRecord(
            path=relative_path,
            language=language,
            content_hash=digest.content_hash,
            size_bytes=digest.size_bytes,
            loc=digest.loc,
        ),
    )


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _excluded(relative_path: str, name: str, patterns: tuple[str, ...]) -> bool:
    return _matches(name, patterns) or _matches(relative_path, patterns)


def _included(relative_path: str, name: str, patterns: tuple[str, ...]) -> bool:
    return _matches(name, patterns) or _matches(relative_path, patterns)


def _valid_ir_path(path: str) -> bool:
    return "#" not in path and "\n" not in path and "\r" not in path


def _looks_like_php(head: bytes) -> bool:
    lower = head.lower()
    if b"<?php" in lower or b"<?=" in lower:
        return True
    first_line = lower.split(b"\n", 1)[0]
    return first_line.startswith(b"#!") and b"php" in first_line
