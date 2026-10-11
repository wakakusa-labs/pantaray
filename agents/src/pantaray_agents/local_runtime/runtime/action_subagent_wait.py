from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.schema.agent.action_subagent import (
    ACTION_SUBAGENT_MAX_ACTIVE_CHILDREN_PER_PARENT,
    ActionSubagentCollectionReceipt,
    ActionSubagentWaitRequest,
    ActionSubagentWaitResult,
)

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from ..storage.transactions import SQLiteTransactionOwnershipError
from .action_subagent_ownership import owned_child_sql
from .action_subagent_terminal import (
    ActionSubagentTerminalResult,
    load_action_subagent_terminal_result_in_connection,
)
from .job_payload_models import parse_action_subagent_job_payload_json
from .job_types import LOCAL_ACTION_JOB_TYPE, LOCAL_ACTION_SUBAGENT_JOB_TYPE

ACTION_SUBAGENT_WAIT_MAX_SECONDS = 30


class ActionSubagentWaitAuthorityError(RuntimeError):
    pass


class ActionSubagentWaitInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionSubagentWaitSnapshot:
    results: tuple[ActionSubagentWaitResult, ...]
    terminal_child_process_ids: tuple[str, ...]

    @property
    def all_terminal(self) -> bool:
        return len(self.results) == len(self.terminal_child_process_ids)


def read_action_subagent_wait_snapshot(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: ActionSubagentWaitRequest,
) -> ActionSubagentWaitSnapshot:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    _validate_request(request)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        return _read_snapshot_in_connection(connection, request)


def collect_action_subagent_results_in_connection(
    connection: sqlite3.Connection,
    *,
    receipt: ActionSubagentCollectionReceipt,
) -> None:
    if not connection.in_transaction:
        raise SQLiteTransactionOwnershipError("collection requires transaction")
    snapshot = _read_snapshot_in_connection(connection, receipt.request)
    if not snapshot.all_terminal:
        raise ActionSubagentWaitAuthorityError("subagent collection is not terminal")
    child_process_ids = receipt.request.child_process_ids
    placeholders = ",".join("?" for _ in child_process_ids)
    # The snapshot above proved ownership; a finished child is collected whichever
    # run of this Action spawned it.
    updated = connection.execute(
        f"""UPDATE processes
        SET result_collected_at=COALESCE(result_collected_at, ?)
        WHERE user_id=? AND action_id=?
          AND kind='action_subagent' AND status IN ('completed','failed','canceled')
          AND process_id IN ({placeholders})""",
        (
            receipt.collected_at,
            receipt.request.user_id,
            receipt.request.action_id,
            *child_process_ids,
        ),
    )
    if int(updated.rowcount) != len(child_process_ids):
        raise ActionSubagentWaitAuthorityError("subagent collection authority changed")


def _validate_request(request: ActionSubagentWaitRequest) -> None:
    identities = (
        request.user_id,
        request.action_id,
        request.parent_process_id,
        request.parent_job_id,
    )
    children = request.child_process_ids
    if any(not value.strip() for value in identities):
        raise ActionSubagentWaitInputError("subagent wait identity is invalid")
    if (
        not children
        or len(children) > ACTION_SUBAGENT_MAX_ACTIVE_CHILDREN_PER_PARENT
        or len(set(children)) != len(children)
        or any(not child.strip() for child in children)
    ):
        raise ActionSubagentWaitInputError("child_process_ids are invalid")


def _read_snapshot_in_connection(
    connection: sqlite3.Connection,
    request: ActionSubagentWaitRequest,
) -> ActionSubagentWaitSnapshot:
    _require_active_parent(connection, request)
    placeholders = ",".join("?" for _ in request.child_process_ids)
    rows = connection.execute(
        f"""
        SELECT child.process_id,payload.payload_json
        FROM processes AS child
        JOIN jobs AS job ON job.process_id=child.process_id
          AND job.job_type=? AND job.logical_key=job.job_id
        JOIN job_payloads AS payload ON payload.job_id=job.job_id
        WHERE child.user_id=? AND child.action_id=?
          AND {owned_child_sql("?")} AND child.kind='action_subagent'
          AND child.process_id IN ({placeholders})
        """,
        (
            LOCAL_ACTION_SUBAGENT_JOB_TYPE,
            request.user_id,
            request.action_id,
            request.parent_process_id,
            *request.child_process_ids,
        ),
    ).fetchall()
    by_id = {str(row["process_id"]): row for row in rows}
    if len(rows) != len(request.child_process_ids) or len(by_id) != len(rows):
        raise ActionSubagentWaitInputError(
            "one or more child_process_ids are not owned by the active parent"
        )

    results: list[ActionSubagentWaitResult] = []
    terminal_ids: list[str] = []
    for child_process_id in request.child_process_ids:
        payload = parse_action_subagent_job_payload_json(
            str(by_id[child_process_id]["payload_json"])
        )
        terminal = load_action_subagent_terminal_result_in_connection(
            connection=connection,
            payload=payload,
        )
        results.append(_project_result(child_process_id, terminal))
        if terminal is not None:
            terminal_ids.append(child_process_id)
    return ActionSubagentWaitSnapshot(tuple(results), tuple(terminal_ids))


def _require_active_parent(
    connection: sqlite3.Connection,
    request: ActionSubagentWaitRequest,
) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM agent_actions AS action
        JOIN processes AS parent ON parent.action_id=action.action_id
          AND parent.user_id=action.user_id
        JOIN jobs AS job ON job.job_id=parent.current_job_id
          AND job.process_id=parent.process_id AND job.user_id=parent.user_id
        WHERE action.action_id=? AND action.user_id=? AND action.status='processing'
          AND parent.process_id=? AND parent.kind='action' AND parent.status='running'
          AND parent.current_job_id=? AND job.job_type=? AND job.status='running'
          AND job.logical_key=action.action_id
        """,
        (
            request.action_id,
            request.user_id,
            request.parent_process_id,
            request.parent_job_id,
            LOCAL_ACTION_JOB_TYPE,
        ),
    ).fetchone()
    if row is None:
        raise ActionSubagentWaitAuthorityError("active parent trace is unavailable")


def _project_result(
    child_process_id: str,
    terminal: ActionSubagentTerminalResult | None,
) -> ActionSubagentWaitResult:
    if terminal is None:
        return {"child_process_id": child_process_id, "status": "nonterminal"}
    if terminal["outcome"] == "success":
        return {
            "child_process_id": child_process_id,
            "status": "success",
            "report": terminal["report"],
        }
    if terminal["outcome"] == "failure":
        return {
            "child_process_id": child_process_id,
            "status": "failure",
            "error_code": terminal["error_code"],
        }
    return {"child_process_id": child_process_id, "status": "canceled"}
