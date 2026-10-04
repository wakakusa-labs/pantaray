"""ActionAgent facade helper functions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from pantaray_agents.agents.action_agent.runtime.graph import (
    ActionGraphRuntime,
    RuntimeServices,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentStateConfig
from pantaray_agents.application.action.ports import ActionStepEmitter
from pantaray_agents.local_runtime.tooling import load_approval_session_by_request
from pantaray_agents.local_runtime.tooling.models import StoredApprovalSession
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionResumeUserStep,
)
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.base import JSONValue

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent.agent import ActionAgent


class ActionGraphRuntimeCreateInput(BaseModel):
    """Graph runtime 生成に必要な入力形状。"""

    model_config = ConfigDict(frozen=True)

    request: ActionAgentRequest
    state_config: ActionAgentStateConfig
    intervening_user_step: ActionResumeUserStep | None


def load_runtime_approval_session_by_request(
    read_local_runtime_db_config: Callable[[], tuple[Path, int]],
    user_id: str,
    tool_request_id: str,
) -> StoredApprovalSession | None:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        tool_request_id=tool_request_id,
    )


def create_action_graph_runtime(
    agent: ActionAgent,
    runtime_services: RuntimeServices,
    *,
    input: ActionGraphRuntimeCreateInput,
    emit_action_step: ActionStepEmitter,
    emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
) -> ActionGraphRuntime:
    return ActionGraphRuntime(
        agent=agent,
        request=input.request,
        state_config=input.state_config,
        emit_action_step=emit_action_step,
        emit_error=emit_error,
        services=runtime_services,
        intervening_user_step=input.intervening_user_step,
    )


def coerce_action_token_budget(raw: object) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise RuntimeError("Action header token_budget must be an integer or null")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            raise RuntimeError(
                f"Action header token_budget must be an integer: {raw!r}"
            ) from None
    else:
        raise RuntimeError(f"Action header token_budget must be an integer: {raw!r}")
    if value <= 0:
        raise RuntimeError(
            f"Action header token_budget must be a positive integer: {raw!r}"
        )
    return value
