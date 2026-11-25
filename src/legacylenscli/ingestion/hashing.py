"""Content hashing for incremental analysis and LLM response caching.

Two kinds of digest are exposed:

* ``hash_bytes`` / ``hash_file`` / ``hash_text`` return a plain ``sha256:<hex>`` digest over
  exactly the supplied content.
* ``file_digest`` / ``digest_stream`` return a :class:`FileDigest` whose digest also covers
  ``EXTRACTOR_VERSION``. The version is folded into the file hash so a parser change
  invalidates the files it can affect without a separate cache column.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "CHUNK_SIZE",
    "EXTRACTOR_VERSION",
    "FileDigest",
    "digest_stream",
    "file_digest",
    "hash_bytes",
    "hash_file",
    "hash_text",
]

HASH_ALGORITHM = "sha256"
HASH_PREFIX = f"{HASH_ALGORITHM}:"
EXTRACTOR_VERSION = "1"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileDigest:
    """Digest and physical measurements produced by :func:`file_digest`."""

    content_hash: str
    size_bytes: int
    loc: int

    def __post_init__(self) -> None:
        if not self.content_hash.startswith(HASH_PREFIX):
            raise ValueError(f"content_hash must start with {HASH_PREFIX!r}")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be >= 0")
        if self.loc < 0:
            raise ValueError("loc must be >= 0")


def hash_bytes(data: bytes) -> str:
    """Return the plain SHA-256 digest of ``data``."""

    return f"{HASH_PREFIX}{hashlib.sha256(data).hexdigest()}"


def hash_text(text: str, *, encoding: str = "utf-8") -> str:
    """Return the plain SHA-256 digest of encoded text."""

    return hash_bytes(text.encode(encoding))


def hash_file(path: str | Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the plain SHA-256 digest of a file, read in chunks."""

    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            hasher.update(chunk)
    return f"{HASH_PREFIX}{hasher.hexdigest()}"


def file_digest(
    path: str | Path,
    *,
    extractor_version: str = EXTRACTOR_VERSION,
    chunk_size: int = CHUNK_SIZE,
) -> FileDigest:
    """Hash a file and count its physical lines in one streaming pass."""

    with Path(path).open("rb") as stream:
        return digest_stream(stream, extractor_version=extractor_version, chunk_size=chunk_size)


def digest_stream(
    stream: BinaryIO,
    *,
    head: bytes = b"",
    extractor_version: str = EXTRACTOR_VERSION,
    chunk_size: int = CHUNK_SIZE,
) -> FileDigest:
    """Digest the rest of ``stream``, prefixed by ``head``.

    ``head`` is for callers that already read the first bytes to detect binary content or
    language. The stream must be positioned immediately after ``head``; hashing ``head``
    again is avoided by the caller, not by seeking.
    """

    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if not isinstance(head, bytes):
        raise TypeError("head must be bytes")

    hasher = hashlib.sha256()
    hasher.update(extractor_version.encode("utf-8"))
    hasher.update(b"\x00")

    size_bytes = 0
    newlines = 0
    last_byte: bytes | None = None

    def feed(chunk: bytes) -> None:
        nonlocal size_bytes, newlines, last_byte
        if not chunk:
            return
        hasher.update(chunk)
        size_bytes += len(chunk)
        newlines += chunk.count(b"\n")
        last_byte = chunk[-1:]

    feed(head)
    while chunk := stream.read(chunk_size):
        feed(chunk)

    loc = 0 if size_bytes == 0 else newlines + (0 if last_byte == b"\n" else 1)
    return FileDigest(
        content_hash=f"{HASH_PREFIX}{hasher.hexdigest()}",
        size_bytes=size_bytes,
        loc=loc,
    )
