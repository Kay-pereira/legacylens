"""Measure repository scan time, throughput and memory.

Run from a checkout (``uv run`` installs the project):

    uv run python scripts/bench_scan.py --root tests/fixtures
    uv run python scripts/bench_scan.py --root path/to/legacy-app --json
"""

from __future__ import annotations

import argparse
import json
import sys
import tracemalloc
from collections import Counter
from pathlib import Path
from time import perf_counter

from legacylenscli.ingestion import (
    DEFAULT_EXCLUDES,
    DEFAULT_MAX_FILE_BYTES,
    ScanError,
    ScannedFile,
    ScanOptions,
    SkippedFile,
    iter_scan,
)


def _mib(value: int | float) -> float:
    return float(value) / (1024 * 1024)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="repository directory to scan")
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help=f"per-file size limit (default: {DEFAULT_MAX_FILE_BYTES})",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="only scan matching relative paths or names; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="additional exclusion patterns; defaults are always applied",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    options = ScanOptions(
        include=tuple(args.include),
        exclude=tuple(DEFAULT_EXCLUDES) + tuple(args.exclude),
        max_file_bytes=args.max_file_bytes,
    )

    languages: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    files = 0
    total_bytes = 0
    errors = 0

    tracemalloc.start()
    started = perf_counter()
    try:
        for event in iter_scan(args.root, options):
            if isinstance(event, ScannedFile):
                files += 1
                total_bytes += event.record.size_bytes
                languages[event.record.language.value] += 1
            elif isinstance(event, SkippedFile):
                reasons[event.reason.value] += 1
            elif isinstance(event, ScanError):
                errors += 1
    except OSError as exc:
        print(f"bench_scan: {exc}", file=sys.stderr)
        return 2
    elapsed = perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    report = {
        "root": str(Path(args.root).resolve()),
        "files": files,
        "bytes": total_bytes,
        "mib": round(_mib(total_bytes), 3),
        "seconds": round(elapsed, 4),
        "files_per_second": round(files / elapsed, 2) if elapsed else None,
        "mib_per_second": round(_mib(total_bytes) / elapsed, 2) if elapsed else None,
        "peak_memory_mib": round(_mib(peak), 3),
        "languages": dict(sorted(languages.items())),
        "skipped": dict(sorted(reasons.items())),
        "errors": errors,
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"root:              {report['root']}")
        print(f"accepted files:    {files:,}")
        print(f"accepted bytes:    {total_bytes:,} ({_mib(total_bytes):.2f} MiB)")
        print(f"elapsed:           {elapsed:.3f}s")
        if elapsed:
            print(
                f"throughput:        {files / elapsed:,.0f} files/s, "
                f"{_mib(total_bytes) / elapsed:,.2f} MiB/s"
            )
        print(f"peak traced memory:{peak / (1024 * 1024):>8.2f} MiB")
        print(f"languages:         {dict(sorted(languages.items())) or '-'}")
        print(f"skipped:           {dict(sorted(reasons.items())) or '-'}")
        print(f"errors:            {errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
