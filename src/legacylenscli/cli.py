"""Command-line interface: ``analyze``, ``build`` and ``serve``.

The CLI is deliberately thin: it walks a repository into the fact store and delegates to
the existing analysis, publisher and Q&A layers. No analysis logic lives here.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from legacylenscli.analysis import HealthReport, build_health_report
from legacylenscli.ingestion import ScanOptions, scan_repository
from legacylenscli.ir import FactStore
from legacylenscli.parsers import parse_source
from legacylenscli.publisher import SiteBuildError, publish_site
from legacylenscli.qa import serve

__all__ = ["main"]

_DEFAULT_DB = Path(".legacylens/legacylens.db")
_DEFAULT_OUT = Path(".legacylens/site")


def main(argv: Sequence[str] | None = None) -> None:
    """Parse arguments and run one command."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            _analyze(args)
        elif args.command == "build":
            _build(args)
        else:
            _serve(args)
    except (OSError, SiteBuildError, ValueError) as exc:
        parser.error(str(exc))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legacylenscli",
        description="Static analysis and documentation for legacy PHP and Java web apps.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="scan a repository and report its health")
    _add_root(analyze)
    _add_db(analyze)
    analyze.add_argument("--output", type=Path, help="write the full report as JSON")
    analyze.add_argument(
        "--top",
        type=int,
        default=5,
        help="number of ranked functions/tables to print (default: 5)",
    )

    build = commands.add_parser("build", help="generate the static documentation site")
    _add_root(build)
    _add_db(build)
    build.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"documentation output directory (default: {_DEFAULT_OUT})",
    )
    build.add_argument("--project", default="LegacyLens", help="project name in the site")
    build.add_argument(
        "--no-build",
        action="store_true",
        help="write Markdown and mkdocs.yml without invoking MkDocs",
    )
    build.add_argument(
        "--strict",
        action="store_true",
        help="fail the MkDocs build on warnings",
    )

    serve = commands.add_parser("serve", help="serve the local Q&A interface")
    serve.add_argument(
        "--root",
        type=Path,
        help="repository to analyse before serving (omitted to use an existing database)",
    )
    serve.add_argument(
        "--db",
        type=Path,
        help=f"fact store path (default: {_DEFAULT_DB} with --root)",
    )
    serve.add_argument("--docs", type=Path, help="index a published Markdown directory")
    serve.add_argument("--host", default="127.0.0.1", help="bind address")
    serve.add_argument("--port", type=int, default=8000, help="bind port")
    serve.add_argument("--project", default="LegacyLens", help="project name shown in the UI")
    serve.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
        help="uvicorn log level",
    )
    return parser


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, required=True, help="repository root to analyse")


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help=f"SQLite fact store path (default: {_DEFAULT_DB})",
    )


def _analyze(args: argparse.Namespace) -> None:
    if args.top < 0:
        raise ValueError("--top must be >= 0")
    root = _root(args.root)
    with FactStore(args.db) as store:
        _populate(store, root)
        report = build_health_report(store, source_root=root, top_n=args.top)
    _print_report(report, top=args.top)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON report written to {args.output}")


def _build(args: argparse.Namespace) -> None:
    root = _root(args.root)
    with FactStore(args.db) as store:
        _populate(store, root)
        result = publish_site(
            store,
            args.out,
            project=args.project,
            build=not args.no_build,
            strict=args.strict,
        )
    print(f"Documentation written to {result.docs_dir} ({len(result.pages)} pages)")
    if result.built:
        print(f"HTML site written to {result.site_dir}")
    elif result.skipped_reason is not None:
        print(f"HTML build skipped: {result.skipped_reason}")


def _serve(args: argparse.Namespace) -> None:
    if args.root is None and args.db is None:
        raise ValueError("serve requires --root or --db")
    if not 0 < args.port < 65_536:
        raise ValueError("--port must be between 1 and 65535")

    db_path = args.db or _DEFAULT_DB
    if args.root is not None:
        root = _root(args.root)
        with FactStore(db_path) as store:
            _populate(store, root)
        print(f"Analysed {root} into {db_path}")
    elif not db_path.is_file():
        raise ValueError(f"fact store does not exist: {db_path}")

    serve(
        db_path,
        host=args.host,
        port=args.port,
        docs_dir=args.docs,
        project=args.project,
        log_level=args.log_level,
    )


def _populate(store: FactStore, root: Path) -> None:
    """Replace stored facts with the current scan, removing deleted files."""

    scan = scan_repository(root, ScanOptions())
    present: set[str] = set()
    for scanned in scan.files:
        present.add(scanned.record.path)
        store.replace_file(
            parse_source(scanned.absolute_path.read_bytes(), scanned.record)
        )
    for existing in store.list_files():
        if existing.path not in present:
            store.delete_file(existing.path)


def _root(value: Path) -> Path:
    root = value.resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    return root


def _print_report(report: HealthReport, *, top: int) -> None:
    inventory = report.inventory
    print("LegacyLens static report")
    print(f"  status      : {report.status.value.upper()}")
    print(f"  files       : {inventory.files}")
    print(f"  lines       : {inventory.loc}")
    print(f"  classes     : {inventory.classes}")
    print(f"  functions   : {inventory.functions}")
    print(f"  entry points: {len(report.entry_points.entries)}")
    print(f"  findings    : {len(report.findings)}")

    if report.metrics.most_complex:
        print("\nMost complex functions")
        for metric in report.metrics.most_complex[:top]:
            print(
                f"  {metric.qualified_name:40} "
                f"complexity={metric.cyclomatic_complexity:<3} "
                f"loc={metric.loc:<3} {metric.file_path}:{metric.start_line}"
            )
    if report.crud.tables:
        print("\nCRUD coverage")
        for table in report.crud.tables[:top]:
            missing = f" missing={','.join(table.missing)}" if table.missing else ""
            print(f"  {table.table_name:30} crud={table.crud or '-'}{missing}")
