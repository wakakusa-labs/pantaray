"""Give back the results of a parallel batch that landed before their rows did.

A call of a parallel batch commits its tool invocation, then its tool row with
the run's checkpoint. A crash between the two leaves a command that ran and no
trace of it in the history the resumed run reads, so the next THINK would plan
it as a new call and run it again. Such a call's step id is derived from the
call (``ActionToolCallOrigin.shared_turn_step_id``) and its tool request id names
that step, so each call of the latest THINK without an answer is matched to what
it ran and recorded as the run would have recorded it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.builders import (
    _build_tool_history_entry,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.outcome_line import (
    build_outcome_line,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    HistoryEntry,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    increment_tool_steps_taken,
)
from pantaray_agents.agents.action_agent.tools.native_tool_use import STEP_NOTE_ARG
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse

from ..tooling.repository.tool_invocation_links import (
    link_tool_invocations_to_action_step_in_connection,
)
from .action_checkpoint_retention import (
    carry_batch_checkpoint_in_connection,
    prune_action_checkpoints_in_connection,
)

_SCOPE = "S"

_NEWEST_CHECKPOINT_SQL = """
SELECT step_id, runtime_state_checkpoint FROM agent_action_steps
WHERE user_id = ? AND action_id = ? AND runtime_state_checkpoint IS NOT NULL
ORDER BY step_number DESC, completed_at IS NULL ASC, completed_at DESC,
         created_at DESC, step_id DESC
LIMIT 1
"""

_RAN_SQL = """
SELECT invocation.invocation_id, invocation.status, invocation.started_at,
       invocation.completed_at, output.output_json, output.output_storage_kind
FROM tool_invocations AS invocation
LEFT JOIN tool_outputs AS output ON output.invocation_id = invocation.invocation_id
WHERE invocation.user_id = ? AND invocation.action_id = ?
  AND invocation.tool_request_id LIKE ?
ORDER BY invocation.started_at DESC, invocation.invocation_id DESC
LIMIT 1
"""


def restore_settled_batch_calls_in_connection(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> int:
    """Record each call of the latest THINK that ran but has no answer."""

    newest = connection.execute(_NEWEST_CHECKPOINT_SQL, (user_id, action_id)).fetchone()
    if newest is None:
        return 0
    state = cast(ActionAgentState, json.loads(newest[1]))
    entries = state.get("history_by_scope", {}).get(_SCOPE, [])
    think = next(
        (e for e in reversed(entries) if e["step_type"] == StepType.LLM_OUTPUT), None
    )
    if think is None:
        return 0
    think_row = connection.execute(
        "SELECT step_number, short_step_id, llm_response_text FROM agent_action_steps"
        " WHERE step_id = ? AND user_id = ? AND action_id = ?",
        (think["step_id"], user_id, action_id),
    ).fetchone()
    if think_row is None or not think_row[2]:
        return 0
    answered = {
        e.get("call_id") for e in entries if e.get("llm_step_id") == think["step_id"]
    }
    head_local = int(str(think_row[1]).split("-")[1])
    calls = LlmActionTurnResponse.model_validate_json(think_row[2]).calls
    restored = 0
    for index, call in enumerate(calls):
        if call.call_id in answered:
            continue
        origin = ActionToolCallOrigin(
            llm_step_id=think["step_id"], call_id=call.call_id
        )
        step_id = origin.shared_turn_step_id()
        ran = connection.execute(
            _RAN_SQL, (user_id, action_id, f"{action_id}:{step_id}:%")
        ).fetchone()
        if ran is None:
            continue
        completed = ran[1] == "completed"
        output = cast(JSONValue, json.loads(ran[4])) if ran[4] else {}
        args = {k: v for k, v in call.arguments.items() if k != STEP_NOTE_ARG}
        entry = _build_tool_history_entry(
            step_id=step_id,
            step_number=int(think_row[0]) + index,
            phase="executing",
            summary=str(call.arguments.get(STEP_NOTE_ARG, "")),
            tool_id=call.name,
            started_at=str(ran[2]),
            completed_at=str(ran[3] or ran[2]),
            result_line=build_outcome_line(
                call.name, output, status="ok" if completed else "failed"
            ),
            args=args,
            output=output,
            short_step_id=f"{_SCOPE}-{head_local + index}-TOOL",
            origin=origin,
        )
        _insert_entry(entries, entry)
        _drop_carried_call(state, call.call_id)
        state["step"] = state["step"] + 1
        increment_tool_steps_taken(state)
        _save_row(
            connection,
            user_id=user_id,
            action_id=action_id,
            entry=entry,
            parent_step_id=think["step_id"],
            local_step_number=head_local + index,
            status="success" if completed else "error",
            output=output,
            storage_kind=str(ran[5] or "inline_json"),
            checkpoint=json.dumps(state, ensure_ascii=False),
            invocation_id=str(ran[0]),
        )
        restored += 1
    return restored


def _insert_entry(entries: list[HistoryEntry], entry: HistoryEntry) -> None:
    position = next(
        (
            i
            for i, existing in enumerate(entries)
            if existing["step_number"] > entry["step_number"]
        ),
        len(entries),
    )
    entries.insert(position, entry)


def _drop_carried_call(state: ActionAgentState, call_id: str) -> None:
    """A batch still waiting in ``next_action`` must not run the call again."""

    next_action = cast(dict[str, JSONValue] | None, state.get("next_action"))
    batch = next_action.get("batch") if next_action else None
    if not isinstance(batch, dict) or not isinstance(batch.get("calls"), list):
        return
    calls = [
        c
        for c in cast(list[dict[str, JSONValue]], batch["calls"])
        if cast(dict[str, JSONValue], c.get("origin") or {}).get("call_id") != call_id
    ]
    if not calls:
        state["next_action"] = None
        return
    batch["calls"] = cast(JSONValue, calls)
    assert next_action is not None
    next_action["tool"] = calls[0]["call"]


def _save_row(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    entry: HistoryEntry,
    parent_step_id: str,
    local_step_number: int,
    status: str,
    output: JSONValue,
    storage_kind: str,
    checkpoint: str,
    invocation_id: str,
) -> None:
    tool_id = str(entry["tool_id"])
    connection.execute(
        """INSERT INTO agent_action_steps(
            step_id, action_id, user_id, parent_step_id, step_number,
            local_step_number, short_step_id, step_type, step_name, status,
            goal_handle, retry_count, prompt_tokens, completion_tokens,
            started_at, completed_at, tool_args, tool_output,
            runtime_state_checkpoint, runtime_state_checkpoint_version,
            created_at, call_id, llm_step_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'tool_execution', ?, ?, ?, 0, 0, 0,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["step_id"],
            action_id,
            user_id,
            parent_step_id,
            entry["step_number"],
            local_step_number,
            entry.get("short_step_id"),
            f"tool::{tool_id}",
            status,
            _SCOPE,
            entry["started_at"],
            entry["completed_at"],
            json.dumps({"tool_id": tool_id, "args": entry.get("args", {})}),
            json.dumps(
                {
                    "schema_version": 1,
                    "status": status,
                    "output": output,
                    "output_storage_kind": storage_kind,
                    "output_owner_kind": "tool_invocation",
                },
                ensure_ascii=False,
            ),
            checkpoint,
            RUNTIME_STATE_CHECKPOINT_VERSION,
            entry["completed_at"],
            entry.get("call_id"),
            entry.get("llm_step_id"),
        ),
    )
    carry_batch_checkpoint_in_connection(
        connection,
        user_id=user_id,
        action_id=action_id,
        llm_step_id=parent_step_id,
        step_number=entry["step_number"],
        checkpoint=checkpoint,
        checkpoint_version=RUNTIME_STATE_CHECKPOINT_VERSION,
    )
    prune_action_checkpoints_in_connection(
        connection, user_id=user_id, action_id=action_id
    )
    link_tool_invocations_to_action_step_in_connection(
        connection,
        action_id=action_id,
        invocation_ids=(invocation_id,),
        step_id=entry["step_id"],
    )


__all__ = ["restore_settled_batch_calls_in_connection"]
