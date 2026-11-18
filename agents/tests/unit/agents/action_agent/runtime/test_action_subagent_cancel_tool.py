from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    execution,
    subagent_cancel,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import CANCEL_SUBAGENT_TOOL
from pantaray_agents.local_runtime.runtime.action_subagent_cancel import (
    ActionSubagentCancelInputError,
)
from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    ActionSubagentWaitSnapshot,
)
from pantaray_agents.utils.trace_context import TraceContextManager

_STATE = cast(ActionAgentState, {"user_id": "user-1", "action_id": "action-1"})


def _trace() -> TraceContextManager:
    return TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id="parent-job",
        extra={"process_id": "parent-process"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    (
        ActionSubagentWaitSnapshot(
            ({"child_process_id": "child-1", "status": "canceled"},),
            ("child-1",),
        ),
        ActionSubagentWaitSnapshot(
            ({"child_process_id": "child-1", "status": "nonterminal"},), ()
        ),
    ),
)
async def test_cancel_requests_before_poll_and_returns_observed_result(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: ActionSubagentWaitSnapshot,
) -> None:
    events: list[object] = []

    def _cancel(**kwargs: object) -> None:
        events.append(kwargs["request"])

    async def _stop(**kwargs: object) -> None:
        events.append(("stop", kwargs["request"].child_process_id))  # type: ignore[attr-defined]

    async def _poll(**_kwargs: object) -> ActionSubagentWaitSnapshot:
        events.append("poll")
        return snapshot

    monkeypatch.setenv("LOCAL_DB_PATH", "/tmp/runtime.db")
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(
        subagent_cancel, "request_action_subagent_cancellation", _cancel
    )
    monkeypatch.setattr(subagent_cancel, "stop_action_subagent_child_processes", _stop)
    monkeypatch.setattr(subagent_cancel, "_poll_wait", _poll)
    with _trace():
        result = await execution.run_validated_tool_impl(
            MagicMock(),
            CANCEL_SUBAGENT_TOOL,
            {"child_process_id": "child-1"},
            _STATE,
            sink=MagicMock(),
            runtime=MagicMock(),
            actor="supervisor",
            step_id="cancel-step",
        )

    # Catches: a queued or paused child, which the request settles and whose
    # claims it releases, keeping its processes running past them.
    assert events[0] == ("stop", "child-1")
    assert events[1].child_process_id == "child-1"  # type: ignore[attr-defined]
    assert events[2] == "poll"
    assert result.output == snapshot.results[0]
    assert (result.subagent_collection_receipt is not None) is snapshot.all_terminal


@pytest.mark.asyncio
async def test_cancel_rejects_goal_worker_and_wrong_child_before_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = MagicMock()
    monkeypatch.setattr(subagent_cancel, "request_action_subagent_cancellation", cancel)

    with pytest.raises(ToolValidationError, match="Only the Supervisor"):
        await subagent_cancel.run_cancel_subagent_tool(
            step_id="cancel-step",
            tool_def=CANCEL_SUBAGENT_TOOL,
            args={"child_process_id": "child-1"},
            state=_STATE,
            actor="goal_worker",
        )
    cancel.assert_not_called()
    poll = AsyncMock()
    monkeypatch.setattr(
        subagent_cancel, "stop_action_subagent_child_processes", AsyncMock()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", "/tmp/runtime.db")
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    cancel.side_effect = ActionSubagentCancelInputError("child is not owned")
    monkeypatch.setattr(subagent_cancel, "_poll_wait", poll)
    with _trace():
        with pytest.raises(ToolValidationError, match="not owned"):
            await subagent_cancel.run_cancel_subagent_tool(
                step_id="cancel-step",
                tool_def=CANCEL_SUBAGENT_TOOL,
                args={"child_process_id": "wrong-child"},
                state=_STATE,
                actor="supervisor",
            )
    poll.assert_not_awaited()
