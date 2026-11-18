"""CRUD coverage derived from stored data-access facts.

``data_access`` rows are statement/table pairs, so this module pivots them into a stable
table -> operations -> functions matrix. It reports absent CRUD letters for each table
instead of inventing tables the repository never mentions; there is no schema snapshot to
compare against yet.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from legacylens.ir.models import DataAccessRecord, SqlOperation
from legacylens.ir.store import FactStore

__all__ = [
    "CrudMatrix",
    "CrudOperation",
    "TableCrud",
    "build_crud",
    "build_crud_from_records",
]

_CRUD_ORDER = ("R", "C", "U", "D")
_OPERATION_ORDER = {
    SqlOperation.SELECT: 0,
    SqlOperation.INSERT: 1,
    SqlOperation.REPLACE: 2,
    SqlOperation.UPDATE: 3,
    SqlOperation.DELETE: 4,
    SqlOperation.MERGE: 5,
    SqlOperation.DDL: 6,
    SqlOperation.EXEC: 7,
    SqlOperation.OTHER: 8,
}


@dataclass(frozen=True, slots=True)
class CrudOperation:
    """One operation observed against one table."""

    operation: SqlOperation
    letter: str | None
    records: int
    functions: tuple[str, ...]
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "letter": self.letter,
            "records": self.records,
            "functions": list(self.functions),
            "files": list(self.files),
        }


@dataclass(frozen=True, slots=True)
class TableCrud:
    """CRUD coverage for one table."""

    table_name: str
    crud: str
    missing: tuple[str, ...]
    operations: tuple[CrudOperation, ...]
    functions: tuple[str, ...]
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "table": self.table_name,
            "crud": self.crud,
            "missing": list(self.missing),
            "functions": list(self.functions),
            "files": list(self.files),
            "operations": [item.as_dict() for item in self.operations],
        }


@dataclass(frozen=True, slots=True)
class CrudMatrix:
    """The whole table/operation/function matrix plus rows with no table."""

    tables: tuple[TableCrud, ...]
    unknown_table: tuple[DataAccessRecord, ...]
    records: int
    script_records: int

    def as_dict(self) -> dict[str, object]:
        read_only = [item.table_name for item in self.tables if item.crud == "R"]
        write_only = [item.table_name for item in self.tables if item.crud and "R" not in item.crud]
        return {
            "records": self.records,
            "script_records": self.script_records,
            "tables": [item.as_dict() for item in self.tables],
            "unknown_table": [_record_dict(item) for item in self.unknown_table],
            "summary": {
                "tables": len(self.tables),
                "read_only": read_only,
                "write_only": write_only,
                "complete": [
                    item.table_name for item in self.tables if item.crud and not item.missing
                ],
                "missing_operations": {
                    item.table_name: list(item.missing) for item in self.tables if item.missing
                },
            },
        }


def build_crud(store: FactStore) -> CrudMatrix:
    """Build the CRUD matrix from every stored ``data_access`` row."""

    return build_crud_from_records(store.list_data_access())


def build_crud_from_records(records: Iterable[DataAccessRecord]) -> CrudMatrix:
    """Build the CRUD matrix from an existing record sequence."""

    materialised = tuple(records)
    by_table: dict[str, list[DataAccessRecord]] = defaultdict(list)
    unknown: list[DataAccessRecord] = []
    for record in materialised:
        if record.table_name:
            by_table[record.table_name].append(record)
        else:
            unknown.append(record)

    tables = [_table_crud(table_name, by_table[table_name]) for table_name in sorted(by_table)]
    unknown.sort(
        key=lambda item: (
            item.file_path,
            item.line,
            item.operation.value,
            item.sql,
        )
    )
    return CrudMatrix(
        tables=tuple(tables),
        unknown_table=tuple(unknown),
        records=len(materialised),
        script_records=sum(1 for item in materialised if item.function_id is None),
    )


def _table_crud(table_name: str, records: list[DataAccessRecord]) -> TableCrud:
    grouped: dict[SqlOperation, list[DataAccessRecord]] = defaultdict(list)
    for record in records:
        grouped[record.operation].append(record)

    operations = [
        CrudOperation(
            operation=operation,
            letter=operation.crud,
            records=len(grouped[operation]),
            functions=tuple(
                sorted(
                    {
                        item.function_id
                        for item in grouped[operation]
                        if item.function_id is not None
                    }
                )
            ),
            files=tuple(sorted({item.file_path for item in grouped[operation]})),
        )
        for operation in sorted(grouped, key=lambda item: _OPERATION_ORDER[item])
    ]
    letters = {item.letter for item in operations if item.letter is not None}
    crud = "".join(letter for letter in _CRUD_ORDER if letter in letters)
    missing = tuple(letter for letter in _CRUD_ORDER if letter not in letters) if crud else ()
    return TableCrud(
        table_name=table_name,
        crud=crud,
        missing=missing,
        operations=tuple(operations),
        functions=tuple(
            sorted({item.function_id for item in records if item.function_id is not None})
        ),
        files=tuple(sorted({item.file_path for item in records})),
    )


def _record_dict(record: DataAccessRecord) -> dict[str, object]:
    return {
        "file_path": record.file_path,
        "line": record.line,
        "operation": record.operation.value,
        "sql": record.sql,
        "parameters": list(record.parameters),
        "function_id": record.function_id,
        "driver": record.driver,
        "confidence": record.confidence.value,
    }
