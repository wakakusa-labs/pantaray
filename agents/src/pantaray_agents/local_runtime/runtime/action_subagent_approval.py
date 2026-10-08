"""Settlement of one paused child's approval decision.

The pause module owns the anchor a gated child Tool call parks on; this module
owns the decision that releases it. A decision reads the child's own anchor,
settles exactly the blocker it names and requeues only that child's job, so a
sibling child, the root and any other approval session are untouched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pantaray_agents.schema.agent.base import JSONValue

from ..storage.migrations.connection import configure_connection
from ..storage.transactions import immediate_transaction
from .action_approval_projection import (
    append_pending_approval_snapshot_in_connection,
)
from .action_subagent_messages import ACTION_SUBAGENT_TOOL_EVENT
from .action_subagent_pause import (
    ACTION_SUBAGENT_PAUSE_EVENT,
    anchor_blocker,
    anchor_object,
    decide_approval_session,
    load_paused_child,
    requeue_paused_child,
)
from .job_envelope import LocalJobEnvelopeIntegrityError
from .utc_timestamps import now_utc_iso

_APPROVAL_DECISION_CONFLICT_CODE = "APPROVAL_DECISION_CONFLICT"
_APPROVAL_DECISION_ALREADY_APPLIED_CODE = "APPROVAL_DECISION_ALREADY_APPLIED"

# The decision and the child's requeue commit together, so a session already
# settled to this exact decision on this child's own anchor proves the resent
# request was applied rather than lost.
_SETTLED_CHILD_DECISION_SQL = """
SELECT EXISTS (
    SELECT 1 FROM process_events AS paused
    JOIN json_each(paused.payload_json, '$.approval_blockers') AS blocker
    WHERE paused.process_id = :child_process_id
      AND paused.event_name = :pause_event
      AND json_extract(blocker.value, '$.approval_session_id') = :approval_session_id
      AND json_extract(blocker.value, '$.tool_request_id') = :tool_request_id
)
FROM approval_sessions
WHERE user_id = :user_id AND action_id = :action_id
  AND approval_session_id = :approval_session_id
  AND tool_request_id = :tool_request_id AND status = :decision
"""


class ActionSubagentApprovalDecisionError(RuntimeError):
    """A child approval decision does not match its private pause anchor.

    ``error_code`` carries the same canonical approval codes the root decision
    boundary reports, so a resent decision that already committed converges the
    caller instead of surfacing as a generic conflict.
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class PendingActionSubagentApproval:
    """The saved gated request a resumed child must settle exactly once."""

    tool_id: str
    arguments: dict[str, JSONValue]
    tool_request_id: str
    approval_session_id: str
    # The call of the paused turn this request answers.
    call_id: str


def load_pending_action_subagent_approval(
    *, db_path: Path, busy_timeout_ms: int, process_id: str
) -> PendingActionSubagentApproval | None:
    """Return the child's unsettled pause anchor, if the child was resumed."""

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            """
            SELECT paused.payload_json FROM process_events AS paused
            WHERE paused.process_id=? AND paused.event_name=?
              AND paused.event_seq=(
                  SELECT MAX(latest.event_seq) FROM process_events AS latest
                  WHERE latest.process_id=paused.process_id AND latest.event_name=?)
              AND NOT EXISTS (
                  SELECT 1 FROM process_events AS settled
                  WHERE settled.process_id=paused.process_id
                    AND settled.event_name=? AND settled.event_seq>paused.event_seq)
            """,
            (
                process_id,
                ACTION_SUBAGENT_PAUSE_EVENT,
                ACTION_SUBAGENT_PAUSE_EVENT,
                ACTION_SUBAGENT_TOOL_EVENT,
            ),
        ).fetchone()
    if row is None:
        return None
    anchor = anchor_object(row[0])
    blocker = anchor_blocker(anchor)
    arguments, call_id = anchor.get("tool_arguments"), anchor.get("call_id")
    if not isinstance(arguments, dict) or not isinstance(call_id, str):
        raise LocalJobEnvelopeIntegrityError(
            "Subagent pause anchor has no saved request"
        )
    return PendingActionSubagentApproval(
        tool_id=str(blocker["tool_id"]),
        arguments=cast(dict[str, JSONValue], arguments),
        tool_request_id=str(blocker["tool_request_id"]),
        approval_session_id=str(blocker["approval_session_id"]),
        call_id=call_id,
    )


def apply_action_subagent_approval_decision(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    child_process_id: str,
    approval_session_id: str,
    tool_request_id: str,
    decision: Literal["approved_once", "denied"],
) -> None:
    """Settle one paused child's exact blocker and requeue only its own job."""

    decided_at = now_utc_iso()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            paused = load_paused_child(
                connection,
                user_id=user_id,
                action_id=action_id,
                child_process_id=child_process_id,
            )
            # The child holds this blocker on neither an older nor a newer pause
            # generation, so both cases ask the same question: was this exact
            # decision already committed, or is it stale?
            if paused is None or not _anchor_holds_blocker(
                paused.anchor,
                approval_session_id=approval_session_id,
                tool_request_id=tool_request_id,
            ):
                raise ActionSubagentApprovalDecisionError(
                    "child approval decision is stale or already applied",
                    error_code=_settled_child_decision_code(
                        connection,
                        user_id=user_id,
                        action_id=action_id,
                        child_process_id=child_process_id,
                        approval_session_id=approval_session_id,
                        tool_request_id=tool_request_id,
                        decision=decision,
                    ),
                )
            status = decide_approval_session(
                connection,
                user_id=user_id,
                action_id=action_id,
                approval_session_id=approval_session_id,
                tool_request_id=tool_request_id,
                next_status=decision,
                decided_at=decided_at,
            )
            if status != "updated":
                raise ActionSubagentApprovalDecisionError(
                    f"approval session is not decidable: {status}",
                    error_code=_APPROVAL_DECISION_CONFLICT_CODE,
                )
            requeue_paused_child(
                connection,
                job_id=paused.job_id,
                user_id=user_id,
                process_id=child_process_id,
                resumed_at=decided_at,
            )
            append_pending_approval_snapshot_in_connection(
                connection,
                user_id=user_id,
                action_id=action_id,
                root_process_id=paused.parent_process_id,
                created_at=decided_at,
            )


def _anchor_holds_blocker(
    anchor: dict[str, JSONValue],
    *,
    approval_session_id: str,
    tool_request_id: str,
) -> bool:
    blocker = anchor_blocker(anchor)
    return (blocker["approval_session_id"], blocker["tool_request_id"]) == (
        approval_session_id,
        tool_request_id,
    )


def _settled_child_decision_code(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    child_process_id: str,
    approval_session_id: str,
    tool_request_id: str,
    decision: str,
) -> str:
    """Report the canonical code for a decision the child is no longer holding."""

    row = connection.execute(
        _SETTLED_CHILD_DECISION_SQL,
        {
            "action_id": action_id,
            "approval_session_id": approval_session_id,
            "child_process_id": child_process_id,
            "decision": decision,
            "pause_event": ACTION_SUBAGENT_PAUSE_EVENT,
            "tool_request_id": tool_request_id,
            "user_id": user_id,
        },
    ).fetchone()
    if row is not None and bool(row[0]):
        return _APPROVAL_DECISION_ALREADY_APPLIED_CODE
    return _APPROVAL_DECISION_CONFLICT_CODE
