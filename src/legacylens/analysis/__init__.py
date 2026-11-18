"""Static analysis over IR facts."""

from .entrypoints import (
    EntryPointDetail,
    EntryPointsReport,
    build_entrypoints,
)
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
    "EntryPointDetail",
    "EntryPointsReport",
    "FunctionMetric",
    "GraphSummary",
    "Inventory",
    "LanguageInventory",
    "MetricsReport",
    "NodeRank",
    "RelationshipGraphs",
    "UnresolvedEdge",
    "build_entrypoints",
    "build_graphs",
    "build_inventory",
    "build_metrics",
    "collect_function_metrics",
]
