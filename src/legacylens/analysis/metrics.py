"""Function-level metrics: length, cyclomatic complexity and nesting.

Per-function values come from the IR: ``loc`` is the declaration span (including
signature and body, as documented in ``docs/ir-spec.md``), while complexity and nesting
are measured by the parser from the function body.

The module turns those rows into distributions and deterministic top-N rankings. It does
not re-parse source and does not use the call graph; fan-in/fan-out hotspots belong to the
graph stage.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil
from statistics import mean, median

from legacylens.ir.models import FunctionRecord
from legacylens.ir.store import FactStore

__all__ = [
    "Distribution",
    "FunctionMetric",
    "MetricsReport",
    "build_metrics",
    "collect_function_metrics",
]


@dataclass(frozen=True, slots=True)
class FunctionMetric:
    """Metrics for one function, method or constructor."""

    function_id: str
    file_path: str
    qualified_name: str
    start_line: int
    loc: int
    cyclomatic_complexity: int
    max_nesting: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "function_id": self.function_id,
            "file_path": self.file_path,
            "qualified_name": self.qualified_name,
            "start_line": self.start_line,
            "loc": self.loc,
            "cyclomatic_complexity": self.cyclomatic_complexity,
            "max_nesting": self.max_nesting,
        }


@dataclass(frozen=True, slots=True)
class Distribution:
    """Summary statistics for one integer metric."""

    count: int
    total: int
    minimum: int
    maximum: int
    mean: float
    median: float
    p95: int

    @classmethod
    def from_values(cls, values: Iterable[int]) -> Distribution:
        data = sorted(values)
        if not data:
            return cls(
                count=0,
                total=0,
                minimum=0,
                maximum=0,
                mean=0.0,
                median=0.0,
                p95=0,
            )
        # Nearest-rank percentile: the smallest value covering at least 95% of samples.
        p95_index = max(0, ceil(0.95 * len(data)) - 1)
        return cls(
            count=len(data),
            total=sum(data),
            minimum=data[0],
            maximum=data[-1],
            mean=round(mean(data), 2),
            median=float(median(data)),
            p95=data[p95_index],
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "count": self.count,
            "total": self.total,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.mean,
            "median": self.median,
            "p95": self.p95,
        }


@dataclass(frozen=True, slots=True)
class MetricsReport:
    """Project-wide metric distributions and rankings."""

    function_count: int
    length: Distribution
    complexity: Distribution
    nesting: Distribution
    longest: tuple[FunctionMetric, ...]
    most_complex: tuple[FunctionMetric, ...]
    deepest: tuple[FunctionMetric, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "function_count": self.function_count,
            "length": self.length.as_dict(),
            "complexity": self.complexity.as_dict(),
            "nesting": self.nesting.as_dict(),
            "longest": [item.as_dict() for item in self.longest],
            "most_complex": [item.as_dict() for item in self.most_complex],
            "deepest": [item.as_dict() for item in self.deepest],
        }


def collect_function_metrics(store: FactStore) -> tuple[FunctionMetric, ...]:
    """Load one metric row per function, ordered by file and line."""

    return tuple(
        _metric(item)
        for item in sorted(
            store.list_functions(),
            key=lambda value: (value.file_path, value.span.start_line, value.qualified_name),
        )
    )


def build_metrics(store: FactStore, *, top_n: int = 10) -> MetricsReport:
    """Build distributions and top-N rankings for every stored function."""

    if top_n < 0:
        raise ValueError("top_n must be >= 0")

    metrics = collect_function_metrics(store)
    return MetricsReport(
        function_count=len(metrics),
        length=Distribution.from_values(item.loc for item in metrics),
        complexity=Distribution.from_values(item.cyclomatic_complexity for item in metrics),
        nesting=Distribution.from_values(item.max_nesting for item in metrics),
        longest=tuple(
            sorted(
                metrics,
                key=lambda item: (
                    -item.loc,
                    -item.cyclomatic_complexity,
                    item.file_path,
                    item.start_line,
                    item.qualified_name,
                ),
            )[:top_n]
        ),
        most_complex=tuple(
            sorted(
                metrics,
                key=lambda item: (
                    -item.cyclomatic_complexity,
                    -item.loc,
                    item.file_path,
                    item.start_line,
                    item.qualified_name,
                ),
            )[:top_n]
        ),
        deepest=tuple(
            sorted(
                metrics,
                key=lambda item: (
                    -item.max_nesting,
                    -item.cyclomatic_complexity,
                    item.file_path,
                    item.start_line,
                    item.qualified_name,
                ),
            )[:top_n]
        ),
    )


def _metric(function: FunctionRecord) -> FunctionMetric:
    return FunctionMetric(
        function_id=function.id,
        file_path=function.file_path,
        qualified_name=function.qualified_name,
        start_line=function.span.start_line,
        loc=function.loc,
        cyclomatic_complexity=function.cyclomatic_complexity,
        max_nesting=function.max_nesting,
    )
