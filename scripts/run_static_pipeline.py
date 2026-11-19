"""Run every implemented static analysis over a repository.

This is the current end-to-end entry point until the Typer CLI and publishing pipeline are
wired. It scans the tree, parses each file into the SQLite fact store, builds the aggregate
health report (which embeds inventory, metrics, graphs, entry points, CRUD, side effects,
dead code and business rules) and prints a human summary. Pass ``--output`` to also write the
full JSON report.

Usage:
    uv run python scripts/run_static_pipeline.py tests/fixtures/legacy_web
    uv run python scripts/run_static_pipeline.py path/to/app --output report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from legacylens.analysis import HealthReport, build_health_report
from legacylens.ingestion import ScanOptions, scan_repository
from legacylens.ir import FactStore
from legacylens.parsers import parse_source


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="repository root to analyse")
    parser.add_argument(
        "--output",
        type=Path,
        help="write the complete report as JSON to this path",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="keep the SQLite fact store at this path instead of a temporary database",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="number of ranked functions/tables to print (default: 5)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    if args.top < 0:
        parser.error("--top must be >= 0")

    if args.db is not None:
        report = _analyse(root, args.db, args.top)
    else:
        with TemporaryDirectory() as temp:
            report = _analyse(root, Path(temp) / "facts.db", args.top)

    _print_report(report, top=args.top)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report.as_dict(), indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON report written to {args.output}")


def _analyse(root: Path, db_path: Path, top: int) -> HealthReport:
    scan = scan_repository(root, ScanOptions())
    with FactStore(db_path) as store:
        for scanned in scan.files:
            store.replace_file(
                parse_source(scanned.absolute_path.read_bytes(), scanned.record)
            )
        return build_health_report(store, source_root=root, top_n=top)


def _print_report(report: HealthReport, *, top: int) -> None:
    inventory = report.inventory
    print("\nLegacyLens static report")
    print(f"  status      : {report.status.value.upper()}")
    print(f"  files       : {inventory.files}")
    print(f"  lines       : {inventory.loc}")
    print(f"  classes     : {inventory.classes}")
    print(f"  functions   : {inventory.functions}")
    print(f"  entry points: {len(report.entry_points.entries)}")
    print(f"  parse errors: {inventory.parse_errors}")

    print("\nFindings")
    if not report.findings:
        print("  (none)")
    for finding in report.findings:
        print(
            f"  [{finding.severity.value:8}] {finding.code:26} {finding.message}"
        )

    print("\nEntry points")
    if not report.entry_points.entries:
        print("  (none)")
    for detail in report.entry_points.entries[: max(top, 1) * 3]:
        entry = detail.entry
        handler = f" -> {detail.handlers[0]}" if detail.handlers else ""
        print(f"  {entry.kind.value:8} {entry.path:30} {entry.file_path}:{entry.line}{handler}")

    print("\nDead code")
    if report.dead_code.skipped_reason is not None:
        print(f"  skipped: {report.dead_code.skipped_reason}")
    elif not report.dead_code.candidates:
        print("  (none)")
    for candidate in report.dead_code.candidates:
        print(
            f"  {candidate.qualified_name:40} "
            f"{candidate.file_path}:{candidate.start_line}"
        )

    print("\nBusiness-rule candidates")
    if not report.business_rules.candidates:
        print("  (none)")
    for rule in report.business_rules.candidates:
        print(
            f"  {rule.kind.value:13} {rule.source.value:11} "
            f"{rule.file_path}:{rule.line}  {rule.evidence}"
        )

    print("\nMost complex functions")
    if top == 0:
        print("  (not requested)")
    elif not report.metrics.most_complex:
        print("  (none)")
    for metric in report.metrics.most_complex[:top]:
        print(
            f"  {metric.qualified_name:40} "
            f"complexity={metric.cyclomatic_complexity:<3} "
            f"nesting={metric.max_nesting:<3} loc={metric.loc:<3} "
            f"{metric.file_path}:{metric.start_line}"
        )

    print("\nCRUD coverage")
    if not report.crud.tables:
        print("  (no tables detected)")
    for table in report.crud.tables[:top]:
        missing = f" missing={','.join(table.missing)}" if table.missing else ""
        print(f"  {table.table_name:30} crud={table.crud or '-'}{missing}")

    print("\nSide effects")
    if not report.side_effects.groups:
        print("  (none)")
    for group in report.side_effects.groups:
        print(f"  {group.kind.value:12} {len(group.records)}")


if __name__ == "__main__":
    main()
