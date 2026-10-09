from __future__ import annotations

import json
import sqlite3
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from pantaray_agents.schema.agent.base import JSONValue

from .specs import MigrationError
from .sql_script import execute_sql_statements


class _LegacyReadFileOutput(BaseModel):
    """Read output persisted before the total_lines contract was introduced."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["file"]
    path: str
    content: str
    offset: int
    end_line: int
    line_count: int | None = None
    truncated: bool
    next_offset: int | None = None


class _PreCursorReadFileOutput(BaseModel):
    """Read output persisted before line-column continuation was introduced."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["file"]
    path: str
    content: str
    offset: int
    end_line: int
    total_lines: int | None
    next_offset: int | None = None
    truncated: bool
    truncation_reason: Literal["line_count_budget", "page_limit"] | None = None
    retry_hint: str | None = None


def apply_action_file_read_memory_migration(
    connection: sqlite3.Connection,
    *,
    migration_statements: tuple[str, ...],
) -> None:
    execute_sql_statements(connection, migration_statements)
    # Delay the application-layer import until migration modules are initialized.
    from pantaray_agents.local_runtime.memory_catalog.errors import MemoryCatalogError
    from pantaray_agents.local_runtime.tooling.action_file_read_memory import (
        build_action_file_read_memory_input,
        register_action_file_read_memory,
    )

    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT
            invocations.invocation_id,
            invocations.user_id,
            invocations.tool_id,
            invocations.status,
            invocations.completed_at,
            outputs.output_json
        FROM tool_invocations AS invocations
        JOIN tool_outputs AS outputs
          ON outputs.invocation_id = invocations.invocation_id
        WHERE invocations.tool_id = 'read'
          AND invocations.status = 'completed'
          AND invocations.completed_at IS NOT NULL
          AND outputs.redaction_applied = 0
          AND json_extract(outputs.output_json, '$.kind') = 'file'
        ORDER BY invocations.completed_at, invocations.invocation_id
        """
    )
    for row in rows:
        invocation_id = str(row["invocation_id"])
        try:
            output_json = cast(JSONValue, json.loads(str(row["output_json"])))
            normalized_output = _normalize_historical_read_output(output_json)
            register_action_file_read_memory(
                connection=connection,
                invocation_id=invocation_id,
                user_id=str(row["user_id"]),
                completed_at=str(row["completed_at"]),
                read_memory=build_action_file_read_memory_input(
                    tool_id=str(row["tool_id"]),
                    status="completed",
                    output=normalized_output,
                ),
                redaction_applied=False,
            )
        except (json.JSONDecodeError, ValidationError, MemoryCatalogError) as exc:
            raise MigrationError(
                f"Failed to backfill historical read output for {invocation_id}"
            ) from exc


def _normalize_historical_read_output(output_json: JSONValue) -> JSONValue:
    if not isinstance(output_json, dict) or "encoding" in output_json:
        return output_json
    # Every read stored before the encoding field decoded its file as UTF-8.
    return {**_with_cursor(output_json), "encoding": "utf-8"}


def _with_cursor(output_json: dict[str, JSONValue]) -> dict[str, JSONValue]:
    if "column" in output_json:
        return output_json
    if "total_lines" not in output_json:
        legacy = _LegacyReadFileOutput.model_validate(output_json)
        output_json = {
            "kind": legacy.kind,
            "path": legacy.path,
            "content": legacy.content,
            "offset": legacy.offset,
            "end_line": legacy.end_line,
            "total_lines": legacy.line_count,
            "next_offset": legacy.next_offset,
            "truncated": legacy.truncated,
            "truncation_reason": None,
            "retry_hint": None,
        }
    pre_cursor = _PreCursorReadFileOutput.model_validate(output_json)
    last_line = pre_cursor.content.splitlines()[-1] if pre_cursor.content else ""
    return {
        "kind": pre_cursor.kind,
        "path": pre_cursor.path,
        "content": pre_cursor.content,
        "offset": pre_cursor.offset,
        "column": 1,
        "end_line": pre_cursor.end_line,
        "end_column": len(last_line),
        "total_lines": pre_cursor.total_lines,
        "next_offset": pre_cursor.next_offset,
        "next_column": 1 if pre_cursor.next_offset is not None else None,
        "truncated": pre_cursor.truncated,
        "truncation_reason": pre_cursor.truncation_reason,
        "retry_hint": pre_cursor.retry_hint,
    }
