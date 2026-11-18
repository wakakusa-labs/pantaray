"""Exact durable authority for one visible terminal Action tool output."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import ValidationError

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.tool_result import FormalToolStepOutput

from .sqlite_visibility import ACTION_TOOL_VISIBILITY_SQL_FUNCTION

type TerminalToolStatus = Literal["success", "error", "timeout"]


class ActionToolOutputIntegrityError(MigrationError):
    """A visible terminal tool step has inconsistent durable output."""


@dataclass(frozen=True, slots=True)
class ActionToolOutputAuthority:
    user_id: str
    action_id: str
    step_id: str
    step_number: int
    tool_id: str
    status: TerminalToolStatus
    completed_at: str
    output: FormalToolStepOutput
    output_owner_id: str


def load_action_tool_output_authority_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    step_id: str,
) -> ActionToolOutputAuthority | None:
    """Load one exact visible terminal tool without opening another connection."""

    row = connection.execute(
        f"""
        SELECT steps.step_number,
               {ACTION_TOOL_VISIBILITY_SQL_FUNCTION}(steps.step_name) AS tool_id,
               steps.status, steps.completed_at, steps.tool_output
        FROM agent_actions AS actions
        JOIN agent_action_steps AS steps
          ON steps.action_id = actions.action_id
         AND steps.user_id = actions.user_id
        WHERE actions.user_id = ? AND actions.action_id = ?
          AND steps.step_id = ? AND steps.step_type = 'tool_execution'
          AND {ACTION_TOOL_VISIBILITY_SQL_FUNCTION}(steps.step_name) IS NOT NULL
        LIMIT 1
        """,
        (user_id, action_id, step_id),
    ).fetchone()
    if row is None or row[2] in {"queued", "processing"}:
        return None

    step_number, tool_id, status, completed_at, raw_output = row
    if (
        type(step_number) is not int
        or step_number <= 0
        or not isinstance(tool_id, str)
        or status not in {"success", "error", "timeout"}
        or not isinstance(completed_at, str)
        or not completed_at.strip()
        or not isinstance(raw_output, str)
    ):
        raise ActionToolOutputIntegrityError(
            "Action tool output authority is incomplete"
        )
    try:
        output = FormalToolStepOutput.model_validate_json(raw_output)
    except ValidationError as exc:
        raise ActionToolOutputIntegrityError(
            "Action tool output violates the formal tool-step contract"
        ) from exc
    expected_output_status = "success" if status == "success" else "error"
    if output.status != expected_output_status:
        raise ActionToolOutputIntegrityError(
            "Action tool step and output envelopes are inconsistent"
        )
    terminal_status = cast(TerminalToolStatus, status)
    output_owner_id = (
        step_id
        if output.output_owner_kind == "action_step"
        else _load_invocation_output_owner_id(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            step_id=step_id,
            tool_id=tool_id,
            status=terminal_status,
            output=output,
        )
    )
    return ActionToolOutputAuthority(
        user_id=user_id,
        action_id=action_id,
        step_id=step_id,
        step_number=step_number,
        tool_id=tool_id,
        status=terminal_status,
        completed_at=completed_at,
        output=output,
        output_owner_id=output_owner_id,
    )


def _load_invocation_output_owner_id(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    step_id: str,
    tool_id: str,
    status: TerminalToolStatus,
    output: FormalToolStepOutput,
) -> str:
    invocation_status = "completed" if status == "success" else "failed"
    rows = connection.execute(
        """
        SELECT invocations.invocation_id, outputs.output_json
        FROM tool_invocations AS invocations
        JOIN tool_outputs AS outputs
          ON outputs.invocation_id = invocations.invocation_id
         AND outputs.output_id = invocations.invocation_id
         AND outputs.created_at = invocations.completed_at
        WHERE invocations.user_id = ? AND invocations.action_id = ?
          AND invocations.step_id = ? AND invocations.tool_id = ?
          AND (invocations.status = ?
               OR (? = 'error' AND invocations.status IN ('completed', 'canceled')))
          AND outputs.output_storage_kind = ?
        """,
        (
            user_id,
            action_id,
            step_id,
            tool_id,
            invocation_status,
            status,
            output.output_storage_kind,
        ),
    ).fetchall()
    expected_json = _canonical_json(output.output)
    matches: list[str] = []
    for invocation_id, raw_output in rows:
        if not isinstance(invocation_id, str) or not isinstance(raw_output, str):
            raise ActionToolOutputIntegrityError(
                "Action tool invocation output authority is incomplete"
            )
        try:
            stored_json = _canonical_json(json.loads(raw_output))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ActionToolOutputIntegrityError(
                "Action tool invocation output JSON is invalid"
            ) from exc
        if stored_json == expected_json:
            matches.append(invocation_id)
    if len(matches) != 1:
        raise ActionToolOutputIntegrityError(
            "Action tool invocation output authority is missing or ambiguous"
        )
    return matches[0]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
