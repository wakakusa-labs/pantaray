from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...storage.migrations import MigrationError
from .common import _configure_connection

# How a command ended.
TerminalOutcome = Literal[
    "exited",
    "signaled",
    "timed_out",
    "canceled",
    "sandbox_violation",
    "spawn_failed",
    "budget_exceeded",
    "broker_failed",
]
# What an audit row records: a command still running after its call returned
# has no end yet.
CommandAuditOutcome = TerminalOutcome | Literal["running"]

BudgetExceededKind = Literal[
    "stdout_limit",
    "stderr_limit",
    "temp_storage_limit",
    "child_count_limit",
    "open_file_lease_limit",
]

SandboxViolationKind = Literal[
    "path_escape",
    "write_denied",
    "exec_denied",
    "network_denied",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class CommandInvocationAuditUpsertInput:
    invocation_id: str
    approval_session_id: str | None
    execution_kind: str
    executable_source_kind: str
    resolved_executable_path: str
    terminal_outcome: CommandAuditOutcome
    exit_code: int | None
    signal: int | None
    stdout_bytes: int
    stderr_bytes: int
    stdout_max_bytes: int
    stderr_max_bytes: int
    temp_storage_limit_bytes: int
    child_count_limit: int
    open_file_lease_limit: int
    budget_exceeded_kind: BudgetExceededKind | None
    sandbox_violation_kind: SandboxViolationKind | None
    sandbox_violation_summary: str | None
    created_at: str
    updated_at: str


def upsert_command_invocation_audit(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    audit: CommandInvocationAuditUpsertInput,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            invocation_row = connection.execute(
                "SELECT invocation_id FROM tool_invocations WHERE invocation_id = ?",
                (audit.invocation_id,),
            ).fetchone()
            if invocation_row is None:
                raise MigrationError(
                    "command invocation audit requires an existing tool invocation"
                )
            if audit.approval_session_id is not None:
                approval_row = connection.execute(
                    """
                    SELECT approval_session_id
                    FROM approval_sessions
                    WHERE approval_session_id = ?
                    """,
                    (audit.approval_session_id,),
                ).fetchone()
                if approval_row is None:
                    raise MigrationError(
                        "command invocation audit approval_session_id must reference "
                        "an existing approval session"
                    )
            connection.execute(
                """
                INSERT INTO command_invocation_audits(
                    invocation_id,
                    approval_session_id,
                    execution_kind,
                    executable_source_kind,
                    resolved_executable_path,
                    terminal_outcome,
                    exit_code,
                    signal,
                    stdout_bytes,
                    stderr_bytes,
                    stdout_max_bytes,
                    stderr_max_bytes,
                    temp_storage_limit_bytes,
                    child_count_limit,
                    open_file_lease_limit,
                    budget_exceeded_kind,
                    sandbox_violation_kind,
                    sandbox_violation_summary,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    approval_session_id = excluded.approval_session_id,
                    execution_kind = excluded.execution_kind,
                    executable_source_kind = excluded.executable_source_kind,
                    resolved_executable_path = excluded.resolved_executable_path,
                    terminal_outcome = excluded.terminal_outcome,
                    exit_code = excluded.exit_code,
                    signal = excluded.signal,
                    stdout_bytes = excluded.stdout_bytes,
                    stderr_bytes = excluded.stderr_bytes,
                    stdout_max_bytes = excluded.stdout_max_bytes,
                    stderr_max_bytes = excluded.stderr_max_bytes,
                    temp_storage_limit_bytes = excluded.temp_storage_limit_bytes,
                    child_count_limit = excluded.child_count_limit,
                    open_file_lease_limit = excluded.open_file_lease_limit,
                    budget_exceeded_kind = excluded.budget_exceeded_kind,
                    sandbox_violation_kind = excluded.sandbox_violation_kind,
                    sandbox_violation_summary = excluded.sandbox_violation_summary,
                    updated_at = excluded.updated_at
                """,
                (
                    audit.invocation_id,
                    audit.approval_session_id,
                    audit.execution_kind,
                    audit.executable_source_kind,
                    audit.resolved_executable_path,
                    audit.terminal_outcome,
                    audit.exit_code,
                    audit.signal,
                    audit.stdout_bytes,
                    audit.stderr_bytes,
                    audit.stdout_max_bytes,
                    audit.stderr_max_bytes,
                    audit.temp_storage_limit_bytes,
                    audit.child_count_limit,
                    audit.open_file_lease_limit,
                    audit.budget_exceeded_kind,
                    audit.sandbox_violation_kind,
                    audit.sandbox_violation_summary,
                    audit.created_at,
                    audit.updated_at,
                ),
            )


def end_running_command_invocation_audit(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    invocation_id: str,
    terminal_outcome: TerminalOutcome,
    exit_code: int | None,
    signal: int | None,
    stdout_bytes: int,
    stderr_bytes: int,
    budget_exceeded_kind: BudgetExceededKind | None,
    updated_at: str,
) -> bool:
    """Write how a running command ended to the row its starting call wrote.

    Byte totals are over the command's whole life. Returns False, changing
    nothing, when the invocation has no running row: its end is already written.
    """

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            cursor = connection.execute(
                """
                UPDATE command_invocation_audits
                SET terminal_outcome = ?,
                    exit_code = ?,
                    signal = ?,
                    stdout_bytes = ?,
                    stderr_bytes = ?,
                    budget_exceeded_kind = ?,
                    updated_at = ?
                WHERE invocation_id = ?
                  AND terminal_outcome = 'running'
                """,
                (
                    terminal_outcome,
                    exit_code,
                    signal,
                    stdout_bytes,
                    stderr_bytes,
                    budget_exceeded_kind,
                    updated_at,
                    invocation_id,
                ),
            )
    return cursor.rowcount == 1
