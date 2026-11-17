"""Analysis reports: distributions, empty-store behaviour and JSON shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacylens.analysis import Distribution, build_inventory, build_metrics
from legacylens.ir import FactStore


def test_distribution_summarises_values_with_nearest_rank_p95() -> None:
    distribution = Distribution.from_values([4, 1, 3, 2, 100])

    assert distribution.count == 5
    assert distribution.total == 110
    assert distribution.minimum == 1
    assert distribution.maximum == 100
    assert distribution.mean == 22.0
    assert distribution.median == 3.0
    assert distribution.p95 == 100


def test_empty_distribution_is_zero_filled() -> None:
    distribution = Distribution.from_values([])

    assert distribution.as_dict() == {
        "count": 0,
        "total": 0,
        "min": 0,
        "max": 0,
        "mean": 0.0,
        "median": 0.0,
        "p95": 0,
    }


def test_empty_store_reports_zero_inventory_and_metrics(tmp_path: Path) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        inventory = build_inventory(store)
        metrics = build_metrics(store)

    assert inventory.files == 0
    assert inventory.classes == 0
    assert inventory.functions == 0
    assert inventory.languages == ()
    assert inventory.class_kinds == ()
    assert metrics.function_count == 0
    assert metrics.length.count == 0
    assert metrics.longest == ()
    assert metrics.most_complex == ()
    assert metrics.deepest == ()


def test_top_n_cannot_be_negative(tmp_path: Path) -> None:
    with FactStore(tmp_path / "facts.db") as store:
        with pytest.raises(ValueError, match="top_n"):
            build_metrics(store, top_n=-1)
