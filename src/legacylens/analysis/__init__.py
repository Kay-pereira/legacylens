"""Static analysis over IR facts."""

from .crud import (
    CrudMatrix,
    CrudOperation,
    TableCrud,
    build_crud,
    build_crud_from_records,
)
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
from .sql_extract import (
    SqlStatement,
    build_data_access,
    detect_driver,
    looks_like_sql,
    parse_sql,
    split_sql_statements,
)

__all__ = [
    "ClassKindCount",
    "CrudMatrix",
    "CrudOperation",
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
    "SqlStatement",
    "TableCrud",
    "UnresolvedEdge",
    "build_crud",
    "build_crud_from_records",
    "build_data_access",
    "build_entrypoints",
    "build_graphs",
    "build_inventory",
    "build_metrics",
    "collect_function_metrics",
    "detect_driver",
    "looks_like_sql",
    "parse_sql",
    "split_sql_statements",
]
