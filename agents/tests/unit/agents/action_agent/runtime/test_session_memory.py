from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    create_state,
    create_state_token_sink,
)

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution import (
    run_validated_tool_impl,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from pantaray_agents.agents.action_agent.tools import WRITE_SESSION_MEMORY_TOOL

# Three UTF-8 bytes per character: counting characters fails both size tests.
_AT_LIMIT = "あ" * 2730 + "xx"
_OVER_LIMIT = "あ" * 2731


async def _write(
    content: str, *, actor: ToolExecutionActor = "supervisor"
) -> UnprojectedToolExecutionResult:
    state = create_state()
    return await run_validated_tool_impl(
        MagicMock(),
        WRITE_SESSION_MEMORY_TOOL,
        {"content": content},
        state,
        sink=create_state_token_sink(state),
        runtime=SimpleNamespace(),
        actor=actor,
    )


@pytest.mark.asyncio
async def test_a_write_at_the_byte_limit_reports_its_usage() -> None:
    result = await _write(_AT_LIMIT)

    assert result.output == {"status": "written", "bytes": 8192, "limit_bytes": 8192}


@pytest.mark.asyncio
async def test_a_write_over_the_byte_limit_is_rejected_with_both_sizes() -> None:
    with pytest.raises(ToolValidationError) as raised:
        await _write(_OVER_LIMIT)

    assert "8,193 UTF-8 bytes" in str(raised.value)
    assert "limit of 8,192 bytes" in str(raised.value)


@pytest.mark.asyncio
async def test_a_subagent_cannot_write_the_session_memory() -> None:
    with pytest.raises(ToolValidationError, match="only available to the Supervisor"):
        await _write("notes", actor="goal_worker")
