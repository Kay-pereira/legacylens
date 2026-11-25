"""Pipeline runner for the three evaluation configurations.

Each run is isolated in its own SQLite store so cached completions, generated claims and
verification state from one configuration cannot leak into another. Ground truth is loaded
from JSON, validated strictly, and compared against deterministic static reports in every
configuration; only the claim and usage sections differ between configurations.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from legacylenscli.analysis import (
    BusinessRuleReport,
    CrudMatrix,
    EntryPointsReport,
    build_business_rules,
    build_crud,
    build_entrypoints,
)
from legacylenscli.ingestion import ScanOptions, scan_repository
from legacylenscli.ir import ClaimOrigin, Confidence, FactStore, SqlOperation
from legacylenscli.llm import Provider, explain_target
from legacylenscli.parsers import parse_source
from legacylenscli.verification import VerificationReport, verify_claims

from .metrics import (
    ClaimMetrics,
    EvaluationMetrics,
    UsageMetrics,
    business_rule_keys,
    compare_sets,
    entry_point_keys,
    normalise_identifier,
    table_operation_key,
    table_operation_keys,
)

__all__ = [
    "Configuration",
    "EvaluationResult",
    "EvaluationSuite",
    "GroundTruth",
    "GroundTruthError",
    "TableGroundTruth",
    "load_ground_truth",
    "run_configuration",
    "run_evaluation",
]

_GROUND_TRUTH_KEYS = frozenset({"app", "commit", "entry_points", "tables", "business_rules", "notes"})
_TABLE_KEYS = frozenset({"name", "operations"})
_OPERATIONS = frozenset(item.value.upper() for item in SqlOperation)


class GroundTruthError(ValueError):
    """Raised when a ground-truth file does not match the documented schema."""


@dataclass(frozen=True, slots=True)
class TableGroundTruth:
    """The operations manual review confirmed for one table."""

    name: str
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise GroundTruthError("table name must not be empty")
        normalised = tuple(operation.strip().upper() for operation in self.operations)
        if not normalised:
            raise GroundTruthError(f"table {self.name!r} must list at least one operation")
        unknown = sorted(set(normalised) - _OPERATIONS)
        if unknown:
            raise GroundTruthError(
                f"table {self.name!r} has unknown operations {unknown}; "
                f"expected {sorted(_OPERATIONS)}"
            )
        if len(set(normalised)) != len(normalised):
            raise GroundTruthError(f"table {self.name!r} lists a duplicate operation")
        object.__setattr__(self, "operations", normalised)

    @property
    def keys(self) -> set[str]:
        return {
            table_operation_key(self.name, operation) for operation in self.operations
        }

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "operations": list(self.operations)}


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """Hand-prepared reference answers for one application."""

    app: str
    entry_points: tuple[str, ...] = ()
    tables: tuple[TableGroundTruth, ...] = ()
    business_rules: tuple[str, ...] = ()
    commit: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.app.strip():
            raise GroundTruthError("app must not be empty")
        entries = tuple(normalise_identifier(item) for item in self.entry_points)
        if any(not item for item in entries):
            raise GroundTruthError("entry point identifiers must not be empty")
        if len(set(entries)) != len(entries):
            raise GroundTruthError("entry points contain a duplicate identifier")
        rules = tuple(normalise_identifier(item) for item in self.business_rules)
        if any(not item for item in rules):
            raise GroundTruthError("business-rule identifiers must not be empty")
        if len(set(rules)) != len(rules):
            raise GroundTruthError("business rules contain a duplicate identifier")
        names = [item.name.casefold() for item in self.tables]
        if len(set(names)) != len(names):
            raise GroundTruthError("tables contain a duplicate name")
        object.__setattr__(self, "entry_points", entries)
        object.__setattr__(self, "business_rules", rules)

    @property
    def entry_point_keys(self) -> set[str]:
        return set(self.entry_points)

    @property
    def table_operation_keys(self) -> set[str]:
        return {key for table in self.tables for key in table.keys}

    @property
    def business_rule_keys(self) -> set[str]:
        return set(self.business_rules)

    def as_dict(self) -> dict[str, object]:
        return {
            "app": self.app,
            "commit": self.commit,
            "entry_points": list(self.entry_points),
            "tables": [table.as_dict() for table in self.tables],
            "business_rules": list(self.business_rules),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> GroundTruth:
        if not isinstance(data, Mapping):
            raise GroundTruthError("ground truth must be a JSON object")
        unknown = sorted(set(data) - _GROUND_TRUTH_KEYS)
        if unknown:
            raise GroundTruthError(f"unknown ground-truth fields: {unknown}")
        app = _required_string(data.get("app"), "app")
        commit = _optional_string(data.get("commit"), "commit")
        notes = _optional_string(data.get("notes"), "notes")
        entry_points = _string_list(data.get("entry_points", []), "entry_points")
        business_rules = _string_list(data.get("business_rules", []), "business_rules")

        raw_tables = data.get("tables", [])
        if not isinstance(raw_tables, list):
            raise GroundTruthError("tables must be a list")
        tables: list[TableGroundTruth] = []
        for index, item in enumerate(raw_tables):
            if not isinstance(item, Mapping):
                raise GroundTruthError(f"tables[{index}] must be an object")
            table_unknown = sorted(set(item) - _TABLE_KEYS)
            if table_unknown:
                raise GroundTruthError(
                    f"tables[{index}] has unknown fields: {table_unknown}"
                )
            name = _required_string(item.get("name"), f"tables[{index}].name")
            operations = _string_list(
                item.get("operations", []), f"tables[{index}].operations"
            )
            tables.append(TableGroundTruth(name=name, operations=tuple(operations)))

        return cls(
            app=app,
            commit=commit,
            notes=notes,
            entry_points=tuple(entry_points),
            tables=tuple(tables),
            business_rules=tuple(business_rules),
        )


def load_ground_truth(path: str | Path) -> GroundTruth:
    """Load and validate one ground-truth JSON file."""

    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GroundTruthError(f"cannot read ground truth {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GroundTruthError(f"ground truth {target} is not valid JSON: {exc}") from exc
    return GroundTruth.from_dict(data)


class Configuration(StrEnum):
    """The three evaluated pipeline configurations."""

    STATIC_ONLY = "static_only"
    LLM_NO_VERIFICATION = "llm_no_verification"
    FULL_PIPELINE = "full_pipeline"


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """One configuration's static metrics, claim state and LLM usage."""

    app: str
    configuration: Configuration
    metrics: EvaluationMetrics
    claims: ClaimMetrics
    usage: UsageMetrics
    targets: tuple[str, ...]
    facts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "app": self.app,
            "configuration": self.configuration.value,
            "metrics": self.metrics.as_dict(),
            "claims": self.claims.as_dict(),
            "usage": self.usage.as_dict(),
            "llm_targets": list(self.targets),
            "facts": dict(self.facts),
        }


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    """The results of every requested configuration for one ground-truth file."""

    ground_truth: GroundTruth
    results: tuple[EvaluationResult, ...]
    usage: UsageMetrics

    def as_dict(self) -> dict[str, object]:
        return {
            "app": self.ground_truth.app,
            "commit": self.ground_truth.commit,
            "results": [result.as_dict() for result in self.results],
            "totals": {"usage": self.usage.as_dict()},
        }

    def write(self, path: str | Path) -> Path:
        """Write deterministic, indented JSON to ``path`` and return it."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return target


def run_configuration(
    root: str | Path,
    ground_truth: GroundTruth,
    configuration: Configuration | str,
    *,
    db_path: str | Path,
    provider: Provider | None = None,
    source_root: str | Path | None = None,
    targets: Iterable[str] | None = None,
    max_targets: int | None = None,
) -> EvaluationResult:
    """Run one configuration into a fresh fact store and score it."""

    root_path = _directory(root, "root")
    source_path = (
        root_path if source_root is None else _directory(source_root, "source_root")
    )
    selected_configuration = Configuration(configuration)
    if max_targets is not None and max_targets < 0:
        raise ValueError("max_targets must be >= 0")
    _validate_provider(selected_configuration, provider)

    store_path = Path(db_path)
    if store_path.exists():
        raise ValueError(f"db_path already exists; use a fresh path: {store_path}")

    with _build_store(root_path, store_path) as store:
        entries = build_entrypoints(store)
        crud = build_crud(store)
        rules = build_business_rules(store, source_root=source_path)
        metrics = _static_metrics(ground_truth, entries, crud, rules)

        selected_targets = (
            ()
            if selected_configuration is Configuration.STATIC_ONLY
            else _select_targets(store, targets, max_targets)
        )
        usage = UsageMetrics()
        if selected_configuration is not Configuration.STATIC_ONLY:
            assert provider is not None
            for target in selected_targets:
                explanation = explain_target(
                    store,
                    target,
                    provider,
                    source_root=source_path,
                )
                usage = usage.with_completion(explanation.completion)

        verification = (
            verify_claims(store, source_root=source_path)
            if selected_configuration is Configuration.FULL_PIPELINE
            else None
        )
        claims = _claim_metrics(store, verification)
        facts = {
            "files": len(store.list_files()),
            "functions": len(store.list_functions()),
            "entry_points": len(entries.entries),
            "tables": len(crud.tables),
            "business_rules": len(rules.candidates),
            "claims": len(store.list_claims()),
        }

    return EvaluationResult(
        app=ground_truth.app,
        configuration=selected_configuration,
        metrics=metrics,
        claims=claims,
        usage=usage,
        targets=selected_targets,
        facts=facts,
    )


def run_evaluation(
    root: str | Path,
    ground_truth: GroundTruth | str | Path,
    *,
    configurations: Iterable[Configuration | str] = (
        Configuration.STATIC_ONLY,
        Configuration.LLM_NO_VERIFICATION,
        Configuration.FULL_PIPELINE,
    ),
    provider: Provider | None = None,
    targets: Iterable[str] | None = None,
    max_targets: int | None = 20,
    work_dir: str | Path | None = None,
    output_path: str | Path | None = None,
) -> EvaluationSuite:
    """Run the requested configurations with isolated stores and optional JSON output."""

    loaded = (
        load_ground_truth(ground_truth)
        if isinstance(ground_truth, (str, Path))
        else ground_truth
    )
    selected = tuple(dict.fromkeys(Configuration(item) for item in configurations))
    if not selected:
        raise ValueError("at least one configuration is required")
    for configuration in selected:
        if configuration is not Configuration.STATIC_ONLY:
            _validate_provider(configuration, provider)

    if work_dir is None:
        with TemporaryDirectory(prefix="legacylenscli-evaluation-") as temp:
            suite = _run_suite(
                Path(root),
                loaded,
                selected,
                Path(temp),
                provider,
                tuple(targets) if targets is not None else None,
                max_targets,
            )
    else:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        suite = _run_suite(
            Path(root),
            loaded,
            selected,
            work,
            provider,
            tuple(targets) if targets is not None else None,
            max_targets,
        )

    if output_path is not None:
        suite.write(output_path)
    return suite


def _run_suite(
    root: Path,
    ground_truth: GroundTruth,
    configurations: tuple[Configuration, ...],
    work_dir: Path,
    provider: Provider | None,
    targets: tuple[str, ...] | None,
    max_targets: int | None,
) -> EvaluationSuite:
    results: list[EvaluationResult] = []
    usage = UsageMetrics()
    for configuration in configurations:
        result = run_configuration(
            root,
            ground_truth,
            configuration,
            db_path=work_dir / f"{configuration.value}.db",
            provider=(
                None if configuration is Configuration.STATIC_ONLY else provider
            ),
            targets=targets,
            max_targets=max_targets,
        )
        results.append(result)
        usage = usage.merge(result.usage)
    return EvaluationSuite(
        ground_truth=ground_truth,
        results=tuple(results),
        usage=usage,
    )


def _build_store(root: Path, db_path: Path) -> FactStore:
    scan = scan_repository(root, ScanOptions())
    store = FactStore(db_path)
    try:
        for scanned in scan.files:
            store.replace_file(
                parse_source(scanned.absolute_path.read_bytes(), scanned.record)
            )
    except BaseException:
        store.close()
        raise
    return store


def _static_metrics(
    ground_truth: GroundTruth,
    entries: EntryPointsReport,
    crud: CrudMatrix,
    rules: BusinessRuleReport,
) -> EvaluationMetrics:
    return EvaluationMetrics(
        entry_points=compare_sets(
            ground_truth.entry_point_keys,
            entry_point_keys(detail.entry for detail in entries.entries),
        ),
        tables=compare_sets(
            ground_truth.table_operation_keys,
            table_operation_keys(crud),
        ),
        business_rules=compare_sets(
            ground_truth.business_rule_keys,
            business_rule_keys(rules.candidates),
        ),
    )


def _claim_metrics(
    store: FactStore,
    verification: VerificationReport | None,
) -> ClaimMetrics:
    claims = store.list_claims(origin=ClaimOrigin.LLM)
    counts = Counter(claim.status for claim in claims)
    if verification is None:
        return ClaimMetrics(
            generated=len(claims),
            inferred=counts[Confidence.INFERRED],
            verified=counts[Confidence.VERIFIED],
            unsupported=counts[Confidence.UNSUPPORTED],
            publishable=len(claims),
        )
    verified_counts = verification.counts
    return ClaimMetrics(
        generated=verification.total,
        verified=verified_counts[Confidence.VERIFIED],
        inferred=verified_counts[Confidence.INFERRED],
        unsupported=verified_counts[Confidence.UNSUPPORTED],
        publishable=verification.resolved,
        verification_rate=verification.verification_rate,
        verified_rate=verification.verified_rate,
    )


def _select_targets(
    store: FactStore,
    targets: Iterable[str] | None,
    max_targets: int | None,
) -> tuple[str, ...]:
    if targets is None:
        selected = [
            function.id
            for function in sorted(
                store.list_functions(),
                key=lambda item: (item.file_path, item.span.start_line, item.qualified_name),
            )
        ]
    else:
        selected = list(dict.fromkeys(targets))
    if max_targets is not None:
        selected = selected[:max_targets]
    return tuple(selected)


def _validate_provider(
    configuration: Configuration,
    provider: Provider | None,
) -> None:
    if configuration is Configuration.STATIC_ONLY:
        if provider is not None and provider.name != "none":
            raise ValueError("static_only does not accept an LLM provider")
        return
    if provider is None or provider.name == "none":
        raise ValueError(
            f"{configuration.value} requires a configured LLM provider"
        )


def _directory(value: str | Path, name: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise ValueError(f"{name} is not a directory: {path}")
    return path.resolve()


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroundTruthError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GroundTruthError(f"{name} must be a string when present")
    return value.strip()


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise GroundTruthError(f"{name} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise GroundTruthError(f"{name}[{index}] must be a non-empty string")
        result.append(item.strip())
    return result
