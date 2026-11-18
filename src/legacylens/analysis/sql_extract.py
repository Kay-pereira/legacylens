"""SQL extraction with SQLGlot.

Parsers call :func:`build_data_access` while they still have the syntax tree and the source
bytes, so every SQL string is attributed to the innermost function that contains it. This
module owns the language-independent part: splitting multi-statement strings, asking
SQLGlot to classify each statement, normalising table names and placeholders, and falling
back to conservative regexes when SQLGlot does not understand a statement.

The fallback never invents a table: a statement that cannot be parsed keeps its operation
(from the leading keyword) and only tables found by an explicit ``FROM``/``INTO``/``UPDATE``
clause. A dynamic fragment is represented by ``<dynamic>`` and never expanded.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from legacylens.ir.models import (
    DYNAMIC_INPUT,
    Confidence,
    DataAccessRecord,
    FunctionRecord,
    Language,
    SqlOperation,
)

__all__ = [
    "SqlStatement",
    "build_data_access",
    "detect_driver",
    "looks_like_sql",
    "parse_sql",
    "split_sql_statements",
]

_DIALECTS: tuple[str | None, ...] = (None, "mysql", "postgres", "tsql")
_SQLGLOT_LOGGER = logging.getLogger("sqlglot")

_ANY_KEYWORD = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|MERGE|WITH|CREATE|ALTER|DROP|TRUNCATE|"
    r"EXEC(?:UTE)?|CALL)\b",
    re.IGNORECASE,
)
_STATEMENT_START = re.compile(
    r"^\(*\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|MERGE|WITH|CREATE|ALTER|DROP|TRUNCATE|"
    r"EXEC(?:UTE)?|CALL)\b",
    re.IGNORECASE,
)
_NAMED_PLACEHOLDER = re.compile(r"[A-Za-z_]\w*")
_DOLLAR_PLACEHOLDER = re.compile(r"\$\d+")
_PERCENT_PLACEHOLDER = re.compile(r"%\([A-Za-z_]\w*\)s|%s")
_AT_PLACEHOLDER = re.compile(r"@[A-Za-z_]\w*")
_QUOTED_TABLE = r"[`\"\[]?[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){0,2}[`\"\]]?"
_TABLE_NAME = rf"(?:{_QUOTED_TABLE})"

_REGEX_TARGETS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\bUPDATE\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bINSERT\s+(?:IGNORE\s+)?INTO\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bREPLACE\s+INTO\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bDELETE\s+FROM\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bMERGE\s+INTO\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(
        rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?"
        rf"(?:TABLE|VIEW|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?({_TABLE_NAME})",
        re.IGNORECASE,
    ),
    re.compile(rf"\bALTER\s+TABLE\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(
        rf"\bDROP\s+(?:TABLE|VIEW|INDEX)\s+(?:IF\s+EXISTS\s+)?({_TABLE_NAME})",
        re.IGNORECASE,
    ),
    re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bFROM\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bJOIN\s+({_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bUSING\s+({_TABLE_NAME})", re.IGNORECASE),
)
_SECONDARY_OPERATIONS = frozenset(
    {
        SqlOperation.INSERT,
        SqlOperation.REPLACE,
        SqlOperation.UPDATE,
        SqlOperation.DELETE,
        SqlOperation.MERGE,
        SqlOperation.DDL,
    }
)


@dataclass(frozen=True, slots=True)
class SqlStatement:
    """One statement parsed from a source string.

    ``line_offset`` is the number of newlines between the start of the source string and
    the statement, so callers can turn the string's start line into an exact line.
    """

    operation: SqlOperation
    sql: str
    tables: tuple[str, ...]
    parameters: tuple[str, ...]
    confidence: Confidence
    line_offset: int = 0


def split_sql_statements(text: str) -> tuple[tuple[str, int], ...]:
    """Split ``text`` on semicolons that are not inside strings or comments.

    Returns ``(statement, line_offset)`` pairs; empty statements are dropped. Line
    offsets count newlines before the statement's first token.
    """

    parts: list[tuple[str, int]] = []
    start = 0
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False

    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
        elif quote is not None:
            if char == "\\" and quote in {"'", '"'}:
                index += 1
            elif char == quote:
                if nxt == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "-" and nxt == "-":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char == ";":
            _append_statement(parts, text, start, index)
            start = index + 1
        index += 1

    _append_statement(parts, text, start, len(text))
    return tuple(parts)


def _append_statement(parts: list[tuple[str, int]], text: str, start: int, end: int) -> None:
    chunk = text[start:end]
    if not chunk.strip():
        return
    leading = chunk[: len(chunk) - len(chunk.lstrip())]
    parts.append((chunk, text[:start].count("\n") + leading.count("\n")))


def looks_like_sql(statement: str) -> bool:
    """Whether a single statement is SQL rather than prose that starts with a keyword.

    SQLGlot is permissive enough to parse ``Select a user from the list`` as a query, so
    the shape check runs first. Statements with strong structure (``WHERE``, ``*``, a
    comma-separated select list, ``INSERT INTO``, ``UPDATE ... SET`` and so on) pass;
    ambiguous prose does not.
    """

    text = statement.strip()
    if not text:
        return False
    match = _STATEMENT_START.match(text)
    if match is None:
        return False

    keyword = match.group(1).upper()
    if keyword in {"EXEC", "EXECUTE"}:
        keyword = "EXEC"
    if keyword == "SELECT":
        return _looks_like_select(text)
    if keyword == "INSERT":
        return re.search(r"\bINSERT\s+(?:IGNORE\s+)?INTO\b", text, re.IGNORECASE) is not None
    if keyword == "REPLACE":
        return re.search(r"\bREPLACE\s+INTO\b", text, re.IGNORECASE) is not None
    if keyword == "UPDATE":
        return re.search(r"\bUPDATE\s+\S+\s+SET\b", text, re.IGNORECASE) is not None
    if keyword == "DELETE":
        return re.search(r"\bDELETE\s+FROM\b", text, re.IGNORECASE) is not None
    if keyword == "MERGE":
        return re.search(r"\bMERGE\s+INTO\b", text, re.IGNORECASE) is not None
    if keyword == "WITH":
        return (
            re.search(r"\bAS\b", text, re.IGNORECASE) is not None
            and re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE)\b", text[4:], re.IGNORECASE)
            is not None
        )
    if keyword == "CREATE":
        return (
            re.search(
                r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?"
                r"(?:TABLE|INDEX|VIEW|SCHEMA|DATABASE|PROCEDURE|FUNCTION|TRIGGER)\b",
                text,
                re.IGNORECASE,
            )
            is not None
        )
    if keyword == "ALTER":
        return (
            re.search(r"\bALTER\s+(?:TABLE|INDEX|VIEW|SCHEMA|DATABASE)\b", text, re.IGNORECASE)
            is not None
        )
    if keyword == "DROP":
        return (
            re.search(
                r"\bDROP\s+(?:TABLE|INDEX|VIEW|SCHEMA|DATABASE|PROCEDURE|FUNCTION|TRIGGER)\b",
                text,
                re.IGNORECASE,
            )
            is not None
        )
    if keyword == "TRUNCATE":
        return re.search(r"\bTRUNCATE\s+(?:TABLE\s+)?\w", text, re.IGNORECASE) is not None
    if keyword == "EXEC":
        return re.search(r"\bEXEC(?:UTE)?\s+[\w.`\"\[\]@]+", text, re.IGNORECASE) is not None
    if keyword == "CALL":
        return re.search(r"\bCALL\s+[\w.`\"\[\]@]+", text, re.IGNORECASE) is not None
    return False


def _looks_like_select(text: str) -> bool:
    upper = text.upper()
    from_match = re.search(r"\bFROM\b", upper)
    if from_match is None:
        return False
    if re.search(
        r"\b(?:WHERE|JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|OFFSET|FETCH)\b",
        upper,
    ):
        return True

    select_match = re.search(r"\bSELECT\b", upper)
    assert select_match is not None
    select_list = upper[select_match.end() : from_match.start()]
    select_list = re.sub(r"^\s*(?:DISTINCT|ALL)\s+", "", select_list)
    if "*" in select_list or "," in select_list:
        return True
    if re.search(
        r"\bFROM\s+(?:[`\"]|[A-Za-z_][\w$]*\.[A-Za-z_]|[A-Za-z_][\w$]*_[A-Za-z_])",
        text,
    ):
        return True

    tokens = select_list.strip().split()
    return (
        len(tokens) == 1
        and re.fullmatch(r"[A-Z_][\w$]*", tokens[0]) is not None
        and text.lstrip("(").startswith("SELECT")
    )


def parse_sql(text: str) -> tuple[SqlStatement, ...]:
    """Parse every SQL statement in ``text``.

    SQLGlot classifies a statement when it understands the syntax; otherwise the leading
    keyword and explicit table clauses classify it and the statement is ``inferred``.
    """

    if not text or _ANY_KEYWORD.search(text) is None:
        return ()

    statements: list[SqlStatement] = []
    for part, offset in split_sql_statements(text):
        statement = " ".join(part.split())
        if not looks_like_sql(statement):
            continue
        # A statement containing an unresolved fragment must not be handed to SQLGlot:
        # letting it guess would risk inventing a table or silently dropping the fragment.
        expression = None if DYNAMIC_INPUT in statement else _try_parse(statement)
        if expression is not None:
            operation = _operation_from_expression(expression)
            tables = _tables_from_expression(expression)
            confidence = Confidence.VERIFIED
        else:
            operation = _operation_from_text(statement)
            if operation is None:
                continue
            tables = _tables_from_text(statement)
            confidence = Confidence.INFERRED
        statements.append(
            SqlStatement(
                operation=operation,
                sql=statement,
                tables=tables,
                parameters=_placeholders(statement),
                confidence=confidence,
                line_offset=offset,
            )
        )
    return tuple(statements)


def _try_parse(statement: str) -> exp.Expression | None:
    previous = _SQLGLOT_LOGGER.level
    _SQLGLOT_LOGGER.setLevel(logging.ERROR)
    try:
        for dialect in _dialects_for(statement):
            try:
                expression = sqlglot.parse_one(
                    statement,
                    read=dialect,
                    error_level=sqlglot.ErrorLevel.IGNORE,
                )
            except Exception:  # SQLGlot raises several unrelated parse exceptions.
                continue
            if expression is None or isinstance(expression, exp.Command):
                continue
            if _operation_from_expression(expression) is None:
                continue
            if _has_misparsed_identifier(expression):
                continue
            return expression
    finally:
        _SQLGLOT_LOGGER.setLevel(previous)
    return None


def _dialects_for(statement: str) -> tuple[str | None, ...]:
    """Order dialects by lexical evidence so backticks and ``%s`` parse correctly."""

    if "`" in statement:
        return ("mysql", "tsql", None, "postgres")
    if re.search(r"\[[A-Za-z_]\w*\]", statement):
        return ("tsql", None, "mysql", "postgres")
    if "%s" in statement or "%(" in statement or "::" in statement:
        return ("postgres", None, "mysql", "tsql")
    return _DIALECTS


def _has_misparsed_identifier(expression: exp.Expression) -> bool:
    """Reject parses that kept a quote character in an identifier."""

    return any(
        quote in part
        for table in expression.find_all(exp.Table)
        for part in (table.catalog, table.db, table.name)
        for quote in ('"', "`", "[", "]")
    )


def _operation_from_expression(expression: exp.Expression) -> SqlOperation | None:
    if isinstance(expression, exp.Select):
        return SqlOperation.SELECT
    if isinstance(expression, exp.Insert):
        return SqlOperation.INSERT
    if isinstance(expression, exp.Replace):
        return SqlOperation.REPLACE
    if isinstance(expression, exp.Update):
        return SqlOperation.UPDATE
    if isinstance(expression, exp.Delete):
        return SqlOperation.DELETE
    if isinstance(expression, exp.Merge):
        return SqlOperation.MERGE
    if isinstance(expression, (exp.Create, exp.Alter, exp.Drop, exp.TruncateTable)):
        return SqlOperation.DDL
    return None


def _tables_from_expression(expression: exp.Expression) -> tuple[str, ...]:
    cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE) if cte.alias_or_name}
    tables: list[str] = []
    seen: set[str] = set()
    for table in expression.find_all(exp.Table):
        if table.name in cte_names:
            continue
        key = _table_key(table)
        if not key or key in seen:
            continue
        seen.add(key)
        tables.append(key)

    target = _target_table(expression)
    if target is not None and target in tables:
        tables.remove(target)
        tables.insert(0, target)
    return tuple(tables)


def _table_key(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _target_table(expression: exp.Expression) -> str | None:
    target = expression.this
    if isinstance(target, exp.Schema):
        target = target.this
    if isinstance(target, exp.Table):
        return _table_key(target)
    return None


def _operation_from_text(statement: str) -> SqlOperation | None:
    match = _STATEMENT_START.match(statement.strip())
    if match is None:
        return None
    keyword = match.group(1).upper()
    if keyword == "SELECT":
        return SqlOperation.SELECT
    if keyword == "INSERT":
        return SqlOperation.INSERT
    if keyword == "REPLACE":
        return SqlOperation.REPLACE
    if keyword == "UPDATE":
        return SqlOperation.UPDATE
    if keyword == "DELETE":
        return SqlOperation.DELETE
    if keyword == "MERGE":
        return SqlOperation.MERGE
    if keyword in {"CREATE", "ALTER", "DROP", "TRUNCATE"}:
        return SqlOperation.DDL
    if keyword in {"EXEC", "EXECUTE", "CALL"}:
        return SqlOperation.EXEC
    return SqlOperation.OTHER


def _tables_from_text(statement: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _REGEX_TARGETS:
        for match in pattern.finditer(statement):
            name = _clean_table(match.group(1))
            if not name or name in seen:
                continue
            seen.add(name)
            found.append(name)
    return tuple(found)


def _clean_table(value: str) -> str | None:
    parts = [_strip_quotes(part) for part in value.split(".")]
    if not parts or any(not part for part in parts):
        return None
    cleaned = ".".join(parts)
    if cleaned.upper() in {"SELECT", "VALUES", "SET", "WHERE", "TABLE"}:
        return None
    return cleaned


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"`", '"', "[", "]"}:
        return value[1:-1]
    return value.strip('`"[]')


def _placeholders(statement: str) -> tuple[str, ...]:
    result: list[str] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False

    while index < len(statement):
        char = statement[index]
        nxt = statement[index + 1] if index + 1 < len(statement) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
        elif quote is not None:
            if char == "\\" and quote in {"'", '"'}:
                index += 1
            elif char == quote:
                if nxt == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "-" and nxt == "-":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char == "?":
            result.append("?")
        elif char == ":":
            if nxt == ":":
                index += 1
            elif index == 0 or statement[index - 1] != ":":
                match = _NAMED_PLACEHOLDER.match(statement, index + 1)
                if match is not None:
                    result.append(":" + match.group(0))
                    index = match.end() - 1
        elif char == "$":
            match = _DOLLAR_PLACEHOLDER.match(statement, index)
            if match is not None:
                result.append(match.group(0))
                index = match.end() - 1
        elif char == "%":
            match = _PERCENT_PLACEHOLDER.match(statement, index)
            if match is not None:
                result.append(match.group(0))
                index = match.end() - 1
        elif char == "@":
            match = _AT_PLACEHOLDER.match(statement, index)
            if match is not None:
                result.append(match.group(0))
                index = match.end() - 1
        index += 1

    return tuple(result)


def build_data_access(
    *,
    file_path: str,
    snippets: Iterable[tuple[str, int]],
    functions: Iterable[FunctionRecord] = (),
    driver: str | None = None,
) -> tuple[DataAccessRecord, ...]:
    """Turn ``(text, line)`` snippets into stored data-access facts.

    A statement that references several tables produces one row per table: the first
    table keeps the statement's operation (insert/update/delete/DDL) and the remaining
    tables are reads, which keeps the CRUD matrix accurate for joins and subqueries.
    """

    spans = [(item.span.start_line, item.span.end_line, item.id) for item in functions]
    records: list[DataAccessRecord] = []
    for text, line in snippets:
        for statement in parse_sql(text):
            statement_line = line + statement.line_offset
            function_id = _innermost_function(statement_line, spans)
            if not statement.tables:
                records.append(
                    DataAccessRecord(
                        file_path=file_path,
                        operation=statement.operation,
                        sql=statement.sql,
                        line=statement_line,
                        function_id=function_id,
                        table_name=None,
                        driver=driver,
                        parameters=statement.parameters,
                        confidence=statement.confidence,
                    )
                )
                continue
            for position, table in enumerate(statement.tables):
                operation = (
                    statement.operation
                    if position == 0
                    else _secondary_operation(statement.operation)
                )
                records.append(
                    DataAccessRecord(
                        file_path=file_path,
                        operation=operation,
                        sql=statement.sql,
                        line=statement_line,
                        function_id=function_id,
                        table_name=table,
                        driver=driver,
                        parameters=statement.parameters,
                        confidence=statement.confidence,
                    )
                )

    records.sort(
        key=lambda item: (
            item.line,
            item.table_name or "",
            item.operation.value,
            item.sql,
        )
    )
    return tuple(records)


def _secondary_operation(operation: SqlOperation) -> SqlOperation:
    return SqlOperation.SELECT if operation in _SECONDARY_OPERATIONS else operation


def _innermost_function(line: int, spans: list[tuple[int, int, str]]) -> str | None:
    candidates = [
        (end - start, start, function_id)
        for start, end, function_id in spans
        if start <= line <= end
    ]
    if not candidates:
        return None
    return min(candidates)[2]


def detect_driver(language: Language, text: str) -> str | None:
    """Return the database driver when the file itself provides evidence.

    No driver is guessed from how a statement looks. PHP needs an explicit ``PDO``/
    ``mysqli``/``wpdb`` marker and Java needs a ``java.sql`` reference.
    """

    if language is Language.PHP:
        if re.search(r"\bPDO\b", text):
            return "PDO"
        if re.search(r"\bmysqli\b|mysqli_", text):
            return "mysqli"
        if re.search(r"\$wpdb\b", text):
            return "wpdb"
        return None
    if language is Language.JAVA and re.search(r"\bjava\.sql\b", text):
        return "JDBC"
    return None
