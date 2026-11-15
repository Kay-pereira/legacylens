"""Hashing: known digests, extractor versioning, streaming and line counts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from legacylens.ingestion import (
    FileDigest,
    digest_stream,
    file_digest,
    hash_bytes,
    hash_file,
    hash_text,
)
from legacylens.ingestion.hashing import EXTRACTOR_VERSION


def test_hash_bytes_known_vectors() -> None:
    assert hash_bytes(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert hash_bytes(b"abc") == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_hash_text_matches_utf8_bytes() -> None:
    assert hash_text("héllo") == hash_bytes("héllo".encode())


def test_hash_file_is_a_plain_content_digest(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_bytes(b"abc")
    assert hash_file(path) == hash_bytes(b"abc")
    assert hash_file(path, chunk_size=1) == hash_bytes(b"abc")


@pytest.mark.parametrize(
    ("content", "expected_loc"),
    [
        (b"", 0),
        (b"a", 1),
        (b"a\n", 1),
        (b"a\nb", 2),
        (b"a\r\nb\r\n", 2),
        (b"\n", 1),
        (b"\n\n", 2),
    ],
)
def test_file_digest_counts_physical_lines(
    tmp_path: Path, content: bytes, expected_loc: int
) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(content)
    digest = file_digest(path)
    assert digest.loc == expected_loc
    assert digest.size_bytes == len(content)


def test_extractor_version_changes_the_digest(tmp_path: Path) -> None:
    path = tmp_path / "source.php"
    path.write_bytes(b"<?php echo 'x';")
    current = file_digest(path)
    other = file_digest(path, extractor_version="other-parser")
    assert current.content_hash != other.content_hash
    assert current.size_bytes == other.size_bytes


def test_extractor_digest_is_version_then_nul_then_content(tmp_path: Path) -> None:
    content = b"<?php"
    path = tmp_path / "source.php"
    path.write_bytes(content)
    expected = (
        "sha256:" + hashlib.sha256(EXTRACTOR_VERSION.encode() + b"\x00" + content).hexdigest()
    )
    assert file_digest(path).content_hash == expected


def test_chunk_size_does_not_change_the_digest(tmp_path: Path) -> None:
    path = tmp_path / "large.bin"
    path.write_bytes(bytes(range(256)) * 4096)
    assert file_digest(path, chunk_size=7) == file_digest(path)


def test_digest_stream_hashes_an_already_read_head(tmp_path: Path) -> None:
    content = b"abcdefghij" * 1000
    path = tmp_path / "source.php"
    path.write_bytes(content)
    with path.open("rb") as stream:
        head = stream.read(17)
        from_head = digest_stream(stream, head=head)
    assert from_head == file_digest(path)


def test_file_digest_rejects_a_malformed_hash() -> None:
    with pytest.raises(ValueError, match="sha256"):
        FileDigest(content_hash="md5:abc", size_bytes=1, loc=1)
