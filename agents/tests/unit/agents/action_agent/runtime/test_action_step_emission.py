from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.action_step_emission import (
    emit_visible_action_step,
)
from pantaray_agents.agents.action_agent.runtime.steps import tool as tool_steps
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    TerminalStepEmission,
    record_tool_step,
)
from pantaray_agents.agents.action_agent.tools.submit_final_answer_tool import (
    SUBMIT_FINAL_ANSWER_TOOL_ID,
)
from pantaray_agents.agents.action_agent.tools.thinking_tool import THINKING_TOOL
from pantaray_agents.application.action.ports import (
    ActionAssistantMessageEmission,
    ActionToolStepEmission,
)
from pantaray_agents.schema.action_conversation import (
    ActionAssistantMessageEventData,
    ActionStepEventData,
    ActionStepStatus,
)


async def test_visible_tool_start_and_terminal_share_replacement_identity() -> None:
    emitted: list[ActionStepEventData] = []

    async def capture(event: ActionStepEventData) -> None:
        emitted.append(event)

    async def emit(status: ActionStepStatus, completed_at: str | None) -> None:
        await emit_visible_action_step(
            capture,
            action_id="action-1",
            process_id="process-1",
            event=ActionToolStepEmission(
                step_id="step-7",
                step_number=7,
                step_name="tool::read",
                label="Reviewing files",
                tool_args={"args": {"path": "src/main.py"}},
                status=status,
                started_at="2026-08-29T01:02:03+09:00",
                completed_at=completed_at,
            ),
        )

    await emit("processing", None)
    await emit("success", "2026-08-29T01:02:05+09:00")

    assert [event.model_dump(mode="json") for event in emitted] == [
        {
            "action_id": "action-1",
            "process_id": "process-1",
            "step_kind": "tool",
            "step_id": "step-7",
            "step_number": 7,
            "tool_id": "read",
            "label": "Reviewing files",
            "subject": "src/main.py",
            "status": "processing",
            "started_at": "2026-08-28T16:02:03.000000Z",
            "completed_at": None,
        },
        {
            "action_id": "action-1",
            "process_id": "process-1",
            "step_kind": "tool",
            "step_id": "step-7",
            "step_number": 7,
            "tool_id": "read",
            "label": "Reviewing files",
            "subject": "src/main.py",
            "status": "success",
            "started_at": "2026-08-28T16:02:03.000000Z",
            "completed_at": "2026-08-28T16:02:05.000000Z",
        },
    ]


async def test_hidden_formal_tools_emit_nothing() -> None:
    emitted: list[ActionStepEventData] = []

    async def capture(event: ActionStepEventData) -> None:
        emitted.append(event)

    for step_name in (
        f"tool::{THINKING_TOOL.tool_id}",
        f"tool::{SUBMIT_FINAL_ANSWER_TOOL_ID}",
        "goal_worker::read",
    ):
        await emit_visible_action_step(
            capture,
            action_id="action-1",
            process_id="process-1",
            event=ActionToolStepEmission(
                step_id=f"step-{step_name}",
                step_number=1,
                step_name=step_name,
                label="private",
                tool_args={},
                status="processing",
                started_at="2026-08-29T00:00:00Z",
                completed_at=None,
            ),
        )

    assert emitted == []


async def test_supervisor_terminal_emits_only_after_durable_save() -> None:
    save = AsyncMock()

    async def capture(event: tool_steps.ActionToolStepEmission) -> None:
        assert save.await_count == 1
        assert event.tool_args == {"args": {"path": "src/main.py"}}

    with (
        patch.object(tool_steps, "project_tool_step_for_persistence", MagicMock()),
        patch.object(tool_steps, "persist_action_step", save),
    ):
        await record_tool_step(
            MagicMock(),
            MagicMock(),
            terminal_emission=TerminalStepEmission(capture, "Read"),
            step_id="step-1",
            action_id="action-1",
            step_number=1,
            step_name="tool::read",
            tool_args={"args": {"path": "src/main.py"}},
            result=MagicMock(status="success"),
            started_at="2026-08-29T00:00:00Z",
            completed_at="2026-08-29T00:00:01Z",
            goal_handle="supervisor",
            user_id="user-1",
            short_step_id="S-1-TOOL",
            local_step_number=1,
        )


async def test_native_assistant_change_emits_existing_conversation_notification() -> (
    None
):
    capture = AsyncMock()

    await emit_visible_action_step(
        capture,
        action_id="action-1",
        process_id="process-1",
        event=ActionAssistantMessageEmission(step_id="message-1", step_number=3),
    )

    assert capture.await_args.args[0] == ActionAssistantMessageEventData(
        action_id="action-1",
        process_id="process-1",
        step_kind="assistant",
        step_id="message-1",
        step_number=3,
        status="success",
    )
