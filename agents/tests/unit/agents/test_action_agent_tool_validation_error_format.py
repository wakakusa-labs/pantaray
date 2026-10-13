from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    create_state_token_sink,
    install_local_runtime_tool_context,
)

from pantaray_agents.agents.action_agent.runtime.handlers.nodes import action_step
from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    ToolValidationError,
    _validate_tool_args,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    build_next_action,
    build_tool_call,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.tools import LIST_TOOL
from pantaray_agents.schema.agent.base import AgentError, JSONValue

type ErrorPayload = dict[str, JSONValue]


class _DummyAgent:
    """action_step の ToolValidationError 経路を検証する最小エージェント。"""

    def __init__(self) -> None:
        self.repository = AsyncMock()

    def _build_node_uuid(self, *, action_id: str, handle: str, kind: str) -> str:  # noqa: ARG002
        """action_step の永続化パスに必要なダミー。"""

        return f"uuid:{action_id}:{kind}:{handle}"

    def _build_agent_error(  # type: ignore[no-untyped-def]
        self,
        *,
        error_type: str,
        error_code: str,
        error_message: str,
        severity: str = "error",
        error_details: ErrorPayload | None = None,
        metadata: ErrorPayload | None = None,
    ) -> AgentError:
        return AgentError(
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            error_details=error_details,
            severity=severity,
            metadata=metadata,
        )


class _Runtime:
    """action_step に必要な emit_* を備えた最小ランタイム。"""

    def __init__(self) -> None:
        self.emit_action_step = AsyncMock()
        self.emit_error = AsyncMock()
        self.services = SimpleNamespace(
            cancellation=SimpleNamespace(
                check_cancellation=AsyncMock(return_value=False)
            ),
            response=SimpleNamespace(
                build_agent_error=_DummyAgent()._build_agent_error
            ),
        )


def test_schema_required_error_names_missing_arg() -> None:
    with pytest.raises(ToolValidationError) as exc_info:
        _validate_tool_args(LIST_TOOL, {})

    message = (
        "Missing required arg `path`. Provide `args.path` according to the tool schema."
    )
    assert str(exc_info.value) == message
    assert exc_info.value.details["message"] == message
    assert exc_info.value.details["metadata"]["validator"] == "required"


@pytest.mark.asyncio
async def test_action_step_keeps_recoverable_validation_error_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """スキーマ違反時、クライアント送信イベントには詳細を露出しないこと。"""
    agent = _DummyAgent()
    runtime = _Runtime()

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=create_initial_state(
            user_id="user-123",
            suggestion_id="sug-123",
            action_id="act-123",
            started_at="2025-01-01T00:00:00Z",
            max_steps=10,
            max_tool_steps=10,
            token_budget=None,
        ),
        allowed_tool_ids=("web_search",),
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id="web_search",
            args={"queries": ["pantaray"], "topic": "invalid"},
        ),
        decided_at="2025-01-01T00:00:01Z",
    )

    updated = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    # ToolValidationError は「自己修復可能なエラー」として扱い、Action を即終了しない
    assert updated["status"] == "processing"
    runtime.emit_error.assert_not_awaited()
    agent.repository.save_action_step.assert_awaited_once()

    # 内部エラー詳細には、自己修復用の validation 詳細が含まれる
    assert updated["errors"], "state.errors に validation_error が保存されていません"
    first_error = updated["errors"][0]
    assert first_error.get("severity") == "warning"
    details = first_error.get("error_details", {})
    assert details.get("tool_id") == "web_search"
    validation = details.get("validation", {})
    metadata = validation.get("metadata", {})
    assert metadata.get("validator") == "enum"
    validator_value = metadata.get("validator_value", [])
    assert isinstance(validator_value, list)
    assert "general" in validator_value
    assert "news" in validator_value


@pytest.mark.asyncio
async def test_action_step_malformed_args_does_not_emit_empty_allowed_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """operation enum がないツールでは allowed_operations を出さないこと。"""
    agent = _DummyAgent()
    runtime = _Runtime()

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=create_initial_state(
            user_id="user-123",
            suggestion_id="sug-123",
            action_id="act-123",
            started_at="2025-01-01T00:00:00Z",
            max_steps=10,
            max_tool_steps=10,
            token_budget=None,
        ),
        allowed_tool_ids=("memory_search",),
    )
    state["context"]["use_goal_workers"] = False
    state["phase"] = "executing"
    state["next_action"] = {
        "tool": {"tool_id": "memory_search", "args": "invalid-args-type"},
        "decided_at": "2025-01-01T00:00:01Z",
    }

    updated = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    assert updated["status"] == "processing"
    runtime.emit_error.assert_not_awaited()

    assert updated["errors"], "state.errors に validation_error が保存されていません"
    first_error = updated["errors"][0]
    assert first_error.get("severity") == "warning"
    details = first_error.get("error_details", {})
    assert details.get("tool_id") == "memory_search"
    assert "allowed_operations" not in details
    validation = details.get("validation", {})
    assert validation.get("path") == ["next_action", "tool", "args"]
