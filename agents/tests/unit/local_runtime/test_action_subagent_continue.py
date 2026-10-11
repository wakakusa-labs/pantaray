"""A message to a subagent that stopped or finished continues it as a new one."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.tasks.test_action_subagent_job import _Client

from pantaray_agents.local_runtime.runtime import action_subagent_terminal as terminal
from pantaray_agents.local_runtime.runtime.action_subagent_continue import (
    ActionSubagentContinueInputError,
    ActionSubagentContinueRequest,
    continue_action_subagent,
)
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ACTION_SUBAGENT_NOTICE_EVENT,
    ActionSubagentMessageInputError,
    append_action_subagent_event,
)
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    bind_local_runtime_db_execution_context,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.tasks.internal_jobs import action_subagent as subagent_job
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from . import test_action_subagent_spawn as spawn
from . import test_action_subagent_wait as wait

EARLIER_NOTE = "Earlier: the boundary is in src/api."
MESSAGE = "Continue from the API boundary."


def _payload(db_path: Path, job_id: str) -> ActionSubagentJobPayload:
    with spawn._connect(db_path) as connection:
        raw = connection.execute(
            "SELECT payload_json FROM job_payloads WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    return parse_action_subagent_job_payload_json(str(raw))


def _claim(db_path: Path) -> None:
    assert claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        job_type="execute_action_subagent",
        owner_user_id="user-1",
        claimed_by="test-worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )


def _finished_child(
    tmp_path: Path, result: terminal.ActionSubagentTerminalResult
) -> tuple[Path, ActionExecutionContext, ActionSubagentJobPayload]:
    """A child that said something to itself, then stopped or finished."""

    db_path, context = spawn._runtime(tmp_path)
    created = spawn._spawn(db_path, spawn._request(context))
    payload = _payload(db_path, created.job_id)
    _claim(db_path)
    append_action_subagent_event(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        payload=payload,
        event_name=ACTION_SUBAGENT_NOTICE_EVENT,
        event_payload={
            "type": "user",
            "content": [{"type": "input_text", "text": EARLIER_NOTE}],
        },
    )
    if result["outcome"] == "canceled":
        with spawn._connect(db_path) as connection:
            connection.execute(
                "UPDATE jobs SET cancel_requested_at=? WHERE job_id=?",
                (spawn.TIMESTAMP, payload["job_id"]),
            )
    terminal.finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        payload=payload,
        result=result,
    )
    return db_path, context, payload


def _continue(
    db_path: Path,
    context: ActionExecutionContext,
    child_process_id: str,
    *,
    llm_step_id: str = "supervisor-think-2",
    step_number: int = 10,
    content: str = MESSAGE,
) -> str:
    think = replace(
        spawn._request(context),
        origin=ActionToolCallOrigin(llm_step_id=llm_step_id, call_id="call-1"),
    )
    spawn._insert_think(db_path, think, step_number=step_number)
    return continue_action_subagent(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        request=ActionSubagentContinueRequest(
            user_id="user-1",
            action_id="action-1",
            parent_process_id="parent-process",
            parent_job_id="parent-job",
            root_execution_session_id=context.execution_session_id,
            manifest_id=context.manifest_id,
            origin=think.origin,
            action_context="# Workspace Paths\nparent context",
            current_cwd=context.workspace_path,
            child_process_id=child_process_id,
            message_id="message-1",
            content=content,
            continued_at=spawn.TIMESTAMP,
        ),
    ).child_process_id


def _child_row(db_path: Path, process_id: str) -> tuple[object, ...]:
    with spawn._connect(db_path) as connection:
        return tuple(
            connection.execute(
                "SELECT status,terminal_event_id,completed_at,"
                "(SELECT COUNT(*) FROM process_events WHERE process_id=?) "
                "FROM processes WHERE process_id=?",
                (process_id, process_id),
            ).fetchone()
        )


@pytest.mark.parametrize(
    "result",
    (
        terminal.build_action_subagent_canceled_result(),
        terminal.build_action_subagent_success_result("Found the boundary."),
    ),
    ids=("stopped", "finished"),
)
def test_a_message_continues_the_child_with_its_conversation_and_the_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: terminal.ActionSubagentTerminalResult,
) -> None:
    db_path, context, finished = _finished_child(tmp_path, result)
    before = _child_row(db_path, finished["process_id"])

    new_child_id = _continue(db_path, context, finished["process_id"])
    continued = _payload(db_path, _job_id(db_path, new_child_id))

    assert new_child_id != finished["process_id"]
    assert (continued["task"], continued["context_refs"]) == (
        finished["task"],
        finished["context_refs"],
    )
    assert continued["inference_profile_id"] == finished["inference_profile_id"]
    with spawn._connect(db_path) as connection:
        claims = connection.execute(
            "SELECT child_process_id,resource_kind,root_identity,normalized_key,"
            "released_at IS NULL FROM action_subagent_resource_claims ORDER BY rowid"
        ).fetchall()
    assert [tuple(row)[1:4] for row in claims] == [
        ("external_resource", "workspace", "repository:src")
    ] * 2
    assert [(row[0], row[4]) for row in claims] == [
        (finished["process_id"], 0),
        (new_child_id, 1),
    ]
    # The finished child is left exactly as it ended.
    assert _child_row(db_path, finished["process_id"]) == before

    _claim(db_path)
    client = _Client()
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=spawn.BUSY_TIMEOUT_MS
    ):
        subagent_job.run_action_subagent_job(continued)

    sent = client.tool_uses[0].conversation
    assert sent is not None
    texts = [
        block.text
        for item in sent
        if item.type == "user"
        for block in item.content
        if block.type == "input_text"
    ]
    assert texts[-2:] == [EARLIER_NOTE, MESSAGE]
    with spawn._connect(db_path) as connection:
        stream_end = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id=? AND event_name='stream_end'",
            (new_child_id,),
        ).fetchone()
    assert json.loads(stream_end[0])["outcome"] == "success"


def test_a_continuation_counts_against_the_active_child_limit(tmp_path: Path) -> None:
    db_path, context, finished = _finished_child(
        tmp_path, terminal.build_action_subagent_canceled_result()
    )
    for index in range(4):
        request = spawn._request(
            context,
            llm_step_id=f"think-active-{index}",
            claim_key=f"resource:{index}",
        )
        spawn._insert_think(db_path, request, step_number=index + 3)
        spawn._spawn(db_path, request)
    events_before = _child_row(db_path, finished["process_id"])

    with pytest.raises(MigrationError, match="active child limit"):
        _continue(db_path, context, finished["process_id"])

    assert _child_row(db_path, finished["process_id"]) == events_before
    with spawn._connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM processes WHERE kind='action_subagent'"
        ).fetchone()[0]
    assert count == 5


def test_a_finished_child_of_another_action_is_not_continued(tmp_path: Path) -> None:
    db_path, context = spawn._runtime(tmp_path)
    foreign = wait._foreign_child(db_path)
    with spawn._connect(db_path) as connection:
        connection.execute(
            "UPDATE processes SET status='canceled' WHERE process_id=?", (foreign,)
        )

    with pytest.raises(ActionSubagentContinueInputError, match="not a finished"):
        _continue(db_path, context, foreign)


def _job_id(db_path: Path, process_id: str) -> str:
    with spawn._connect(db_path) as connection:
        return str(
            connection.execute(
                "SELECT job_id FROM jobs WHERE process_id=?", (process_id,)
            ).fetchone()[0]
        )


def _subagent_count(db_path: Path) -> int:
    with spawn._connect(db_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM processes WHERE kind='action_subagent'"
            ).fetchone()[0]
        )


@pytest.mark.parametrize("continuation_ended", (False, True), ids=("live", "ended"))
def test_the_same_message_sent_again_gets_the_same_continuation(
    tmp_path: Path, continuation_ended: bool
) -> None:
    """A message continues a finished child once, whichever call sends it again.

    The model names the message_id, so a later turn can carry the same message
    with a new call (a replay after a crash or a resume, or a plain retry).
    """

    db_path, context, finished = _finished_child(
        tmp_path, terminal.build_action_subagent_canceled_result()
    )
    first = _continue(db_path, context, finished["process_id"])
    if continuation_ended:
        _claim(db_path)
        terminal.finalize_action_subagent_terminal(
            db_path=db_path,
            busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
            payload=_payload(db_path, _job_id(db_path, first)),
            result=terminal.build_action_subagent_success_result("Done."),
        )

    again = _continue(
        db_path,
        context,
        finished["process_id"],
        llm_step_id="supervisor-think-3",
        step_number=11,
    )

    assert again == first
    assert _subagent_count(db_path) == 2
    with pytest.raises(ActionSubagentMessageInputError, match="different content"):
        _continue(
            db_path,
            context,
            finished["process_id"],
            llm_step_id="supervisor-think-4",
            step_number=12,
            content="Something else.",
        )
    assert _subagent_count(db_path) == 2
