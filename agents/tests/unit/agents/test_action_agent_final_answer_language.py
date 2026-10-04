from __future__ import annotations

# pylint: disable=import-error,protected-access
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    project_request_user_step,
)
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn

from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.provider_turns import (
    ActionProviderTurnStore,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.schema.agent.base import AgentError


class _ProviderTurnStore:
    """走りの置き場だけを備えた runtime の代役（未設定なので identity は無い）。"""

    async def open_provider_turn_store(self, _state: object) -> ActionProviderTurnStore:
        return ActionProviderTurnStore(identity=None)


def _build_runtime_services() -> SimpleNamespace:
    return SimpleNamespace(
        cancellation=SimpleNamespace(check_cancellation=AsyncMock(return_value=False)),
        response=SimpleNamespace(
            build_agent_error=MagicMock(
                side_effect=lambda **kwargs: AgentError(
                    severity="error",
                    metadata=None,
                    **kwargs,
                )
            )
        ),
        rendering=SimpleNamespace(
            format_history=MagicMock(return_value=""),
            history_entries=MagicMock(return_value=()),
            format_memory_source_coverage=MagicMock(return_value=""),
            render_memory_context_model=MagicMock(return_value=""),
            render_memory_artifact_references=MagicMock(return_value=""),
            render_linkable_memory_context=MagicMock(return_value=""),
            render_request_summary=MagicMock(return_value=""),
            render_target_context=MagicMock(return_value=""),
            render_workspace_path_contract=MagicMock(return_value=""),
            render_workspace_context_rules=MagicMock(return_value=""),
            render_workspace_context_prompt=MagicMock(return_value=""),
        ),
    )


def _build_execution_state() -> dict[str, object]:
    state = create_initial_state(
        user_id="user",
        suggestion_id="sug",
        action_id="act",
        started_at="2025-01-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state = project_request_user_step(
        state,
        build_action_request(user_id="user", suggestion_id="sug", action_id="act"),
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    return state


@pytest.mark.asyncio
async def test_execution_think_injects_final_answer_language_ja() -> None:
    agent = MagicMock()

    # minimal prompt/instruction
    agent.executing_prompt = (
        "{request_summary}{target_context}"
        "{insight_data}{structured_fact_data}{current_time}{action_history}"
    )
    agent.executing_system_instruction = (
        "BASE\n"
        "IMPORTANT: Final answer drafts MUST be written in {final_answer_language}.\n"
        "Other outputs (tool-call JSON) may be in any language.\n"
    )
    agent.DEFAULT_SYSTEM_INSTRUCTION = "DEFAULT"
    agent.executing_world_state_update.return_value = "UPDATE"

    agent._generate_llm_action_turn = AsyncMock(
        return_value=native_tool_turn("draft_final_answer", {"answer": "ok"})
    )
    agent._consume_llm_thoughts.return_value = None
    agent.repository = MagicMock()
    agent.repository.save_action_step = AsyncMock()

    state = _build_execution_state()

    class _Runtime(_ProviderTurnStore):
        request = type("R", (), {"language": "ja"})()
        state_config = {}
        services = _build_runtime_services()

        async def emit_error(self, _event: object) -> None:
            return None

    await execution_think_step(
        agent, state, _Runtime(), sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    _, kwargs = agent._generate_llm_action_turn.call_args
    system_instruction = kwargs.get("system_instruction", "")
    assert "Final answer drafts" in system_instruction
    assert "Japanese" in system_instruction


@pytest.mark.asyncio
async def test_execution_think_injects_final_answer_language_en() -> None:
    agent = MagicMock()

    agent.executing_prompt = (
        "{request_summary}{target_context}"
        "{insight_data}{structured_fact_data}{current_time}{action_history}"
    )
    agent.executing_system_instruction = (
        "BASE\n"
        "IMPORTANT: Final answer drafts MUST be written in {final_answer_language}.\n"
        "Other outputs (tool-call JSON) may be in any language.\n"
    )
    agent.DEFAULT_SYSTEM_INSTRUCTION = "DEFAULT"
    agent.executing_world_state_update.return_value = "UPDATE"

    agent._generate_llm_action_turn = AsyncMock(
        return_value=native_tool_turn("draft_final_answer", {"answer": "ok"})
    )
    agent._consume_llm_thoughts.return_value = None
    agent.repository = MagicMock()
    agent.repository.save_action_step = AsyncMock()

    state = _build_execution_state()

    class _Runtime(_ProviderTurnStore):
        request = type("R", (), {"language": "en"})()
        state_config = {}
        services = _build_runtime_services()

        async def emit_error(self, _event: object) -> None:
            return None

    await execution_think_step(
        agent, state, _Runtime(), sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    _, kwargs = agent._generate_llm_action_turn.call_args
    system_instruction = kwargs.get("system_instruction", "")
    assert "Final answer drafts" in system_instruction
    assert "English" in system_instruction
