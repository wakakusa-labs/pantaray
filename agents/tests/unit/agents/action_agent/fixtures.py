"""action_agent テスト共通ヘルパー

このモジュールは action_agent 配下のテストで共通して使用するヘルパー関数を提供する。
conftest.py の from conftest import パターンによる sys.modules 衝突を回避するため、
明示的なモジュールとして切り出している。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentContext,
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    ActionTokenAccountingService,
    StateTokenSink,
    TokenAccountingDeps,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.base import AgentError, StatusType

# --- goal_worker 用ヘルパー ---


def create_state_token_sink(state: ActionAgentState) -> StateTokenSink:
    def build_agent_error(**values: object) -> AgentError:
        values.setdefault("severity", "error")
        return AgentError.model_validate(values)

    service = ActionTokenAccountingService(
        TokenAccountingDeps(
            build_agent_error=build_agent_error,
        )
    )
    return StateTokenSink(service, state)


def attach_local_execution_context(
    state: ActionAgentState,
    *,
    manifest_id: str = "manifest-test-123",
    execution_session_id: str = "exec-test-123",
    execution_network_policy: str = "restricted",
    read_access_scope: str = "workspace",
    action_temp_dir: str = "/tmp/action-test",
    app_runtime_python: str = "/usr/bin/python3",
) -> ActionAgentState:
    """Attach the minimal local execution context required for tool execution."""

    state["manifest_id"] = manifest_id
    state["execution_session_id"] = execution_session_id
    state["execution_network_policy"] = execution_network_policy
    state["read_access_scope"] = read_access_scope
    state["action_temp_dir"] = action_temp_dir
    state["app_runtime_python"] = app_runtime_python
    context = cast(ActionAgentContext, state["context"])
    context["read_access_scope"] = read_access_scope
    return state


def create_local_runtime_db(*, db_path: Path) -> Path:
    """Create a migrated local runtime DB seeded with the core tooling catalog."""

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    return db_path


def install_local_runtime_env(*, monkeypatch, db_path: Path) -> Path:
    """Point tests at a prepared local runtime DB."""

    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    return db_path


def install_local_runtime_tool_context(
    *,
    monkeypatch,
    tmp_path,
    state: ActionAgentState,
    allowed_tool_ids: tuple[str, ...],
) -> ActionAgentState:
    """Bootstrap a local runtime DB and attach canonical execution context to state."""

    db_path = tmp_path / "runtime.db"
    create_local_runtime_db(db_path=db_path)
    insert_agent_action(
        db_path=db_path,
        user_id=str(state["user_id"]),
        action_id=str(state["action_id"]),
        created_at=str(state["started_at"]),
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=str(state["user_id"]),
        action_id=str(state["action_id"]),
        started_at=str(state["started_at"]),
        allowed_tool_ids=allowed_tool_ids,
    )
    install_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    return attach_local_execution_context(
        state,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        execution_network_policy=context.network_policy,
        read_access_scope=context.read_access_scope,
        action_temp_dir=str(context.action_temp_dir),
        app_runtime_python=str(context.app_runtime_python),
    )


def create_state(
    *,
    action_id: str = "test-action-id",
) -> ActionAgentState:
    """テスト用の ActionAgentState を生成する。"""
    state = create_initial_state(
        user_id="test-user-id",
        suggestion_id="test-suggestion-id",
        action_id=action_id,
        started_at="2025-01-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=10000,
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["steps_taken"] = 1
    state["llm_steps_taken"] = 1
    state["tool_steps_taken"] = 0
    return attach_local_execution_context(state)


# --- tools 用ヘルパー ---


def base_state(action_agent: ActionAgent) -> dict:
    """テスト用の基本状態を生成する。"""
    del action_agent
    state = create_initial_state(
        user_id="user-123",
        suggestion_id="sug-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state["cancel_check_max_consecutive_failures"] = 3
    state["cancel_check_failure_grace_seconds"] = 60
    return state


def build_action_request(
    *,
    action_id: str,
    suggestion_id: str | None,
    user_id: str,
    content: str = "Test Action request.",
    message_id: str | None = None,
    user_step_id: str | None = None,
    user_step_number: int = 1,
    user_step_local_step_number: int = 1,
    user_step_short_id: str | None = None,
    user_step_created_at: str = "2025-01-01T00:00:00Z",
    suggestion_approval: SuggestionApprovalInput | None = None,
    approval_resume_session_id: str | None = None,
    approval_resume_tool_request_id: str | None = None,
    language: Literal["en", "ja"] | None = None,
) -> ActionAgentRequest:
    """Build a complete durable Action request for runtime tests."""

    resolved_message_id = message_id or f"message:{action_id}"
    resolved_user_step_id = user_step_id or f"step:{resolved_message_id}"
    resolved_short_id = user_step_short_id or (f"S-{user_step_local_step_number}-USER")
    return ActionAgentRequest(
        action_id=action_id,
        suggestion_id=suggestion_id,
        user_id=user_id,
        user_step_id=resolved_user_step_id,
        user_step_number=user_step_number,
        user_step_local_step_number=user_step_local_step_number,
        user_step_short_id=resolved_short_id,
        user_step_created_at=user_step_created_at,
        user_message=ActionUserMessageInput(
            message_id=resolved_message_id,
            content=content,
            suggestion_approval=suggestion_approval,
            language=language,
        ),
        approval_resume_session_id=approval_resume_session_id,
        approval_resume_tool_request_id=approval_resume_tool_request_id,
    )


def project_request_user_step(
    state: ActionAgentState, request: ActionAgentRequest
) -> ActionAgentState:
    """Give direct THINK tests the adopted USER that initialization normally adds."""
    return project_persisted_user_request_step(
        state,
        step_id=request.user_step_id,
        step_number=request.user_step_number,
        local_step_number=request.user_step_local_step_number,
        short_step_id=request.user_step_short_id,
        request_text=request.user_message.content,
        occurred_at=request.user_step_created_at,
        history_phase="init",
    )


async def seed_action_header(
    action_agent: ActionAgent,
    *,
    action_id: str,
    suggestion_id: str,
    user_id: str,
) -> None:
    """テスト用のアクションヘッダーを保存する。"""
    await action_agent.repository.save_action(
        {
            "action_id": action_id,
            "suggestion_id": suggestion_id,
            "user_id": user_id,
            "final_output": "",
            "status": StatusType.PROCESSING.value,
            "prompt_name": "action/executing",
            "prompt_version": "test",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        },
        prompt_name="action/executing",
        prompt_version="test",
    )
