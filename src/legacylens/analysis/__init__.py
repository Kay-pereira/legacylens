"""Static analysis over IR facts."""

from .graph import (
    GraphSummary,
    NodeRank,
    RelationshipGraphs,
    UnresolvedEdge,
    build_graphs,
)
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
    "GraphSummary",
    "Inventory",
    "LanguageInventory",
    "MetricsReport",
    "NodeRank",
    "RelationshipGraphs",
    "UnresolvedEdge",
    "build_graphs",
    "build_inventory",
    "build_metrics",
    "collect_function_metrics",
]
