from __future__ import annotations

import pytest

from pantaray_agents.agents.artifact_react import (
    ReactFinish,
    ReactLoopStep,
    run_react_loop,
)
from pantaray_agents.tools.contract import ReactToolCall, ReactToolResult


@pytest.mark.asyncio
async def test_react_loop_records_consumed_llm_thoughts() -> None:
    recorded_steps: list[ReactLoopStep] = []

    async def call_llm(_prompt: str) -> str:
        return "done"

    async def execute_tool(_call: ReactToolCall, _step_number: int) -> ReactToolResult:
        raise AssertionError("tool should not be executed")

    result = await run_react_loop(
        run_id="run-thinking",
        initial_prompt="prompt",
        call_llm=call_llm,
        parse_output=lambda _output: ReactFinish(final_text="done"),
        execute_tool=execute_tool,
        record_step=lambda step: _record(recorded_steps, step),
        consume_llm_thoughts=lambda: "gemini thought",
    )

    assert result.status == "success"
    assert recorded_steps[0].thinking == "gemini thought"


async def _record(steps: list[ReactLoopStep], step: ReactLoopStep) -> None:
    steps.append(step)
