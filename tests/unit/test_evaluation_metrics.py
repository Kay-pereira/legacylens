"""Evaluation metrics: set matching, normalisation and usage accounting."""

from __future__ import annotations

from types import SimpleNamespace

from legacylenscli.evaluation import (
    ClaimMetrics,
    UsageMetrics,
    business_rule_keys,
    compare_sets,
    entry_point_key,
    normalise_identifier,
    table_operation_key,
    table_operation_keys,
)
from legacylenscli.ir import EntryKind, EntryPointRecord, HttpMethod, SqlOperation


def test_compare_sets_reports_matches_misses_and_extras() -> None:
    metrics = compare_sets({"a", "b"}, {"b", "c"})

    assert metrics.expected == 2
    assert metrics.actual == 2
    assert metrics.matched == ("b",)
    assert metrics.missing == ("a",)
    assert metrics.unexpected == ("c",)
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5


def test_compare_sets_empty_sides_do_not_divide_by_zero() -> None:
    empty = compare_sets((), ())
    actual_only = compare_sets((), ("x",))
    expected_only = compare_sets(("x",), ())

    assert (empty.precision, empty.recall, empty.f1) == (0.0, 0.0, 0.0)
    assert (actual_only.precision, actual_only.recall) == (0.0, 0.0)
    assert (expected_only.precision, expected_only.recall) == (0.0, 0.0)


def test_entry_point_keys_include_method_and_collapse_duplicates() -> None:
    method = EntryPointRecord(
        file_path="src/User.java",
        kind=EntryKind.SERVLET,
        path="/users/*",
        line=1,
        http_method=HttpMethod.GET,
    )
    plain = EntryPointRecord(
        file_path="public/index.php",
        kind=EntryKind.ROUTE,
        path="/index.php",
        line=1,
    )

    assert entry_point_key(method) == "servlet:GET /users/*"
    assert entry_point_key(plain) == "route:/index.php"
    assert normalise_identifier(" a\t b \n") == "a b"


def test_table_operation_keys_compare_pairs_case_insensitively() -> None:
    matrix = SimpleNamespace(
        tables=(
            SimpleNamespace(
                table_name="Users",
                operations=(
                    SimpleNamespace(operation=SqlOperation.SELECT),
                    SimpleNamespace(operation=SqlOperation.INSERT),
                ),
            ),
        )
    )

    assert table_operation_keys(matrix) == {"users:SELECT", "users:INSERT"}
    assert table_operation_key(" Users ", "update") == "users:UPDATE"


def test_business_rule_keys_use_normalised_evidence() -> None:
    candidates = (
        SimpleNamespace(evidence="  filter_var($email,\n FILTER_VALIDATE_EMAIL) "),
        SimpleNamespace(evidence="isset($_POST['role'])"),
    )

    assert business_rule_keys(candidates) == {
        "filter_var($email, FILTER_VALIDATE_EMAIL)",
        "isset($_POST['role'])",
    }


def test_usage_metrics_separate_estimated_and_billable_cost() -> None:
    fresh = SimpleNamespace(
        cached=False,
        cost_usd=0.002,
        prompt_tokens=100,
        completion_tokens=20,
    )
    cached = SimpleNamespace(
        cached=True,
        cost_usd=0.002,
        prompt_tokens=100,
        completion_tokens=20,
    )

    usage = UsageMetrics().with_completion(fresh).with_completion(cached)

    assert usage.requests == 2
    assert usage.cache_hits == 1
    assert usage.cache_misses == 1
    assert usage.cache_hit_rate == 0.5
    assert usage.total_tokens == 240
    assert usage.estimated_cost_usd == 0.004
    assert usage.billable_cost_usd == 0.002
    assert UsageMetrics().merge(usage) == usage
    assert usage.as_dict()["cache_hit_rate"] == 0.5


def test_claim_metrics_reports_null_rates_when_unverified() -> None:
    metrics = ClaimMetrics(generated=2, inferred=2, publishable=2)

    assert metrics.as_dict() == {
        "generated": 2,
        "counts": {"verified": 0, "inferred": 2, "unsupported": 0},
        "publishable": 2,
        "verification_rate": None,
        "verified_rate": None,
    }
