import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_local_runtime_db,
    create_state_token_sink,
    install_local_runtime_env,
    install_local_runtime_tool_context,
    project_request_user_step,
)
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.conversation_service import (
    pause_goal_worker_turn,
    start_goal_worker_turn,
)
from pantaray_agents.agents.action_agent.runtime.graph import (
    RESUME_ROUTE_TO_NODE,
    ActionGraphRuntime,
    RuntimeServices,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import action_step
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.models import ExecutionContextModel
from pantaray_agents.agents.action_agent.runtime.models.conversation import (
    GoalConversationStateModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    build_next_action,
    build_pending_approval_request,
    build_tool_call,
    create_initial_state,
)
from pantaray_agents.application.action.approval_resume_restoration import (
    clear_current_approval_blocker,
)
from pantaray_agents.application.action.failure_recovery import (
    ActionRuntimeApplicationFailure,
)
from pantaray_agents.application.action.persistence import (
    ActionAgentPersistence,
)
from pantaray_agents.application.action.resume_service import ResumeStateError
from pantaray_agents.application.action.use_case_service import (
    ActionUseCaseDeps,
    ActionUseCaseService,
)
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.proxy_errors import build_llm_proxy_agent_error
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionResumeUserStep,
)
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    ActionExecutionResult,
)
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_WORKSPACE
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_agents.utils.trace_context import get_trace_context, set_trace_context
from pantaray_llm.errors import LlmProxyExecutionError


def _runtime_services(action_use_case: ActionUseCaseService) -> RuntimeServices:
    return action_use_case._runtime_services  # noqa: SLF001


def _stub_runtime_services() -> RuntimeServices:
    return RuntimeServices.model_construct(
        resume=MagicMock(),
        cancellation=MagicMock(),
        response=MagicMock(),
        rendering=MagicMock(),
        token_accounting=MagicMock(),
    )


def _test_execution_context() -> ExecutionContextModel:
    return ExecutionContextModel(
        manifest_id="manifest:action-1",
        execution_session_id="session-1",
        execution_network_policy="cloud-proxy-only",
        action_temp_dir="/tmp/action-1",
        app_runtime_python="/usr/bin/python3",
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    )


def _set_paused_goal_worker_turn(state, goal_id: str) -> None:
    goals = state["context"].setdefault("goals", [])
    if not any(goal.get("id") == goal_id for goal in goals):
        goals.append(
            {
                "id": goal_id,
                "check_item_id": "check-1",
                "title": "Goal",
                "status": "open",
                "dependencies": [],
            }
        )
    running = start_goal_worker_turn(
        GoalConversationStateModel(goal_id=goal_id),
        generation=1,
        started_at="2026-03-20T10:00:00Z",
        targeted=False,
    )
    state["goal_conversations"][goal_id] = pause_goal_worker_turn(running)


@pytest.fixture(autouse=True)
def setup_env_vars():
    """テスト全体で必要な環境変数を設定するフィクスチャ"""
    with patch.dict(
        os.environ,
        {
            "LLM_PROXY_URL": "https://llm-proxy.test",
        },
    ):
        yield


@pytest.fixture(autouse=True)
def clear_mock_data():
    """各テストの前にモックデータをクリアするフィクスチャ"""
    MockRepository.clear_data()
    yield
    MockRepository.clear_data()


@pytest.fixture
def mock_action_repository() -> MockActionAgentRepository:
    """MockActionAgentRepositoryのフィクスチャ"""
    return MockActionAgentRepository()


def _assert_checkpoint_matches_runtime_subset(
    restored_state: dict[str, object],
    runtime_state: dict[str, object],
) -> None:
    for field_name in (
        "action_id",
        "suggestion_id",
        "user_id",
        "phase",
        "step",
        "max_steps",
        "max_tool_steps",
        "status",
        "context",
        "history_by_scope",
    ):
        assert restored_state[field_name] == runtime_state[field_name]
    for optional_field in (
        "next_action",
        "pending_approval_request",
        "manifest_id",
        "execution_session_id",
        "execution_network_policy",
        "action_temp_dir",
        "app_runtime_python",
    ):
        assert restored_state.get(optional_field) == runtime_state.get(optional_field)


@pytest.mark.asyncio
async def test_execute_action_runtime_sets_trace_context_for_action_runtime(
    action_use_case: ActionUseCaseService,
) -> None:
    set_trace_context(None)
    request = build_action_request(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
    )
    observed: dict[str, object] = {}

    async def _fake_run_graph(**kwargs):
        del kwargs
        trace_context = get_trace_context()
        assert trace_context is not None
        observed["user_id"] = trace_context.user_id
        observed["action_id"] = trace_context.action_id
        observed["suggestion_id"] = trace_context.suggestion_id
        observed["request_id"] = trace_context.request_id
        return {"status": "success"}

    action_use_case._execution_service.run_graph = _fake_run_graph  # type: ignore[method-assign]  # noqa: SLF001
    action_use_case._response_service.state_to_execution_result = MagicMock(  # noqa: SLF001
        return_value=MagicMock(spec=ActionExecutionResult)
    )

    await action_use_case.execute_action_runtime(
        request,
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
    )

    assert observed["user_id"] == "user-1"
    assert observed["action_id"] == "action-1"
    assert observed["suggestion_id"] == "suggestion-1"
    assert isinstance(observed["request_id"], str)
    assert observed["request_id"]
    assert get_trace_context() is None


@pytest.mark.asyncio
async def test_execute_action_runtime_maps_llm_proxy_error_to_structured_terminal_result(
    action_use_case: ActionUseCaseService,
) -> None:
    request = build_action_request(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
    )

    async def _raise_proxy_error(**kwargs):
        del kwargs
        proxy_error = LlmProxyExecutionError(
            error_code="PROXY_UPSTREAM_FORBIDDEN",
            error_message=(
                "blocked by upstream: https://signed.example.invalid/path?token=secret"
            ),
            retryable=False,
            local_job_id="job-1",
            profile_id="action.planning",
            upstream_provider="openai",
            upstream_status_code=403,
            upstream_code="blocked",
            upstream_request_id="up-1",
        )
        raise ActionRuntimeApplicationFailure(
            stage="execute",
            error=build_llm_proxy_agent_error(
                exception=proxy_error,
                error_code_prefix="ACTION",
            ),
            execution_session_id=None,
        )

    action_use_case._execution_service.run_graph = _raise_proxy_error  # type: ignore[method-assign]  # noqa: SLF001

    result = await action_use_case.execute_action_runtime(
        request,
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
    )

    assert result.run_result.status == "error"
    assert result.run_result.action_failure_code == "ACTION_LLM_RESPONSE_ERROR"
    assert result.run_result.error_payload is not None
    assert result.run_result.error_payload["error_code"] == "ACTION_LLM_RESPONSE_ERROR"
    assert result.run_result.error_payload["error_message"] == (
        "blocked by upstream: <redacted_url>"
    )
    assert result.run_result.error_payload["error_details"]["local_job_id"] == "job-1"
    assert result.run_result.error_payload["error_details"]["profile_id"] == (
        "action.planning"
    )


@pytest.mark.asyncio
async def test_stream_action_redacts_proxy_error_in_public_response(
    action_use_case: ActionUseCaseService,
) -> None:
    request = build_action_request(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
    )
    emit_error = AsyncMock()

    async def _raise_proxy_error(**kwargs):
        del kwargs
        proxy_error = LlmProxyExecutionError(
            error_code="PROXY_UPSTREAM_FORBIDDEN",
            error_message="blocked by upstream",
            retryable=False,
            local_job_id="job-1",
            profile_id="action.planning",
            upstream_provider="openai",
            upstream_status_code=403,
            upstream_code="blocked",
            upstream_request_id="up-1",
        )
        raise ActionRuntimeApplicationFailure(
            stage="execute",
            error=build_llm_proxy_agent_error(
                exception=proxy_error,
                error_code_prefix="ACTION",
            ),
            execution_session_id=None,
        )

    action_use_case._execution_service.run_graph = _raise_proxy_error  # type: ignore[method-assign]  # noqa: SLF001

    response = await action_use_case.stream_action(
        request,
        emit_action_step=AsyncMock(),
        emit_error=emit_error,
    )

    assert response.status == "error"
    assert response.error is not None
    assert response.error.error_code == "ACTION_LLM_RESPONSE_ERROR"
    assert response.error.error_message == (
        "The operation failed due to an internal error."
    )
    assert response.error.error_details is None
    assert response.error.metadata is None
    emit_error.assert_awaited_once()
    emitted_payload = emit_error.await_args.args[0]
    assert emitted_payload["error_code"] == "ACTION_LLM_RESPONSE_ERROR"
    assert emitted_payload["error_message"] == (
        "The operation failed due to an internal error."
    )
    assert emitted_payload["error_details"] is None


@pytest.fixture
def mock_llm_client() -> MockLLMClient:
    """MockLLMClientのフィクスチャ

    executing THINK は native tool call を期待する。
    """
    client = MockLLMClient()
    # executing THINK 用のデフォルト応答
    default_thinking_response = (
        "{"
        '"thinking_summary":"Inspecting the request.",'
        '"tool_id":"thinking",'
        '"args":{"query":"Test query"}'
        "}"
    )
    client.responses["action"] = default_thinking_response
    return client


@pytest.fixture
def action_agent(
    mock_action_repository: MockActionAgentRepository,
    mock_llm_client: MockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ActionAgent:
    """ActionAgentのテストインスタンス"""
    db_path = create_local_runtime_db(db_path=tmp_path / "runtime.db")
    install_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)

    def _fake_load_config(prompt_name: str) -> PromptConfig:
        # ActionAgent.__init__ が load_config する prompt を全部返す
        if prompt_name == "action/executing":
            return PromptConfig(prompt="{current_time}", system_instruction="SYS")
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_action_repository,
        )
        agent.client = mock_llm_client  # google.genai.Client のパッチを削除し、直接モッククライアントを設定
        ActionUseCaseService(ActionUseCaseDeps(agent=agent))
        yield agent


@pytest.fixture
def action_use_case(action_agent: ActionAgent) -> ActionUseCaseService:
    return ActionUseCaseService(ActionUseCaseDeps(agent=action_agent))


def test_action_agent_requires_repository() -> None:
    with pytest.raises(ValueError):
        ActionAgent(config={}, repository=None)


def test_graph_route_from_resume_uses_action_for_persisted_supervisor_tool_call(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id="memory_search",
            args={"query": "resume"},
        ),
        decided_at="2026-03-20T10:00:05Z",
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "max_tool_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "max_parallel_memory_queries": 1,
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )

    route = runtime.route_from_resume(state)

    assert route == "action"
    assert route in RESUME_ROUTE_TO_NODE


def test_graph_route_from_resume_rejects_persisted_goal_worker_dispatch(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = True
    state["next_action"] = build_next_action(
        tool=build_tool_call(tool_id="run_next_goal", args={}),
        decided_at="2026-03-20T10:00:05Z",
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "max_tool_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "max_parallel_memory_queries": 1,
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )

    with pytest.raises(ResumeStateError, match="parent-only graph"):
        runtime.route_from_resume(state)


def test_graph_route_from_resume_uses_action_for_run_next_goal_when_goal_workers_disabled(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    state["next_action"] = build_next_action(
        tool=build_tool_call(tool_id="run_next_goal", args={}),
        decided_at="2026-03-20T10:00:05Z",
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "max_tool_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "max_parallel_memory_queries": 1,
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )

    assert runtime.route_from_resume(state) == "action"


def test_graph_route_from_resume_rejects_malformed_goal_worker_mode(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = "true"
    state["next_action"] = build_next_action(
        tool=build_tool_call(tool_id="run_next_goal", args={}),
        decided_at="2026-03-20T10:00:05Z",
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "max_tool_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "max_parallel_memory_queries": 1,
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )

    with pytest.raises(ResumeStateError, match="parent-only graph"):
        runtime.route_from_resume(state)


def test_resume_route_map_does_not_include_pause() -> None:
    """resume ルート集合に pause が含まれないことを保証する。"""

    assert "pause" not in RESUME_ROUTE_TO_NODE


def test_route_after_action_returns_halt_when_approval_pending(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    """承認待ち中は次推論へ進まず停止ルートへ分岐する。"""

    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["pending_approval_request"] = build_pending_approval_request(
        owner="supervisor",
        tool_id="bash",
        args={"command": "pwd"},
        approval_session_id="approval-1",
        tool_request_id="request-1",
        requested_at="2026-03-20T10:00:05Z",
        intent_class="process_exec_local",
        command_summary={"kind": "bash", "command": "pwd"},
    )
    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "max_tool_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )

    assert runtime.route_after_action(state) == "halt"


def test_route_after_action_returns_halt_when_an_unselected_blocker_remains(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["current_approval_blockers"] = [
        build_pending_approval_request(
            owner="goal_worker",
            tool_id="bash",
            args={"command": "pwd"},
            approval_session_id="approval-2",
            tool_request_id="request-2",
            requested_at="2026-03-20T10:00:05Z",
            intent_class="process_exec_local",
            command_summary={"kind": "bash", "command": "pwd"},
            goal_id="goal-2",
        )
    ]
    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "max_tool_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )

    assert runtime.route_after_action(state) == "halt"


@pytest.mark.parametrize(
    ("selected_session_id", "selected_request_id", "remaining_request_id"),
    [
        ("approval-1", "request-1", "request-2"),
        ("approval-2", "request-2", "request-1"),
    ],
)
def test_resume_normalizer_consumes_only_the_selected_blocker(
    selected_session_id: str,
    selected_request_id: str,
    remaining_request_id: str,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state["current_approval_blockers"] = [
        build_pending_approval_request(
            owner="goal_worker",
            tool_id="bash",
            args={"command": request_id},
            approval_session_id=session_id,
            tool_request_id=request_id,
            requested_at="2026-03-20T10:00:05Z",
            intent_class="process_exec_local",
            command_summary={"kind": "bash", "command": request_id},
            goal_id=goal_id,
        )
        for session_id, request_id, goal_id in (
            ("approval-1", "request-1", "goal-1"),
            ("approval-2", "request-2", "goal-2"),
        )
    ]

    clear_current_approval_blocker(
        state,
        approval_session_id=selected_session_id,
        tool_request_id=selected_request_id,
    )

    assert [
        blocker.tool_request_id for blocker in state["current_approval_blockers"]
    ] == [remaining_request_id]


@pytest.mark.asyncio
async def test_execution_think_persists_checkpoint_matching_returned_state(
    action_agent: ActionAgent,
    mock_action_repository: MockActionAgentRepository,
    action_use_case: ActionUseCaseService,
) -> None:
    await mock_action_repository.upsert_action_header(
        action_id="action-1",
        user_id="user-1",
        suggestion_id="suggestion-1",
        prompt_name="action/executing",
        prompt_version="1.0",
        status=StatusType.PROCESSING.value,
    )
    action_use_case._cancellation_service.check_cancellation = AsyncMock(  # noqa: SLF001
        return_value=False
    )
    action_agent._generate_llm_action_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=native_tool_turn(
            "memory_search",
            {"query": "resume"},
        )
    )
    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
        execution_context=_test_execution_context(),
    )
    state = project_request_user_step(state, runtime.request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    state["context"]["read_access_scope"] = READ_ACCESS_SCOPE_WORKSPACE

    updated_state = await runtime.think(state)

    saved_step = mock_action_repository.data["action_steps"][-1]
    restored_state = restore_runtime_state_checkpoint(
        saved_step["runtime_state_checkpoint"],
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    _assert_checkpoint_matches_runtime_subset(restored_state, updated_state)


@pytest.mark.asyncio
async def test_action_step_persists_checkpoint_matching_returned_state_on_success(
    action_agent: ActionAgent,
    mock_action_repository: MockActionAgentRepository,
    action_use_case: ActionUseCaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await mock_action_repository.upsert_action_header(
        action_id="action-1",
        user_id="user-1",
        suggestion_id="suggestion-1",
        prompt_name="action/executing",
        prompt_version="1.0",
        status=StatusType.PROCESSING.value,
    )
    action_use_case._cancellation_service.check_cancellation = AsyncMock(  # noqa: SLF001
        return_value=False
    )
    action_agent._generate_llm_response = AsyncMock(  # type: ignore[method-assign]
        return_value="<thinking>Reasoning</thinking><answer>Done</answer>"
    )
    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=("thinking",),
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id="thinking",
            args={"query": "checkpoint test"},
        ),
        decided_at="2026-03-20T10:00:02Z",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    saved_step = mock_action_repository.data["action_steps"][-1]
    restored_state = restore_runtime_state_checkpoint(
        saved_step["runtime_state_checkpoint"],
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    _assert_checkpoint_matches_runtime_subset(restored_state, updated_state)


@pytest.mark.asyncio
async def test_action_step_persists_checkpoint_matching_returned_state_on_error(
    action_agent: ActionAgent,
    mock_action_repository: MockActionAgentRepository,
    action_use_case: ActionUseCaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await mock_action_repository.upsert_action_header(
        action_id="action-1",
        user_id="user-1",
        suggestion_id="suggestion-1",
        prompt_name="action/executing",
        prompt_version="1.0",
        status=StatusType.PROCESSING.value,
    )
    action_use_case._cancellation_service.check_cancellation = AsyncMock(  # noqa: SLF001
        return_value=False
    )

    async def _always_fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("tool failed")

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        _always_fail,
    )
    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="action-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
        ),
        state_config={
            "max_steps": 12,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_use_case),
    )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=("thinking",),
    )
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id="thinking",
            args={"query": "checkpoint test"},
        ),
        decided_at="2026-03-20T10:00:02Z",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    saved_step = mock_action_repository.data["action_steps"][-1]
    restored_state = restore_runtime_state_checkpoint(
        saved_step["runtime_state_checkpoint"],
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    _assert_checkpoint_matches_runtime_subset(restored_state, updated_state)


@pytest.mark.asyncio
async def test_run_graph_resumes_from_latest_runtime_checkpoint(
    action_agent: ActionAgent,
    mock_action_repository: MockActionAgentRepository,
    action_use_case: ActionUseCaseService,
) -> None:
    request = build_action_request(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
    )
    await mock_action_repository.upsert_action_header(
        action_id="action-1",
        user_id="user-1",
        suggestion_id="suggestion-1",
        prompt_name="action/executing",
        prompt_version="1.0",
        status=StatusType.PROCESSING.value,
    )
    resumed_state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=12,
        max_tool_steps=8,
        token_budget=None,
    )
    resumed_state["phase"] = "executing"
    resumed_state["step"] = 4
    resumed_state["history_by_scope"] = {
        "S": [
            {
                "step_id": request.user_step_id,
                "step_number": 1,
                "phase": "init",
                "step_type": "user_request",
                "summary": "",
                "user_request_text": request.user_message.content,
                "tool_id": None,
                "started_at": request.user_step_created_at,
                "completed_at": request.user_step_created_at,
                "short_step_id": "S-1-USER",
            },
            {
                "step_id": "step-1",
                "step_number": 3,
                "phase": "executing",
                "step_type": "llm_output",
                "summary": "Selecting next tool",
                "tool_id": "memory_search",
                "started_at": "2026-03-20T10:00:01Z",
                "completed_at": "2026-03-20T10:00:02Z",
                "short_step_id": "S-2-THINK",
            },
        ]
    }
    resumed_state["context"]["local_step_counters"] = {"S": 2}
    await mock_action_repository.save_action_step(
        step_id="resume-step",
        action_id="action-1",
        step_number=4,
        step_name="supervisor_think",
        step_type="llm_output",
        llm_prompt_text="prompt",
        llm_response_text='{"tool_call":{"tool_id":"memory_search","args":{}}}',
        runtime_state_checkpoint=build_runtime_state_checkpoint(resumed_state),
        runtime_state_checkpoint_version=RUNTIME_STATE_CHECKPOINT_VERSION,
        thinking="thought",
        status="success",
        user_id="user-1",
        goal_handle="S",
        short_step_id="S-2-THINK",
        local_step_number=2,
    )

    captured_states: list[dict] = []

    async def _fake_runner(state: dict) -> dict:
        captured_states.append(state)
        return state

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=_fake_runner,
    ):
        final_state = await action_use_case._entrypoint.run_graph(  # noqa: SLF001
            request,
            emit_action_step=lambda _event: AsyncMock()(),
            emit_error=lambda _event: AsyncMock()(),
        )

    assert captured_states[0]["step"] == 4
    assert captured_states[0]["phase"] == "executing"
    assert [
        entry["step_id"] for entry in captured_states[0]["history_by_scope"]["S"]
    ] == [request.user_step_id, "step-1"]
    assert final_state["step"] == 4


def test_state_to_response_uses_started_at_as_created_at() -> None:
    """created_at が updated_at に引きずられず、started_at を使うことを確認。"""
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "action_id": "act-1",
        "suggestion_id": "sug-1",
        "user_id": "user-1",
        "status": "success",
        "final_output": "ok",
        "started_at": "START",
        "updated_at": "END",
        "errors": [],
    }
    resp = persistence.state_to_response(state)  # type: ignore[arg-type]
    assert resp.created_at == "START"


@pytest.mark.parametrize(
    ("error_code", "expected_failure_stage"),
    [
        ("ACTION_ORCHESTRATION_MODE_RETIRED", "resume_failed"),
        ("ACTION_RESUME_FROM_FUTURE", "running_failed"),
    ],
)
def test_state_to_run_result_preserves_resume_error_without_failing(
    error_code: str,
    expected_failure_stage: str,
) -> None:
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "action_id": "act-retired",
        "suggestion_id": None,
        "user_id": "user-retired",
        "status": "error",
        "final_output": "",
        "started_at": "START",
        "updated_at": "END",
        "context": {},
        "errors": [
            {
                "error_type": "resume_error",
                "error_code": error_code,
                "error_message": "retired",
                "severity": "error",
            }
        ],
    }

    execution_result = persistence.state_to_execution_result(  # type: ignore[arg-type]
        state
    )
    result = execution_result.run_result

    assert result.action_failure_code == error_code
    assert result.failure_stage == expected_failure_stage
    assert result.failure_message_public == "Action execution failed."
    assert execution_result.runtime_state_checkpoint is None


def test_state_to_response_does_not_silently_drop_invalid_error_object() -> None:
    """status=error で errors がある場合、error の検証失敗を黙殺せず可観測な形で返す。"""
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "action_id": "act-err",
        "suggestion_id": "sug-err",
        "user_id": "user-err",
        "status": "error",
        "final_output": "",
        "started_at": "START",
        "updated_at": "END",
        # AgentError として必須フィールドが欠けているため ValidationError になる
        "errors": [{"oops": "broken"}],
    }
    resp = persistence.state_to_response(state)  # type: ignore[arg-type]
    assert resp.error is not None
    assert resp.error.error_code == "ACTION_AGENT_ERROR_PAYLOAD_INVALID"
    assert resp.error.error_details is None
    assert resp.error.metadata is None


def test_state_to_response_redacts_error_details_for_client() -> None:
    """クライアント向けレスポンスでは error_details/metadata を露出しない。"""
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "action_id": "act-err",
        "suggestion_id": "sug-err",
        "user_id": "user-err",
        "status": "error",
        "final_output": "",
        "started_at": "START",
        "updated_at": "END",
        "errors": [
            {
                "error_type": "validation_error",
                "error_code": "ACTION_TOOL_ARGS_INVALID",
                "error_message": "tool args invalid",
                "error_details": {"tool_id": "close_goal", "validation": {"x": 1}},
                "severity": "error",
                "metadata": {"action_id": "act-err"},
            }
        ],
    }

    resp = persistence.state_to_response(state)  # type: ignore[arg-type]
    assert resp.error is not None
    assert resp.error.error_code == "ACTION_TOOL_ARGS_INVALID"
    assert resp.error.error_details is None
    assert resp.error.metadata is None

    persisted = persistence.state_to_response(  # type: ignore[arg-type]
        state,
        redact_for_client=False,
    )
    assert persisted.error is not None
    assert persisted.error.error_details == {
        "tool_id": "close_goal",
        "validation": {"x": 1},
    }
    assert persisted.error.metadata == {"action_id": "act-err"}


def test_state_to_response_prefers_terminal_error_after_recoverable_warning() -> None:
    """先行する再試行可能 warning ではなく、後続の terminal error を返す。"""
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "action_id": "act-err",
        "suggestion_id": "sug-err",
        "user_id": "user-err",
        "status": "error",
        "final_output": "",
        "started_at": "START",
        "updated_at": "END",
        "errors": [
            {
                "error_type": "validation_error",
                "error_code": "ACTION_TOOL_ARGS_INVALID",
                "error_message": "tool args invalid",
                "error_details": None,
                "severity": "warning",
                "metadata": None,
            },
            {
                "error_type": "llm_api_error",
                "error_code": "ACTION_LLM_REQUEST_FAILED",
                "error_message": "request failed",
                "error_details": None,
                "severity": "error",
                "metadata": None,
            },
        ],
    }

    response = persistence.state_to_response(  # type: ignore[arg-type]
        state,
        redact_for_client=False,
    )

    assert response.error is not None
    assert response.error.error_code == "ACTION_LLM_REQUEST_FAILED"


def test_state_to_execution_result_ignores_bool_token_counters() -> None:
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "action_id": "act-bool-counts",
        "suggestion_id": "sug-bool-counts",
        "user_id": "user-bool-counts",
        "status": "processing",
        "final_output": "ok",
        "started_at": "START",
        "updated_at": "END",
        "step": 1,
        "steps_taken": True,
        "llm_steps_taken": True,
        "tool_steps_taken": True,
        "total_prompt_tokens": True,
        "total_completion_tokens": True,
        "errors": [],
    }

    result = persistence.state_to_execution_result(state)  # type: ignore[arg-type]

    assert result.run_result.total_steps is None
    assert result.run_result.total_llm_steps is None
    assert result.run_result.total_tool_steps is None
    assert result.run_result.total_prompt_tokens is None
    assert result.run_result.total_completion_tokens is None


def test_state_to_execution_result_allows_processing_pause_result() -> None:
    persistence = ActionAgentPersistence(
        default_prompt_name="action/executing",
        default_prompt_version="1.0",
        now_provider=lambda: "NOW",
    )
    state = {
        "manifest_id": "manifest:act-approval-pause",
        "action_id": "act-approval-pause",
        "suggestion_id": "sug-approval-pause",
        "user_id": "user-approval-pause",
        "status": "processing",
        "final_output": "",
        "started_at": "START",
        "updated_at": "PAUSED",
        "step": 1,
        "execution_session_id": "session-paused",
        "errors": [],
        "pending_approval_request": {
            "owner": "goal_worker",
            "tool_call": {
                "tool_id": "bash",
                "args": {},
            },
            "approval_session_id": "approval-1",
            "tool_request_id": "tool-request-1",
            "requested_at": "PAUSED",
            "intent_class": "process_exec_local",
            "command_summary": {"summary": "Run bash command"},
        },
    }

    result = persistence.state_to_execution_result(state)  # type: ignore[arg-type]

    assert result.run_result.status == "processing"
    assert result.run_result.completed_at == "PAUSED"
    assert result.run_result.final_output == ""
    assert result.execution_session_id == "session-paused"
    assert result.runtime_state_checkpoint is None


@pytest.mark.asyncio
async def test_action_agent_repository_error_handling(
    action_agent: ActionAgent,
    mock_action_repository: MockActionAgentRepository,
):
    """リポジトリのエラーハンドリングをテストします

    save_action_step のエラーは LangGraph 内で発生するため、
    このテストでは set_next_save_action_error の設定が正しく機能することを確認する。
    """
    # 保存エラーを設定
    mock_action_repository.set_next_save_action_error("DB Save Failed")

    # エラーが設定されていることを確認
    # 実際の save_action 呼び出しは LangGraph フロー内で行われるため、
    # ここではモックの設定が正しく行われていることのみを確認
    assert mock_action_repository._next_save_action_error == "DB Save Failed"


@pytest.mark.asyncio
async def test_validate_request_other_pydantic_model(
    action_agent: ActionAgent,
):
    """_validate_request に ActionAgentRequest 以外の Pydantic モデルを渡すテスト"""
    from unittest.mock import MagicMock

    # ActionAgentRequest の必須フィールドを含む辞書を作成
    wrong_request_dict = build_action_request(
        action_id="act-dict-test",
        suggestion_id="sug-dict-test",
        user_id="user-dict-test",
    ).model_dump()
    wrong_request_dict["extra_field_from_suggestion"] = "some_value"

    # 辞書を AgentRequest 型として渡すために MagicMock でラップする
    mock_request = MagicMock()
    mock_request.model_dump = MagicMock(return_value=wrong_request_dict)

    try:
        validated = await action_agent._validate_request(mock_request)
        assert isinstance(validated, ActionAgentRequest)
        assert validated.action_id == "act-dict-test"
        assert not hasattr(validated, "extra_field_from_suggestion")
    except TypeError as e:
        pytest.fail(f"TypeError raised unexpectedly: {e}")
    except ValidationError as e:
        pytest.fail(f"ValidationError raised unexpectedly: {e}")


@pytest.mark.asyncio
async def test_create_success_response_raises_not_implemented(
    action_agent: ActionAgent,
):
    """_create_success_response は LangGraph 実装では NotImplementedError を投げることを確認

    LangGraph 実装では _state_to_response を使用するため、
    BaseAgent 互換の _create_success_response は使用しない。
    """
    request = build_action_request(
        action_id="act-test-1",
        suggestion_id="sug-test-1",
        user_id="user-test-1",
    )
    extracted_data = {"thinking": "Test thinking", "answer": "Test answer"}

    with pytest.raises(NotImplementedError):
        action_agent._create_success_response(request, extracted_data)


@pytest.mark.asyncio
async def test_run_graph_requires_existing_parent_record_and_persists_final_state(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
    monkeypatch,
) -> None:
    """_run_graph は既存 header 前提で動作し、terminal 保存は行わない。"""

    async def dummy_runner(state):
        return state

    # build_action_agent_graph は agent.py 内でインポートされているため、そのパスをモック
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        lambda runtime: dummy_runner,
    )

    await action_agent.repository.upsert_action_header(
        action_id="act-123",
        user_id="user-123",
        suggestion_id="sug-123",
        prompt_name="action/executing",
        prompt_version="1.0",
    )
    save_action_mock = AsyncMock(
        return_value=RepositoryResult(data={"action_id": "act-123"})
    )
    monkeypatch.setattr(action_agent.repository, "save_action", save_action_mock)

    request = build_action_request(
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    async def noop(_event):
        return None

    await action_use_case._entrypoint.run_graph(  # noqa: SLF001
        request,
        emit_action_step=noop,
        emit_error=noop,
    )

    existing = await action_agent.repository.get_action(
        user_id="user-123",
        action_id="act-123",
    )
    assert existing.data is not None
    save_action_mock.assert_not_awaited()


def test_resolve_initial_state_overrides_runtime_config_fields_on_checkpoint_resume(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    checkpoint_state = project_persisted_user_request_step(
        checkpoint_state,
        step_id="step:message:act-resume",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="Test Action request.",
        occurred_at="2026-03-20T10:00:00Z",
        history_phase="init",
    )

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-resume",
            suggestion_id="sug-resume",
            user_id="user-resume",
        ),
        started_at="2026-03-21T09:00:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(
                    checkpoint_state
                ),
            }
        ),
    )

    assert resolved["step"] == 2
    assert resolved["max_steps"] == 25
    assert resolved["max_tool_steps"] == 26
    assert resolved["token_budget"] == 99
    assert resolved["cancel_check_max_consecutive_failures"] == 3
    assert resolved["cancel_check_failure_grace_seconds"] == 60


@pytest.mark.parametrize(
    ("checkpoint_status", "action_temp_dir"),
    [
        ("success", "/tmp/action-resume"),
        ("processing", "/tmp/action-resume"),
        ("processing", "/tmp/.runtime-temp"),
    ],
)
def test_resolve_initial_state_opens_prior_checkpoint_for_persisted_next_user_turn(
    action_use_case: ActionUseCaseService,
    checkpoint_status: str,
    action_temp_dir: str,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    checkpoint_state = project_persisted_user_request_step(
        checkpoint_state,
        step_id="user-step-1",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="First request",
        occurred_at="2026-03-20T10:00:00Z",
        history_phase="init",
    )
    checkpoint_state["phase"] = "finalizing"
    checkpoint_state["status"] = checkpoint_status
    checkpoint_state["context"]["use_goal_workers"] = True
    checkpoint_state["final_output"] = (
        "First answer" if checkpoint_status == "success" else None
    )
    checkpoint_state["manifest_id"] = "old-manifest"
    checkpoint_state["execution_session_id"] = "old-session"
    checkpoint_state["execution_network_policy"] = "restricted"
    checkpoint_state["action_temp_dir"] = action_temp_dir
    checkpoint_state["app_runtime_python"] = "/usr/bin/python3"
    checkpoint_state["read_access_scope"] = "workspace"
    checkpoint_state["tokens_used"] = 7
    checkpoint_state["total_prompt_tokens"] = 7

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-resume",
            suggestion_id="sug-resume",
            user_id="user-resume",
            content="Third request",
            message_id="message-3",
            user_step_id="user-step-3",
            user_step_number=3,
            user_step_local_step_number=3,
            user_step_created_at="2026-03-21T09:00:00Z",
        ),
        started_at="2026-03-21T09:00:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(
                    checkpoint_state
                ),
            }
        ),
        intervening_user_steps=(
            ActionResumeUserStep(
                step_id="user-step-2",
                step_number=2,
                local_step_number=2,
                short_step_id="S-2-USER",
                message=ActionUserMessageInput(
                    message_id="message-2", content="Second request"
                ),
                created_at="2026-03-20T11:00:00Z",
            ),
        ),
    )

    if action_temp_dir.endswith("/.runtime-temp"):
        assert resolved["status"] == "error"
        assert resolved["errors"][0]["error_code"] == "ACTION_RESUME_CHECKPOINT_INVALID"
        return

    assert resolved["phase"] == "init"
    assert resolved["status"] == "processing"
    assert resolved["step"] == 3
    assert resolved["tokens_used"] == 7
    assert resolved["final_output"] is None
    assert "use_goal_workers" not in resolved["context"]
    assert [entry["step_id"] for entry in resolved["history_by_scope"]["S"]] == [
        "user-step-1",
        "user-step-2",
    ]
    assert resolved["suggestion_id"] == "sug-resume"
    assert "manifest_id" not in resolved
    assert "execution_session_id" not in resolved


def test_resolve_initial_state_rejects_new_user_turn_from_active_checkpoint(
    action_use_case: ActionUseCaseService,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id=None,
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-resume",
            suggestion_id=None,
            user_id="user-resume",
            content="Second request",
            message_id="message-2",
            user_step_id="user-step-2",
        ),
        started_at="2026-03-21T09:00:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(
                    checkpoint_state
                ),
            }
        ),
    )

    assert resolved["phase"] == "finalizing"
    assert resolved["status"] == "error"
    assert resolved["errors"][0]["error_code"] == "ACTION_RESUME_NOT_ALLOWED"


def test_resolve_initial_state_rejects_v3_checkpoint_without_conversion(
    action_use_case: ActionUseCaseService,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-resume",
            suggestion_id="sug-resume",
            user_id="user-resume",
        ),
        started_at="2026-03-21T09:00:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": 3,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(
                    checkpoint_state
                ),
            }
        ),
    )

    assert resolved["status"] == "error"
    assert resolved["phase"] == "finalizing"
    assert resolved["errors"][0]["error_code"] == "ACTION_RESUME_CHECKPOINT_INVALID"
    assert (
        "Unsupported action runtime checkpoint version: 3"
        in resolved["errors"][0]["error_message"]
    )


def test_resolve_initial_state_rejects_goal_worker_tool_call_after_approval(
    action_use_case: ActionUseCaseService,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    pending_request = build_pending_approval_request(
        owner="goal_worker",
        tool_id="read",
        args={"path": "/tmp/input.txt"},
        approval_session_id="approval-1",
        tool_request_id="request-1",
        requested_at="2026-03-20T10:00:05Z",
        intent_class="read_local",
        command_summary={"kind": "read", "path": "/tmp/input.txt"},
        thinking="thought",
        thinking_summary="summary",
        goal_id="goal-1",
    )
    checkpoint_state["pending_approval_request"] = pending_request
    checkpoint_state["current_approval_blockers"] = [pending_request]
    checkpoint_state["context"]["goal_worker_current_goal_id"] = "goal-1"
    checkpoint_state["context"]["approval_resume_tool_request_id"] = "request-1"
    _set_paused_goal_worker_turn(checkpoint_state, "goal-1")

    with patch(
        "pantaray_agents.agents.action_agent.agent.load_runtime_approval_session_by_request",
        return_value=SimpleNamespace(
            status="approved_once",
            approval_session_id="approval-1",
        ),
    ):
        resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
            request=build_action_request(
                action_id="act-resume",
                suggestion_id="sug-resume",
                user_id="user-resume",
                approval_resume_session_id="approval-1",
                approval_resume_tool_request_id="request-1",
            ),
            started_at="2026-03-21T09:00:00Z",
            state_config={
                "max_steps": 25,
                "max_tool_steps": 26,
                "token_budget": 99,
                "prompt_name": "action/executing",
                "prompt_version": "1.0",
                "cancel_check_max_consecutive_failures": 3,
                "cancel_check_failure_grace_seconds": 60,
            },
            token_budget=99,
            checkpoint_row=RepositoryResult(
                data={
                    "runtime_state_checkpoint_version": (
                        RUNTIME_STATE_CHECKPOINT_VERSION
                    ),
                    "runtime_state_checkpoint": build_runtime_state_checkpoint(
                        checkpoint_state
                    ),
                }
            ),
        )

    assert resolved["status"] == "error"
    assert resolved["phase"] == "finalizing"
    assert resolved["errors"][0]["error_code"] == "ACTION_RESUME_APPROVAL_STATE_INVALID"


def test_resolve_initial_state_rejects_goal_worker_tool_call_after_denial(
    action_use_case: ActionUseCaseService,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    pending_request = build_pending_approval_request(
        owner="goal_worker",
        tool_id="read",
        args={"path": "/tmp/input.txt"},
        approval_session_id="approval-1",
        tool_request_id="request-1",
        requested_at="2026-03-20T10:00:05Z",
        intent_class="read_local",
        command_summary={"kind": "read", "path": "/tmp/input.txt"},
        thinking="thought",
        thinking_summary="summary",
        goal_id="goal-1",
    )
    checkpoint_state["pending_approval_request"] = pending_request
    checkpoint_state["current_approval_blockers"] = [pending_request]
    checkpoint_state["context"]["goal_worker_current_goal_id"] = "goal-1"
    checkpoint_state["context"]["approval_resume_tool_request_id"] = "request-1"
    _set_paused_goal_worker_turn(checkpoint_state, "goal-1")

    with patch(
        "pantaray_agents.agents.action_agent.agent.load_runtime_approval_session_by_request",
        return_value=SimpleNamespace(
            status="denied",
            approval_session_id="approval-1",
        ),
    ):
        resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
            request=build_action_request(
                action_id="act-resume",
                suggestion_id="sug-resume",
                user_id="user-resume",
                approval_resume_session_id="approval-1",
                approval_resume_tool_request_id="request-1",
            ),
            started_at="2026-03-21T09:00:00Z",
            state_config={
                "max_steps": 25,
                "max_tool_steps": 26,
                "token_budget": 99,
                "prompt_name": "action/executing",
                "prompt_version": "1.0",
                "cancel_check_max_consecutive_failures": 3,
                "cancel_check_failure_grace_seconds": 60,
            },
            token_budget=99,
            checkpoint_row=RepositoryResult(
                data={
                    "runtime_state_checkpoint_version": (
                        RUNTIME_STATE_CHECKPOINT_VERSION
                    ),
                    "runtime_state_checkpoint": build_runtime_state_checkpoint(
                        checkpoint_state
                    ),
                }
            ),
        )

    assert resolved["status"] == "error"
    assert resolved["phase"] == "finalizing"
    assert resolved["errors"][0]["error_code"] == "ACTION_RESUME_APPROVAL_STATE_INVALID"


def test_resolve_initial_state_does_not_restore_already_claimed_approval_request(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    claimed_pending_request = build_pending_approval_request(
        owner="supervisor",
        tool_id="read",
        args={"path": "/tmp/input.txt"},
        approval_session_id="approval-1",
        tool_request_id="request-1",
        requested_at="2026-03-20T10:00:05Z",
        intent_class="read_local",
        command_summary={"kind": "read", "path": "/tmp/input.txt"},
        thinking="thought",
        thinking_summary="summary",
    )
    checkpoint_state["pending_approval_request"] = claimed_pending_request
    checkpoint_state["current_approval_blockers"] = [claimed_pending_request]
    checkpoint_state["context"]["approval_resume_tool_request_id"] = "request-1"

    with patch(
        "pantaray_agents.agents.action_agent.agent.load_runtime_approval_session_by_request",
        return_value=SimpleNamespace(
            status="approved_once",
            approval_session_id="approval-1",
            claimed_at="2026-03-21T09:00:00Z",
        ),
    ):
        resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
            request=build_action_request(
                action_id="act-resume",
                suggestion_id="sug-resume",
                user_id="user-resume",
                approval_resume_session_id="approval-1",
                approval_resume_tool_request_id="request-1",
            ),
            started_at="2026-03-21T09:00:00Z",
            state_config={
                "max_steps": 25,
                "max_tool_steps": 26,
                "token_budget": 99,
                "prompt_name": "action/executing",
                "prompt_version": "1.0",
                "cancel_check_max_consecutive_failures": 3,
                "cancel_check_failure_grace_seconds": 60,
            },
            token_budget=99,
            checkpoint_row=RepositoryResult(
                data={
                    "runtime_state_checkpoint_version": (
                        RUNTIME_STATE_CHECKPOINT_VERSION
                    ),
                    "runtime_state_checkpoint": build_runtime_state_checkpoint(
                        checkpoint_state
                    ),
                }
            ),
        )

    assert "pending_approval_request" not in resolved
    assert "current_approval_blockers" not in resolved
    assert "approval_resume_tool_request_id" not in resolved["context"]
    # 既に claim 済みの承認は再実行されない。
    assert resolved["next_action"] is None


@pytest.mark.parametrize(
    ("use_goal_workers", "phase"),
    [(True, "executing"), (False, "planning")],
)
def test_resolve_initial_state_terminalizes_retired_orchestration_checkpoint(
    action_use_case: ActionUseCaseService,
    use_goal_workers: bool,
    phase: str,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    checkpoint_state = project_persisted_user_request_step(
        checkpoint_state,
        step_id="step:message:act-resume",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="Test Action request.",
        occurred_at="2026-03-20T10:00:00Z",
        history_phase="init",
    )
    checkpoint_state["context"]["use_goal_workers"] = use_goal_workers
    checkpoint_state["phase"] = phase

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-resume",
            suggestion_id="sug-resume",
            user_id="user-resume",
        ),
        started_at="2026-03-21T09:00:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(
                    checkpoint_state
                ),
            }
        ),
    )

    assert resolved["status"] == "error"
    assert resolved["phase"] == "finalizing"
    assert resolved["errors"][0]["error_code"] == "ACTION_ORCHESTRATION_MODE_RETIRED"


@pytest.mark.parametrize(
    ("status", "final_output", "run_authority", "skip_persist"),
    [
        ("success", "Completed answer", "authoritative", False),
        ("processing", None, "superseded", False),
        ("processing", None, "authoritative", True),
    ],
)
def test_resolve_initial_state_preserves_nonresumable_retired_checkpoint(
    action_use_case: ActionUseCaseService,
    status: str,
    final_output: str | None,
    run_authority: str,
    skip_persist: bool,
) -> None:
    checkpoint_state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    checkpoint_state = project_persisted_user_request_step(
        checkpoint_state,
        step_id="step:message:act-resume",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="Test Action request.",
        occurred_at="2026-03-20T10:00:00Z",
        history_phase="init",
    )
    checkpoint_state["context"]["use_goal_workers"] = True
    checkpoint_state["phase"] = "finalizing"
    checkpoint_state["status"] = status  # type: ignore[typeddict-item]
    checkpoint_state["final_output"] = final_output
    checkpoint_state["run_authority"] = run_authority  # type: ignore[typeddict-item]
    checkpoint_state["skip_persist"] = skip_persist

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-resume",
            suggestion_id="sug-resume",
            user_id="user-resume",
        ),
        started_at="2026-03-21T09:00:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(
                    checkpoint_state
                ),
            }
        ),
    )

    assert resolved["status"] == status
    assert resolved["phase"] == "finalizing"
    assert resolved["final_output"] == final_output
    assert resolved["run_authority"] == run_authority
    assert resolved["skip_persist"] is skip_persist
    assert resolved["errors"] == []


def test_resolve_execution_session_terminal_status_skips_processing_state(
    action_agent: ActionAgent,
    action_use_case: ActionUseCaseService,
) -> None:
    state = create_initial_state(
        user_id="user-resume",
        suggestion_id="sug-resume",
        action_id="act-resume",
        started_at="2026-03-20T10:00:00Z",
        max_steps=3,
        max_tool_steps=4,
        token_budget=11,
    )
    state["execution_session_id"] = "exec-1"
    state["pending_approval_request"] = build_pending_approval_request(
        owner="supervisor",
        tool_id="bash",
        args={"command": "echo hi"},
        approval_session_id="approval-1",
        tool_request_id="request-1",
        requested_at="2026-03-20T10:00:05Z",
        intent_class="process_exec_local",
        command_summary={"kind": "bash", "command": "echo hi"},
    )

    assert (
        action_use_case._execution_service.resolve_execution_session_terminal_status(
            state
        )  # noqa: SLF001
        is None
    )

    state["status"] = "success"
    assert (
        action_use_case._execution_service.resolve_execution_session_terminal_status(
            state
        )  # noqa: SLF001
        == "completed"
    )


def test_prepare_failure_followup_reconstructs_assistant_before_first_user(
    action_use_case: ActionUseCaseService,
) -> None:
    from pantaray_agents.schema.agent.action_assistant_message import (
        ActionAssistantMessageStep,
    )

    request = build_action_request(
        action_id="act-resume",
        suggestion_id=None,
        user_id="user-resume",
        content="もう一度お願い",
        user_step_number=3,
        user_step_local_step_number=3,
    ).model_copy(
        update={
            "preceding_assistant_messages": (
                ActionAssistantMessageStep(
                    step_id="assistant-step",
                    step_number=1,
                    local_step_number=1,
                    short_step_id="S-1-ASSISTANT",
                    content="元の提案",
                    created_at="2026-09-09T00:00:00Z",
                ),
            )
        }
    )
    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=request,
        started_at="2026-09-09T00:00:03Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 26,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(data=None),
        intervening_user_steps=(
            ActionResumeUserStep(
                step_id="first-reply",
                step_number=2,
                local_step_number=2,
                short_step_id="S-2-USER",
                message=ActionUserMessageInput(
                    message_id="reply",
                    content="それについて教えて",
                ),
                created_at="2026-09-09T00:00:01Z",
            ),
        ),
    )
    assert [
        (entry["step_id"], entry["step_type"])
        for entry in resolved["history_by_scope"]["S"]
    ] == [
        ("assistant-step", "assistant_message"),
        ("first-reply", "user_request"),
    ]
    assert resolved["step"] == 3
    assert resolved["context"]["local_step_counters"]["S"] == 2


def test_checkpoint_resume_projects_later_assistant_before_opening_user_turn(
    action_use_case: ActionUseCaseService,
) -> None:
    from pantaray_agents.schema.agent.action_assistant_message import (
        ActionAssistantMessageStep,
    )

    previous = create_initial_state(
        user_id="user-resume",
        suggestion_id=None,
        action_id="act-resume",
        started_at="2026-09-09T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=99,
        execution_context=_test_execution_context(),
    )
    previous = project_persisted_user_request_step(
        previous,
        step_id="first-user",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="最初の依頼",
        occurred_at="2026-09-09T00:00:00Z",
        history_phase="init",
    )
    previous["status"] = "success"
    previous["phase"] = "finalizing"
    previous["final_output"] = "最初の回答"
    request = build_action_request(
        action_id="act-resume",
        suggestion_id=None,
        user_id="user-resume",
        content="その発言について教えて",
        user_step_number=3,
        user_step_local_step_number=3,
    ).model_copy(
        update={
            "preceding_assistant_messages": (
                ActionAssistantMessageStep(
                    step_id="later-assistant",
                    step_number=2,
                    local_step_number=2,
                    short_step_id="S-2-ASSISTANT",
                    content="チェックポイント後の発言",
                    created_at="2026-09-09T00:00:01Z",
                ),
            )
        }
    )
    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=request,
        started_at="2026-09-09T00:00:02Z",
        state_config={
            "max_steps": 10,
            "max_tool_steps": 10,
            "token_budget": 99,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=99,
        checkpoint_row=RepositoryResult(
            data={
                "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                "runtime_state_checkpoint": build_runtime_state_checkpoint(previous),
            }
        ),
    )
    assert resolved["status"] == "processing"
    assert (
        resolved["history_by_scope"]["S"][-1]["assistant_message_text"]
        == "チェックポイント後の発言"
    )
    assert resolved["step"] == request.user_step_number
