"""Static analysis over IR facts."""

from .inventory import (
    ClassKindCount,
    Inventory,
    LanguageInventory,
    build_inventory,
)
from .metrics import (
    Distribution,
    FunctionMetric,
    MetricsReport,
    build_metrics,
    collect_function_metrics,
)

__all__ = [
    "ClassKindCount",
    "Distribution",
    "FunctionMetric",
    "Inventory",
    "LanguageInventory",
    "MetricsReport",
    "build_inventory",
    "build_metrics",
    "collect_function_metrics",
]
