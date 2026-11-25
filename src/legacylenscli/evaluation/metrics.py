"""Evaluation metrics: set precision/recall, verification counts, and LLM cost.

Detection is scored as a set comparison against hand-prepared ground truth. Identifiers
are normalised before comparison so whitespace and casing in prose-style values cannot
create accidental misses, but paths and expressions are never rewritten or guessed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from legacylenscli.analysis import CrudMatrix, RuleCandidate
from legacylenscli.ir import EntryPointRecord, SqlOperation

__all__ = [
    "ClaimMetrics",
    "EvaluationMetrics",
    "SetMetrics",
    "UsageMetrics",
    "business_rule_keys",
    "compare_sets",
    "entry_point_key",
    "entry_point_keys",
    "normalise_identifier",
    "table_operation_key",
    "table_operation_keys",
]


def normalise_identifier(value: str) -> str:
    """Collapse whitespace and trim a human-authored identifier."""

    return " ".join(value.split())


def entry_point_key(entry: EntryPointRecord) -> str:
    """Return the stable, human-readable identity used to match an entry point.

    The key is ``<kind>:<method> <path>`` when an HTTP method is known and
    ``<kind>:<path>`` otherwise. Ordering a set of keys collapses duplicate declarations
    of the same endpoint (for example an annotation and ``web.xml``).
    """

    method = "" if entry.http_method is None else f"{entry.http_method.value} "
    return f"{entry.kind.value}:{method}{entry.path}"


def entry_point_keys(entries: Iterable[EntryPointRecord]) -> set[str]:
    """Return the de-duplicated entry-point keys for a report."""

    return {normalise_identifier(entry_point_key(item)) for item in entries}


def table_operation_key(table_name: str, operation: SqlOperation | str) -> str:
    """Return the identity of one table/operation pair."""

    value = operation.value if isinstance(operation, SqlOperation) else str(operation)
    return f"{normalise_identifier(table_name).casefold()}:{value.upper()}"


def table_operation_keys(matrix: CrudMatrix) -> set[str]:
    """Return every observed table/operation pair from a CRUD matrix."""

    return {
        table_operation_key(table.table_name, operation.operation)
        for table in matrix.tables
        for operation in table.operations
    }


def business_rule_keys(candidates: Iterable[RuleCandidate]) -> set[str]:
    """Return the normalised evidence text of each business-rule candidate."""

    return {normalise_identifier(item.evidence) for item in candidates}


@dataclass(frozen=True, slots=True)
class SetMetrics:
    """Precision/recall for one detection category."""

    expected: int
    actual: int
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def true_positives(self) -> int:
        return len(self.matched)

    @property
    def false_positives(self) -> int:
        return len(self.unexpected)

    @property
    def false_negatives(self) -> int:
        return len(self.missing)

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return 0.0 if denominator == 0 else self.true_positives / denominator

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return 0.0 if denominator == 0 else self.true_positives / denominator

    @property
    def f1(self) -> float:
        precision = self.precision
        recall = self.recall
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def as_dict(self) -> dict[str, object]:
        return {
            "expected": self.expected,
            "actual": self.actual,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "matched": list(self.matched),
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
        }


def compare_sets(expected: Iterable[str], actual: Iterable[str]) -> SetMetrics:
    """Compare two already-normalised identifier sets."""

    expected_set = set(expected)
    actual_set = set(actual)
    return SetMetrics(
        expected=len(expected_set),
        actual=len(actual_set),
        matched=tuple(sorted(expected_set & actual_set)),
        missing=tuple(sorted(expected_set - actual_set)),
        unexpected=tuple(sorted(actual_set - expected_set)),
    )


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """Static detection metrics for one configuration."""

    entry_points: SetMetrics
    tables: SetMetrics
    business_rules: SetMetrics

    def as_dict(self) -> dict[str, object]:
        return {
            "entry_points": self.entry_points.as_dict(),
            "tables": self.tables.as_dict(),
            "business_rules": self.business_rules.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ClaimMetrics:
    """Claim counts and verification rates for one configuration.

    ``verification_rate`` is ``None`` when the configuration did not run the verifier, so
    an unverified run is never reported as a zero-percent verification failure.
    """

    generated: int = 0
    verified: int = 0
    inferred: int = 0
    unsupported: int = 0
    publishable: int = 0
    verification_rate: float | None = None
    verified_rate: float | None = None

    @property
    def counts(self) -> dict[str, int]:
        return {
            "verified": self.verified,
            "inferred": self.inferred,
            "unsupported": self.unsupported,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "generated": self.generated,
            "counts": self.counts,
            "publishable": self.publishable,
            "verification_rate": (
                None if self.verification_rate is None else round(self.verification_rate, 4)
            ),
            "verified_rate": (
                None if self.verified_rate is None else round(self.verified_rate, 4)
            ),
        }


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    """Accumulated LLM usage for one configuration.

    ``estimated_cost_usd`` is the provider-reported cost of every completion, including
    cache hits, and therefore estimates what an uncached run would cost.
    ``billable_cost_usd`` counts only cache misses, which are the calls that were actually
    sent to the provider.
    """

    requests: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    billable_cost_usd: float = 0.0

    @property
    def cache_misses(self) -> int:
        return self.requests - self.cache_hits

    @property
    def cache_hit_rate(self) -> float:
        return 0.0 if self.requests == 0 else self.cache_hits / self.requests

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def with_completion(self, completion: object) -> UsageMetrics:
        """Return new totals including one ``Completion``."""

        cached = bool(getattr(completion, "cached", False))
        cost = float(getattr(completion, "cost_usd", 0.0))
        return UsageMetrics(
            requests=self.requests + 1,
            cache_hits=self.cache_hits + int(cached),
            prompt_tokens=self.prompt_tokens + int(getattr(completion, "prompt_tokens", 0)),
            completion_tokens=(
                self.completion_tokens + int(getattr(completion, "completion_tokens", 0))
            ),
            estimated_cost_usd=self.estimated_cost_usd + cost,
            billable_cost_usd=self.billable_cost_usd + (0.0 if cached else cost),
        )

    def merge(self, other: UsageMetrics) -> UsageMetrics:
        """Return the sum of two usage reports."""

        return UsageMetrics(
            requests=self.requests + other.requests,
            cache_hits=self.cache_hits + other.cache_hits,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            estimated_cost_usd=self.estimated_cost_usd + other.estimated_cost_usd,
            billable_cost_usd=self.billable_cost_usd + other.billable_cost_usd,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "billable_cost_usd": round(self.billable_cost_usd, 6),
        }
