from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    subagent_wait,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.tools import WAIT_SUBAGENTS_TOOL
from pantaray_agents.local_runtime.agent_state import LocalActionRepository
from pantaray_agents.local_runtime.runtime import action_subagent_terminal as terminal
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import StepStatusType
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.utils.trace_context import TraceContextManager

from . import test_action_subagent_spawn as spawn
from .action_seed import insert_agent_action


def _children(
    tmp_path: Path, count: int
) -> tuple[Path, list[ActionSubagentJobPayload]]:
    db_path, context = spawn._runtime(tmp_path)
    payloads: list[ActionSubagentJobPayload] = []
    for index in range(count):
        request = spawn._request(
            context,
            llm_step_id=f"supervisor-think-{index + 1}",
            claim_key="repository:src" if index == 0 else f"resource:{index}",
        )
        if index:
            spawn._insert_think(db_path, request, step_number=index + 2)
        created = spawn._spawn(db_path, request)
        with spawn._connect(db_path) as connection:
            raw = connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id=?",
                (created.job_id,),
            ).fetchone()[0]
        payloads.append(parse_action_subagent_job_payload_json(str(raw)))
    return db_path, payloads


def _terminalize(
    db_path: Path,
    payload: ActionSubagentJobPayload,
    result: terminal.ActionSubagentTerminalResult,
) -> None:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        job_type="execute_action_subagent",
        owner_user_id="user-1",
        claimed_by="test-worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None and claimed["job_id"] == payload["job_id"]
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


def _foreign_child(db_path: Path) -> str:
    insert_agent_action(
        db_path=db_path, action_id="action-2", suggestion_id="suggestion-2"
    )
    with spawn._connect(db_path) as connection:
        connection.executescript(
            """
            INSERT INTO processes(process_id,user_id,kind,status,action_id,current_job_id,started_at,updated_at,heartbeat_at,next_event_seq) VALUES ('foreign-parent','user-1','action','running','action-2','foreign-parent-job','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z',1);
            INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,claimed_by,claimed_at,heartbeat_at,scheduled_at,started_at,logical_key) VALUES ('foreign-parent-job','user-1','execute_action','foreign-parent','running',1,'worker','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','action-2');
            INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,updated_at,heartbeat_at,next_event_seq,parent_process_id) VALUES ('foreign-child','user-1','action_subagent','enqueued','action-2','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z',1,'foreign-parent');
            INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,heartbeat_at,scheduled_at,logical_key) VALUES ('foreign-child-job','user-1','execute_action_subagent','foreign-child','queued',0,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','foreign-child-job');
            INSERT INTO job_payloads(job_id,payload_json) VALUES ('foreign-child-job','{"action_context":"parent context","action_id":"action-2","context_refs":[],"inference_profile_id":"action.subagent.luna","job_id":"foreign-child-job","parent_process_id":"foreign-parent","process_id":"foreign-child","resource_claim_ids":[],"task":"private","user_id":"user-1"}');
            """
        )
    return "foreign-child"


async def _save_collection_step(
    repository: LocalActionRepository,
    *,
    step_id: str,
    receipt: ActionSubagentCollectionReceipt,
    step_name: str = "tool::wait_subagents",
    parent_step_id: str | None = None,
):
    return await repository.save_action_step(
        step_id=step_id,
        action_id="action-1",
        step_number=10,
        step_name=step_name,
        step_type=StepType.TOOL_EXECUTION,
        tool_output={"results": []},
        status=StepStatusType.SUCCESS,
        completed_at=receipt.collected_at,
        goal_handle="S",
        user_id="user-1",
        short_step_id="S-10-TOOL",
        local_step_number=10,
        parent_step_id=parent_step_id,
        subagent_collection_receipt=receipt,
    )


@pytest.mark.asyncio
async def test_wait_projects_mixed_results_and_collects_with_step_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, children = _children(tmp_path, 4)
    _terminalize(
        db_path, children[0], terminal.build_action_subagent_success_result("ok")
    )
    _terminalize(
        db_path, children[1], terminal.build_action_subagent_failure_result("BROKEN")
    )
    _terminalize(db_path, children[2], terminal.build_action_subagent_canceled_result())
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(spawn.BUSY_TIMEOUT_MS))
    monkeypatch.setattr(subagent_wait, "ACTION_SUBAGENT_WAIT_MAX_SECONDS", 0)
    state = {"user_id": "user-1", "action_id": "action-1"}
    with TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id="parent-job",
        extra={"process_id": "parent-process"},
    ):
        execution = await subagent_wait.run_wait_subagents_tool(
            step_id="wait",
            tool_def=WAIT_SUBAGENTS_TOOL,
            args={"child_process_ids": [child["process_id"] for child in children]},
            state=state,
            actor="supervisor",  # type: ignore[arg-type]
        )
        for hidden_id in ("missing-child", _foreign_child(db_path)):
            with pytest.raises(ToolValidationError, match="not owned"):
                await subagent_wait.run_wait_subagents_tool(
                    step_id="hidden",
                    tool_def=WAIT_SUBAGENTS_TOOL,
                    args={"child_process_ids": [hidden_id]},
                    state=state,
                    actor="supervisor",  # type: ignore[arg-type]
                )
    results = execution.output["results"]
    statuses = [result["status"] for result in results]
    assert statuses == ["success", "failure", "canceled", "nonterminal"]
    assert results[1].get("error_code") == "BROKEN"
    assert execution.subagent_collection_receipt is not None
    repository = LocalActionRepository(
        db_path=db_path, busy_timeout_ms=spawn.BUSY_TIMEOUT_MS
    )
    receipt = replace(
        execution.subagent_collection_receipt,
        collected_at="2026-09-01T00:01:00Z",
    )
    saved = await _save_collection_step(repository, step_id="wait-1", receipt=receipt)
    assert saved.error is None
    replay = replace(receipt, collected_at="2026-09-01T00:02:00Z")
    saved = await _save_collection_step(repository, step_id="wait-2", receipt=replay)
    assert saved.error is None
    _terminalize(
        db_path, children[3], terminal.build_action_subagent_success_result("late")
    )
    rollback = replace(
        replay,
        request=replace(replay.request, child_process_ids=(children[3]["process_id"],)),
        collected_at="2026-09-01T00:03:00Z",
    )
    # A rejected step row must roll back the collection it was saved with.
    assert (
        await _save_collection_step(
            repository,
            step_id="wait-3",
            receipt=rollback,
            step_name="tool::cancel_subagent",
            parent_step_id="missing-step",
        )
    ).error
    with spawn._connect(db_path) as connection:
        rows = connection.execute(
            "SELECT result_collected_at FROM processes WHERE process_id IN (?,?) ORDER BY process_id",
            (children[0]["process_id"], children[3]["process_id"]),
        ).fetchall()
    assert {row[0] for row in rows} == {None, "2026-09-01T00:01:00Z"}
    assert (
        await _save_collection_step(
            repository,
            step_id="cancel-retry",
            receipt=rollback,
            step_name="tool::cancel_subagent",
        )
    ).error is None
    with spawn._connect(db_path) as connection:
        collected_at = connection.execute(
            "SELECT result_collected_at FROM processes WHERE process_id=?",
            (children[3]["process_id"],),
        ).fetchone()[0]
    assert collected_at == rollback.collected_at


@pytest.mark.asyncio
async def test_a_stopped_wait_returns_without_waiting_for_its_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止は wait_subagents の待ち合わせを即座に打ち切る。

    子は非終端のままなので、cancel が届かなければこの待ちは
    ``ACTION_SUBAGENT_WAIT_MAX_SECONDS`` まで走り続ける。
    """

    import asyncio

    db_path, children = _children(tmp_path, 1)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(spawn.BUSY_TIMEOUT_MS))
    assert subagent_wait.ACTION_SUBAGENT_WAIT_MAX_SECONDS > 5
    state = {"user_id": "user-1", "action_id": "action-1"}
    with TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id="parent-job",
        extra={"process_id": "parent-process"},
    ):
        waiting = asyncio.ensure_future(
            subagent_wait.run_wait_subagents_tool(
                step_id="wait-canceled",
                tool_def=WAIT_SUBAGENTS_TOOL,
                args={"child_process_ids": [children[0]["process_id"]]},
                state=state,
                actor="supervisor",  # type: ignore[arg-type]
            )
        )
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        canceled_at = loop.time()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert loop.time() - canceled_at < 1.0
