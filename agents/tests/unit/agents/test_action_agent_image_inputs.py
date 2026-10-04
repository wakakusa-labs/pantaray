from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import create_state_token_sink
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.provider_turns import (
    ActionProviderTurnStore,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    ActionUserMessageInput,
    StepType,
)
from pantaray_agents.utils.memory_source_policy import (
    build_unknown_memory_source_coverage_snapshot,
)
from pantaray_agents.utils.prompt_loader import PromptConfig

_IMAGE_PAYLOAD = b"user-attached-image"
_IMAGE_SHA256 = hashlib.sha256(_IMAGE_PAYLOAD).hexdigest()
_STORAGE_PATH = "user-1/2026-04-05/9d2fd1a7-8c1c-4d0a-9d5f-1e2f3a4b5c6d.png"
_REF = f"user_attachment:{_IMAGE_SHA256[:24]}"
_ATTACHMENT = {
    "type": "file",
    "ref": _REF,
    "blob_ref": f"attachment_blob_{_IMAGE_SHA256[:24]}",
    "display_path": "image-1.png",
    "mime_type": "image/png",
    "byte_size": len(_IMAGE_PAYLOAD),
    "sha256": _IMAGE_SHA256,
    "source_kind": "local_image_blob",
    "storage_path": _STORAGE_PATH,
}


def _build_agent() -> ActionAgent:
    repo = MockActionAgentRepository()

    def _fake_load_config(prompt_name: str) -> PromptConfig:
        del prompt_name
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        return ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=repo,
        )


def _build_runtime(agent: ActionAgent) -> SimpleNamespace:
    return SimpleNamespace(
        state_config={
            "prompt_name": "action/executing",
            "prompt_version": "test",
            "max_steps": 10,
            "max_tool_steps": 10,
            "token_budget": None,
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        open_provider_turn_store=AsyncMock(
            return_value=ActionProviderTurnStore(identity=None)
        ),
        services=agent._runtime_services,  # noqa: SLF001
        request=ActionAgentRequest(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
            user_step_id="user-step-1",
            user_step_number=1,
            user_step_local_step_number=1,
            user_step_short_id="S-1-USER",
            user_step_created_at="2026-04-05T00:00:00Z",
            user_message=ActionUserMessageInput(
                message_id="message-1",
                content="suggestion",
            ),
        ),
    )


def _build_state() -> dict:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-04-05T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state["context"].update(
        {
            "request_summary": "",
            "target_context": {"organization_name": None, "project_name": None},
            "memory_source_coverage": build_unknown_memory_source_coverage_snapshot(
                evaluated_at="2026-04-05T00:00:00Z"
            ),
            "tool_validation_error_streak": 0,
            "local_step_counters": {"S": 1},
        }
    )
    state["history_by_scope"] = {
        "S": [
            {
                "step_id": "user-step-1",
                "step_number": 1,
                "phase": "init",
                "step_type": StepType.USER_REQUEST,
                "summary": "",
                "user_request_text": "look at this",
                "tool_id": None,
                "started_at": "2026-04-05T00:00:00Z",
                "completed_at": "2026-04-05T00:00:00Z",
                "short_step_id": "S-1-USER",
                "attachments": [_ATTACHMENT],
            }
        ]
    }
    state["step"] = 2
    return state


@pytest.mark.asyncio
async def test_execution_think_sends_user_images_as_local_image_file_inputs() -> None:
    agent = _build_agent()
    runtime = _build_runtime(agent)
    state = _build_state()
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=native_tool_turn("thinking", {})
    )
    agent._consume_llm_thoughts = MagicMock(return_value=None)

    await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    _, kwargs = agent._generate_llm_action_turn.call_args
    assert f"- Attached images: {_REF}" in kwargs["prompt"]
    assert kwargs["file_inputs"] == [
        {
            "ref": _REF,
            "blob_ref": f"attachment_blob_{_IMAGE_SHA256[:24]}",
            "mime_type": "image/png",
            "byte_size": len(_IMAGE_PAYLOAD),
            "sha256": _IMAGE_SHA256,
            "source_kind": "local_image_blob",
            "storage_path": _STORAGE_PATH,
        }
    ]
