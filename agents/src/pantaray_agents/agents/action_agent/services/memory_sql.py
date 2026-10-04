from __future__ import annotations

import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.schema.repositories.repository import (
    JSONValue,
    RepositoryErrorKind,
    RepositoryResult,
)

DEFAULT_MEMORY_SQL_LIMIT = 100
MAX_MEMORY_SQL_LIMIT = 200
MAX_MEMORY_SQL_CELL_CHARS = 4_000
MAX_MEMORY_SQL_OUTPUT_CHARS = 30_000
# SQLite checks the progress handler only at jumps (about once per row), so a
# smaller interval stops a query with heavy per-row expressions closer to the
# deadline. At 100 a plain table scan pays about 10% for the callbacks.
MEMORY_SQL_PROGRESS_HANDLER_OPCODES = 100
# Seconds. One query runs on a worker thread; this bounds how long it holds it.
MEMORY_SQL_MAX_SECONDS = 10.0
# Bytes. Caps every string or blob SQLite builds, so a query cannot inflate a cell
# (nested hex(), replace()) to gigabytes before the cell truncation runs. Real
# stores stay well below this; a stored value above it fails even length(x).
MEMORY_SQL_MAX_VALUE_BYTES = 4 * 1024 * 1024
# SQLite materializes a whole result row, so the worst row is this many columns
# times MEMORY_SQL_MAX_VALUE_BYTES. The largest legitimate row, SELECT * over all
# seven allowed tables joined, has 136 columns.
MEMORY_SQL_MAX_COLUMNS = 200
# Bytes. The progress handler cannot interrupt a row's expressions between jumps,
# so this caps how many heavy calls one statement can chain and keeps that overrun
# finite. Queries the model writes are far shorter.
MEMORY_SQL_MAX_SQL_BYTES = 20_000
_SQL_IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

MEMORY_SQL_ALLOWED_TABLES = frozenset(
    {
        "activity_logs",
        "activity_summaries",
        "agent_actions",
        "agent_facts",
        "agent_insights",
        "agent_suggestions",
        "source_records",
    }
)

_MEMORY_SQL_USER_SCOPED_TABLES = frozenset(
    {
        "activity_logs",
        "activity_summaries",
        "agent_actions",
        "agent_facts",
        "agent_insights",
        "agent_suggestions",
        "source_records",
    }
)

MEMORY_SQL_BLOCKED_FUNCTIONS = frozenset(
    {
        "format",
        "load_extension",
        "printf",
        "randomblob",
        "sqlite_compileoption_get",
        "sqlite_compileoption_used",
        "sqlite_offset",
        "zeroblob",
    }
)

_ALLOWED_AUTHORIZE_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
    }
)


class MemorySqlPayload(TypedDict):
    columns: list[str]
    rows: list[dict[str, JSONValue]]
    row_count: int
    truncated: bool
    notes: list[str]


class _SerializedRows(TypedDict):
    rows: list[dict[str, JSONValue]]
    truncated: bool


class _ValidatedSql(TypedDict):
    sql: str
    referenced_tables: frozenset[str]


class _ScopedViewPlan(TypedDict):
    public_tables: frozenset[str]
    private_views: frozenset[str]
    private_view_base_tables: dict[str, frozenset[str]]


def run_local_memory_sql(
    *,
    user_id: str,
    sql: str,
    limit: int,
) -> RepositoryResult[MemorySqlPayload]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        sql=sql,
        limit=limit,
    )


def execute_memory_sql(
    *,
    db_path: str,
    busy_timeout_ms: int,
    user_id: str,
    sql: str,
    limit: int,
) -> RepositoryResult[MemorySqlPayload]:
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        return _validation_error("memory_sql: user_id is required.")
    normalized_sql = sql.strip()
    validated_sql = _validate_sql(normalized_sql)
    if isinstance(validated_sql, str):
        return _validation_error(validated_sql)
    normalized_limit = _normalize_limit(limit)

    notes: list[str] = []
    timed_out = False
    try:
        with _connect_read_only(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as conn:
            scoped_view_plan = _install_user_scoped_temp_views(
                conn=conn,
                user_id=normalized_user_id,
                table_names=validated_sql["referenced_tables"],
            )
            deadline = time.monotonic() + MEMORY_SQL_MAX_SECONDS

            def _progress_handler() -> int:
                nonlocal timed_out
                timed_out = time.monotonic() > deadline
                return int(timed_out)

            observed_base_reads: set[str] = set()
            conn.set_authorizer(
                _build_memory_sql_authorizer(
                    scoped_view_plan=scoped_view_plan,
                    observed_base_reads=observed_base_reads,
                )
            )
            conn.set_progress_handler(
                _progress_handler,
                MEMORY_SQL_PROGRESS_HANDLER_OPCODES,
            )
            cursor = conn.execute(normalized_sql)
            columns = [description[0] for description in cursor.description or ()]
            if not observed_base_reads:
                return _validation_error(
                    "memory_sql: query must read at least one allowed memory table."
                )
            serialized = _serialize_rows(
                cursor=cursor,
                columns=columns,
                limit=normalized_limit,
                notes=notes,
            )
    except sqlite3.DatabaseError as exc:
        if timed_out:
            return _validation_error(
                "memory_sql rejected query: it ran longer than "
                f"{MEMORY_SQL_MAX_SECONDS:g} seconds. Read fewer rows (WHERE, "
                "LIMIT) or compute less per row."
            )
        return _validation_error(f"memory_sql rejected query: {exc}")

    rows = serialized["rows"]
    return RepositoryResult(
        data={
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": serialized["truncated"],
            "notes": notes,
        }
    )


def _connect_read_only(*, db_path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MEMORY_SQL_MAX_VALUE_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MEMORY_SQL_MAX_COLUMNS)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MEMORY_SQL_MAX_SQL_BYTES)
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.row_factory = sqlite3.Row
    return conn


def _install_user_scoped_temp_views(
    *,
    conn: sqlite3.Connection,
    user_id: str,
    table_names: frozenset[str],
) -> _ScopedViewPlan:
    user_id_literal = _quote_sql_literal(user_id)
    # The authorizer lets a main table be read only from inside its private view,
    # identified by the innermost view name SQLite reports. A CTE name is reported
    # the same way, so the name must be one the query cannot guess.
    private_view_nonce = secrets.token_hex(16)
    private_view_base_tables: dict[str, frozenset[str]] = {}
    for table_name in sorted(table_names & _MEMORY_SQL_USER_SCOPED_TABLES):
        private_view_name = f"memory_sql_{private_view_nonce}_{table_name}"
        private_view_base_tables[private_view_name] = frozenset({table_name})
        conn.execute(
            f"""
            CREATE TEMP VIEW {_quote_identifier(private_view_name)} AS
            SELECT *
            FROM main.{_quote_identifier(table_name)}
            WHERE user_id = {user_id_literal}
            """
        )
        _install_public_scoped_view(
            conn=conn,
            public_table_name=table_name,
            private_view_name=private_view_name,
        )
    return {
        "public_tables": table_names,
        "private_views": frozenset(private_view_base_tables),
        "private_view_base_tables": private_view_base_tables,
    }


def _install_public_scoped_view(
    *,
    conn: sqlite3.Connection,
    public_table_name: str,
    private_view_name: str,
) -> None:
    conn.execute(
        f"""
        CREATE TEMP VIEW {_quote_identifier(public_table_name)} AS
        SELECT *
        FROM {_quote_identifier(private_view_name)}
        """
    )


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _quote_sql_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _validate_sql(sql: str) -> _ValidatedSql | str:
    if not sql:
        return "memory_sql: sql is required."
    if "\x00" in sql:
        return "memory_sql: sql must not contain NUL bytes."
    normalized = sql.strip()
    stripped_statement = _strip_optional_trailing_semicolon(normalized)
    if stripped_statement is None:
        return "memory_sql: only a single SELECT statement is allowed."
    normalized = stripped_statement
    masked_sql = _mask_sql_literals_and_comments(normalized)
    first_token_match = _SQL_IDENTIFIER_PATTERN.search(masked_sql)
    if first_token_match is None:
        return "memory_sql: sql is required."
    first_token = first_token_match.group(0).casefold()
    if first_token not in {"select", "with"}:
        return "memory_sql: only SELECT or WITH ... SELECT is allowed."
    referenced_tables = _extract_referenced_memory_tables(masked_sql)
    if not referenced_tables:
        return "memory_sql: query must reference at least one allowed memory table."
    return {"sql": normalized, "referenced_tables": referenced_tables}


def _strip_optional_trailing_semicolon(sql: str) -> str | None:
    masked_sql = _mask_sql_literals_and_comments(sql)
    statement_end = len(masked_sql.rstrip())
    if statement_end == 0:
        return ""
    if masked_sql[statement_end - 1] == ";":
        sql = f"{sql[: statement_end - 1]}{sql[statement_end:]}".strip()
        masked_sql = _mask_sql_literals_and_comments(sql)
    if ";" in masked_sql:
        return None
    return sql.strip()


def _mask_sql_literals_and_comments(sql: str) -> str:
    characters = list(sql)
    index = 0
    while index < len(characters):
        current = characters[index]
        following = characters[index + 1] if index + 1 < len(characters) else ""
        if current == "'":
            index = _mask_quoted_sql(sql=sql, characters=characters, index=index)
            continue
        if current == '"':
            index = _mask_quoted_sql(sql=sql, characters=characters, index=index)
            continue
        if current == "-" and following == "-":
            index = _mask_line_comment(characters=characters, index=index)
            continue
        if current == "/" and following == "*":
            index = _mask_block_comment(sql=sql, characters=characters, index=index)
            continue
        index += 1
    return "".join(characters)


def _mask_quoted_sql(*, sql: str, characters: list[str], index: int) -> int:
    quote = characters[index]
    characters[index] = " "
    index += 1
    while index < len(characters):
        characters[index] = " "
        if sql[index] == quote:
            if index + 1 < len(characters) and sql[index + 1] == quote:
                characters[index + 1] = " "
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _mask_line_comment(*, characters: list[str], index: int) -> int:
    while index < len(characters) and characters[index] not in {"\n", "\r"}:
        characters[index] = " "
        index += 1
    return index


def _mask_block_comment(*, sql: str, characters: list[str], index: int) -> int:
    characters[index] = " "
    characters[index + 1] = " "
    index += 2
    while index < len(characters):
        if sql[index] == "*" and index + 1 < len(characters) and sql[index + 1] == "/":
            characters[index] = " "
            characters[index + 1] = " "
            return index + 2
        characters[index] = " "
        index += 1
    return index


def _extract_sql_identifiers(sql: str) -> frozenset[str]:
    return frozenset(
        match.group(0).casefold() for match in _SQL_IDENTIFIER_PATTERN.finditer(sql)
    )


def _extract_referenced_memory_tables(sql: str) -> frozenset[str]:
    identifiers = _extract_sql_identifiers(sql)
    return frozenset(
        table_name
        for table_name in MEMORY_SQL_ALLOWED_TABLES
        if table_name.casefold() in identifiers
    )


def _normalize_limit(limit: int) -> int:
    if limit <= 0:
        return DEFAULT_MEMORY_SQL_LIMIT
    return min(limit, MAX_MEMORY_SQL_LIMIT)


def _build_memory_sql_authorizer(
    *,
    scoped_view_plan: _ScopedViewPlan,
    observed_base_reads: set[str],
) -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    public_tables = scoped_view_plan["public_tables"]
    private_views = scoped_view_plan["private_views"]
    private_view_base_tables = scoped_view_plan["private_view_base_tables"]
    allowed_temp_objects = public_tables | private_views

    def _authorize_memory_sql(
        action_code: int,
        arg1: str | None,
        arg2: str | None,
        database_name: str | None,
        trigger_or_view_name: str | None,
    ) -> int:
        if action_code not in _ALLOWED_AUTHORIZE_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action_code == sqlite3.SQLITE_READ:
            if database_name == "temp" and arg1 in allowed_temp_objects:
                return sqlite3.SQLITE_OK
            if database_name == "main" and _is_private_view_base_read(
                table_name=arg1,
                trigger_or_view_name=trigger_or_view_name,
                private_view_base_tables=private_view_base_tables,
            ):
                if arg1 is not None:
                    observed_base_reads.add(arg1)
                return sqlite3.SQLITE_OK
            if database_name is None and arg1 in allowed_temp_objects and arg2 == "":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if (
            action_code == sqlite3.SQLITE_FUNCTION
            and (arg2 or "").casefold() in MEMORY_SQL_BLOCKED_FUNCTIONS
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return _authorize_memory_sql


def _is_private_view_base_read(
    *,
    table_name: str | None,
    trigger_or_view_name: str | None,
    private_view_base_tables: dict[str, frozenset[str]],
) -> bool:
    if table_name is None or trigger_or_view_name is None:
        return False
    return table_name in private_view_base_tables.get(trigger_or_view_name, frozenset())


def _serialize_rows(
    *,
    cursor: sqlite3.Cursor,
    columns: list[str],
    limit: int,
    notes: list[str],
) -> _SerializedRows:
    # Rows are serialized as they are fetched so only one raw row is held at a time.
    rows: list[dict[str, JSONValue]] = []
    output_chars = 0
    truncated = False
    for raw_row in cursor:
        if len(rows) == limit:
            return {"rows": rows, "truncated": True}
        row: dict[str, JSONValue] = {}
        for column in columns:
            value, cell_truncated = _serialize_value(raw_row[column])
            if cell_truncated:
                truncated = True
            output_chars += len(str(value))
            if output_chars > MAX_MEMORY_SQL_OUTPUT_CHARS:
                notes.append(
                    "memory_sql output was truncated by total character limit."
                )
                return {"rows": rows, "truncated": True}
            row[column] = value
        rows.append(row)
    return {"rows": rows, "truncated": truncated}


def _serialize_value(value: object) -> tuple[JSONValue, bool]:
    if value is None or isinstance(value, bool | int | float):
        return value, False
    if isinstance(value, bytes):
        return f"<bytes {len(value)} bytes>", False
    text = str(value)
    if len(text) <= MAX_MEMORY_SQL_CELL_CHARS:
        return text, False
    return f"{text[: MAX_MEMORY_SQL_CELL_CHARS - 3]}...", True


def _validation_error(message: str) -> RepositoryResult[MemorySqlPayload]:
    return RepositoryResult(
        error=message,
        error_kind=RepositoryErrorKind.VALIDATION,
        retryable=False,
    )


__all__ = [
    "DEFAULT_MEMORY_SQL_LIMIT",
    "MAX_MEMORY_SQL_LIMIT",
    "MEMORY_SQL_ALLOWED_TABLES",
    "MEMORY_SQL_BLOCKED_FUNCTIONS",
    "MemorySqlPayload",
    "execute_memory_sql",
    "run_local_memory_sql",
]
