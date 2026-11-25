"""Project health report assembled from the deterministic analyses.

The report is a reproducible snapshot, not a score invented by an LLM. It runs the
existing inventory, metrics, graph, entry-point, CRUD, side-effect, dead-code and
business-rule analyses and turns their counts into a small set of stable findings.

Severity is rule-based:

* ``critical`` — parse errors affect at least ``parse_error_critical_ratio`` of files;
* ``warning`` — parse errors, unresolved references, dead code or functions above the
  complexity threshold exist;
* ``info`` — long/deep functions, CRUD gaps, absent auth evidence, external effects and
  business-rule candidates.

Thresholds are explicit arguments with fixed defaults, so the same store and settings
always produce the same status and findings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from legacylenscli.ir.store import FactStore

from .crud import CrudMatrix, build_crud
from .entrypoints import EntryPointsReport, build_entrypoints
from .graph import UnresolvedEdge, build_graphs
from .inventory import Inventory, build_inventory
from .metrics import (
    FunctionMetric,
    MetricsReport,
    build_metrics,
    collect_function_metrics,
)
from .rules import (
    BusinessRuleReport,
    DeadCodeReport,
    build_business_rules,
    build_dead_code,
)
from .side_effects import SideEffectReport, build_side_effect_report

__all__ = [
    "FindingSeverity",
    "HealthFinding",
    "HealthReport",
    "HealthStatus",
    "build_health_report",
]

_SEVERITY_RANK = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}


class FindingSeverity(StrEnum):
    """How strongly a finding should draw attention."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class HealthStatus(StrEnum):
    """Overall status derived from the most severe finding."""

    OK = "ok"
    ATTENTION = "attention"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class HealthFinding:
    """One deterministic health observation."""

    code: str
    severity: FindingSeverity
    message: str
    value: int | float | None = None
    threshold: int | float | None = None
    citations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "citations": list(self.citations),
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    """All health findings plus the analysis reports they were built from."""

    status: HealthStatus
    findings: tuple[HealthFinding, ...]
    inventory: Inventory
    metrics: MetricsReport
    entry_points: EntryPointsReport
    unresolved_references: tuple[UnresolvedEdge, ...]
    crud: CrudMatrix
    side_effects: SideEffectReport
    dead_code: DeadCodeReport
    business_rules: BusinessRuleReport

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "summary": {
                "files": self.inventory.files,
                "loc": self.inventory.loc,
                "functions": self.inventory.functions,
                "entry_points": len(self.entry_points.entries),
                "unresolved_references": len(self.unresolved_references),
                "dead_code": len(self.dead_code.candidates),
                "business_rules": len(self.business_rules.candidates),
            },
            "findings": [item.as_dict() for item in self.findings],
            "inventory": self.inventory.as_dict(),
            "metrics": self.metrics.as_dict(),
            "entry_points": self.entry_points.as_dict(),
            "unresolved_references": [
                item.as_dict() for item in self.unresolved_references
            ],
            "crud": self.crud.as_dict(),
            "side_effects": self.side_effects.as_dict(),
            "dead_code": self.dead_code.as_dict(),
            "business_rules": self.business_rules.as_dict(),
        }


def build_health_report(
    store: FactStore,
    *,
    source_root: str | Path | None = None,
    top_n: int = 10,
    complexity_threshold: int = 10,
    function_length_threshold: int = 80,
    nesting_threshold: int = 4,
    parse_error_critical_ratio: float = 0.25,
) -> HealthReport:
    """Run every deterministic analysis and derive the project health findings."""

    if top_n < 0:
        raise ValueError("top_n must be >= 0")
    if complexity_threshold < 1:
        raise ValueError("complexity_threshold must be >= 1")
    if function_length_threshold < 1:
        raise ValueError("function_length_threshold must be >= 1")
    if nesting_threshold < 1:
        raise ValueError("nesting_threshold must be >= 1")
    if not 0.0 <= parse_error_critical_ratio <= 1.0:
        raise ValueError("parse_error_critical_ratio must be between 0 and 1")

    inventory = build_inventory(store)
    metrics = build_metrics(store, top_n=top_n)
    entries = build_entrypoints(store)
    graphs = build_graphs(store)
    crud = build_crud(store)
    effects = build_side_effect_report(store)
    dead_code = build_dead_code(store)
    business_rules = build_business_rules(store, source_root=source_root)
    function_metrics = collect_function_metrics(store)
    store_summary = store.summary()

    findings: list[HealthFinding] = []

    if inventory.parse_errors:
        ratio = inventory.parse_errors / inventory.files if inventory.files else 0.0
        severity = (
            FindingSeverity.CRITICAL
            if ratio >= parse_error_critical_ratio
            else FindingSeverity.WARNING
        )
        findings.append(
            HealthFinding(
                code="parse-errors",
                severity=severity,
                message=f"{inventory.parse_errors} file(s) have parse errors",
                value=inventory.parse_errors,
                threshold=round(parse_error_critical_ratio, 2),
            )
        )

    if graphs.unresolved:
        ratio = (
            len(graphs.unresolved) / store_summary.edges
            if store_summary.edges
            else 0.0
        )
        findings.append(
            HealthFinding(
                code="unresolved-references",
                severity=(
                    FindingSeverity.WARNING
                    if ratio >= 0.1
                    else FindingSeverity.INFO
                ),
                message=f"{len(graphs.unresolved)} reference(s) could not be resolved",
                value=len(graphs.unresolved),
                threshold=0.1,
                citations=_unresolved_citations(graphs.unresolved),
            )
        )

    if dead_code.candidates:
        findings.append(
            HealthFinding(
                code="dead-code",
                severity=FindingSeverity.WARNING,
                message=f"{len(dead_code.candidates)} function(s) are unreachable from an entry point",
                value=len(dead_code.candidates),
                citations=tuple(
                    item.function_id for item in dead_code.candidates[:10]
                ),
            )
        )
    elif dead_code.skipped_reason is not None:
        findings.append(
            HealthFinding(
                code="dead-code-skipped",
                severity=FindingSeverity.INFO,
                message=f"dead-code analysis skipped: {dead_code.skipped_reason}",
            )
        )

    complex_functions = sorted(
        (
            item
            for item in function_metrics
            if item.cyclomatic_complexity >= complexity_threshold
        ),
        key=lambda item: (
            -item.cyclomatic_complexity,
            -item.loc,
            item.file_path,
            item.start_line,
            item.qualified_name,
        ),
    )
    if complex_functions:
        findings.append(
            HealthFinding(
                code="high-complexity",
                severity=FindingSeverity.WARNING,
                message=(
                    f"{len(complex_functions)} function(s) have cyclomatic complexity "
                    f">= {complexity_threshold}"
                ),
                value=len(complex_functions),
                threshold=complexity_threshold,
                citations=_metric_citations(complex_functions),
            )
        )

    long_functions = sorted(
        (item for item in function_metrics if item.loc >= function_length_threshold),
        key=lambda item: (-item.loc, item.file_path, item.start_line, item.qualified_name),
    )
    if long_functions:
        findings.append(
            HealthFinding(
                code="long-functions",
                severity=FindingSeverity.INFO,
                message=(
                    f"{len(long_functions)} function(s) span {function_length_threshold} "
                    "or more lines"
                ),
                value=len(long_functions),
                threshold=function_length_threshold,
                citations=_metric_citations(long_functions),
            )
        )

    deep_functions = sorted(
        (item for item in function_metrics if item.max_nesting >= nesting_threshold),
        key=lambda item: (
            -item.max_nesting,
            -item.cyclomatic_complexity,
            item.file_path,
            item.start_line,
            item.qualified_name,
        ),
    )
    if deep_functions:
        findings.append(
            HealthFinding(
                code="deep-nesting",
                severity=FindingSeverity.INFO,
                message=(
                    f"{len(deep_functions)} function(s) nest control flow "
                    f"{nesting_threshold} or more levels deep"
                ),
                value=len(deep_functions),
                threshold=nesting_threshold,
                citations=_metric_citations(deep_functions),
            )
        )

    missing_operations = sum(len(table.missing) for table in crud.tables)
    if missing_operations:
        findings.append(
            HealthFinding(
                code="crud-gaps",
                severity=FindingSeverity.INFO,
                message=(
                    f"{missing_operations} CRUD operation(s) are not exercised for known tables"
                ),
                value=missing_operations,
                citations=tuple(
                    f"table:{table.table_name}"
                    for table in crud.tables
                    if table.missing
                )[:10],
            )
        )

    unknown_auth = sum(1 for item in entries.entries if item.entry.requires_auth is None)
    if entries.entries and unknown_auth:
        findings.append(
            HealthFinding(
                code="auth-evidence",
                severity=FindingSeverity.INFO,
                message=(
                    f"authentication requirements were not observed for {unknown_auth} "
                    "entry point(s)"
                ),
                value=unknown_auth,
            )
        )

    if effects.total:
        findings.append(
            HealthFinding(
                code="external-effects",
                severity=FindingSeverity.INFO,
                message=f"{effects.total} external side effect(s) were recorded",
                value=effects.total,
            )
        )

    if business_rules.candidates:
        findings.append(
            HealthFinding(
                code="business-rule-candidates",
                severity=FindingSeverity.INFO,
                message=f"{len(business_rules.candidates)} business-rule candidate(s) were mined",
                value=len(business_rules.candidates),
            )
        )

    return HealthReport(
        status=_status(findings),
        findings=tuple(findings),
        inventory=inventory,
        metrics=metrics,
        entry_points=entries,
        unresolved_references=tuple(graphs.unresolved),
        crud=crud,
        side_effects=effects,
        dead_code=dead_code,
        business_rules=business_rules,
    )


def _status(findings: list[HealthFinding]) -> HealthStatus:
    rank = max(
        (_SEVERITY_RANK[item.severity.value] for item in findings),
        default=0,
    )
    if rank >= _SEVERITY_RANK[FindingSeverity.CRITICAL.value]:
        return HealthStatus.CRITICAL
    if rank >= _SEVERITY_RANK[FindingSeverity.WARNING.value]:
        return HealthStatus.ATTENTION
    return HealthStatus.OK


def _metric_citations(items: list[FunctionMetric]) -> tuple[str, ...]:
    return tuple(item.function_id for item in items[:10])


def _unresolved_citations(items: tuple[UnresolvedEdge, ...]) -> tuple[str, ...]:
    return tuple(
        f"{item.file_path}:{item.line}" for item in items[:10]
    )
