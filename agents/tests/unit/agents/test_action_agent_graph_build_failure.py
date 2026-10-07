from __future__ import annotations

# pylint: disable=import-error,protected-access
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn
from tests.unit.agents.action_runtime_failure_test_support import (
    build_action_use_case as _build_action_use_case,
)
from tests.unit.agents.action_runtime_failure_test_support import (
    build_mock_action_use_case as _build_mock_action_use_case,
)
from tests.unit.agents.action_runtime_failure_test_support import (
    build_repo as _build_repo,
)
from tests.unit.agents.action_runtime_failure_test_support import (
    install_local_runtime_test_db as _install_local_runtime_test_db,
)
from tests.unit.agents.action_runtime_failure_test_support import (
    request as _request,
)

from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    CounterInvariantError,
)
from pantaray_agents.local_runtime.tooling import (
    ensure_action_scratch_execution_context,
    load_execution_session,
)
from pantaray_agents.local_runtime.tooling.bootstrap import ActionExecutionContextError
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.repositories import action_runtime_resume_contract as resume
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_CONNECTION_NOT_CONFIGURED,
    PROXY_INSUFFICIENT_BALANCE,
    PROXY_INVALID_INPUT,
    PROXY_REQUEST_FAILED,
    LlmProxyExecutionError,
)


@pytest.mark.asyncio
async def test_llm_runtime_error_returns_typed_application_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)
    MockRepository.clear_data()
    repo = MockActionAgentRepository()
    llm_client = MockLLMClient()
    action_use_case = _build_mock_action_use_case(repo, llm_client=llm_client)
    await repo.save_suggestion(
        {
            "suggestion_id": "sug-llm-error",
            "user_id": "user-llm-error",
            "answer": "Test suggestion content.",
            "thinking": "Test suggestion thinking.",
            "accepted_at": "2026-01-01T00:00:00+00:00",
        }
    )
    llm_client.set_next_error(RuntimeError("LLM Error"))
    request = _request(
        action_id="act-llm-error",
        suggestion_id="sug-llm-error",
        user_id="user-llm-error",
    )
    await repo.upsert_action_header(
        action_id=request.action_id,
        user_id=request.user_id,
        suggestion_id=request.suggestion_id,
        prompt_name="action/executing",
        prompt_version="1.0",
    )

    try:
        response = await action_use_case.process(request)

        saved = await repo.get_action(
            user_id="user-llm-error",
            action_id=request.action_id,
        )
        assert saved.data is not None
        assert saved.data.get("status") == "processing"
        assert response.error is not None
        assert response.error.error_code == "ACTION_GRAPH_EXECUTION_FAILED"
    finally:
        MockRepository.clear_data()


@pytest.mark.asyncio
async def test_graph_build_failure_returns_typed_application_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-build-fail",
        suggestion_id="sug-build-fail",
        user_id="user-build-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-build-fail",
        suggestion_id="sug-build-fail",
        user_id="user-build-fail",
    )
    service = action_use_case._execution_service  # noqa: SLF001
    service.load_runtime_checkpoint_for_request = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            MagicMock(data=None, error=None),
            resume.ActionResumeUserStep(
                step_id="prior-user",
                step_number=1,
                local_step_number=1,
                short_step_id="S-1-USER",
                message=ActionUserMessageInput(
                    message_id="prior-message", content="Prior"
                ),
                created_at="2026-03-20T00:00:00Z",
            ),
        )
    )
    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        side_effect=RuntimeError("compile failed"),
    ):
        response = await action_use_case.process(request)

    repo.get_action.assert_awaited_once_with(
        user_id="user-build-fail",
        action_id="act-build-fail",
    )
    assert response.error is not None
    assert response.error.error_code == "ACTION_GRAPH_BUILD_FAILED"
    repo.update_action_status_if_processing.assert_not_awaited()
    repo.save_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_runner_failure_returns_typed_application_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-run-fail",
        suggestion_id="sug-run-fail",
        user_id="user-run-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-run-fail",
        suggestion_id="sug-run-fail",
        user_id="user-run-fail",
    )

    async def fail_runner(_state):
        raise RuntimeError("runner failed")

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        response = await action_use_case.process(request)

    assert response.error is not None
    assert response.error.error_code == "ACTION_GRAPH_EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_graph_runner_proxy_failure_returns_typed_error_and_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-proxy-fail",
        suggestion_id="sug-proxy-fail",
        user_id="user-proxy-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-proxy-fail",
        suggestion_id="sug-proxy-fail",
        user_id="user-proxy-fail",
    )
    proxy_error = LlmProxyExecutionError(
        error_code=PROXY_REQUEST_FAILED,
        error_message="proxy failed",
        retryable=False,
        local_job_id="proxy-job-1",
    )

    async def fail_runner(_state):
        raise proxy_error

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    error = result.run_result.error_payload
    assert error is not None
    assert error["error_code"] == "ACTION_LLM_RESPONSE_ERROR"
    assert error["error_message"] == "proxy failed"
    details = error["error_details"]
    assert isinstance(details, dict)
    assert details["proxy_error_code"] == PROXY_REQUEST_FAILED
    assert result.execution_session_id
    session = load_execution_session(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        execution_session_id=result.execution_session_id,
    )
    assert session.status == "failed"


@pytest.mark.asyncio
async def test_retryable_runtime_failure_is_not_converted_to_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)
    repo = _build_repo(
        action_id="act-retryable",
        suggestion_id="sug-retryable",
        user_id="user-retryable",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-retryable",
        suggestion_id="sug-retryable",
        user_id="user-retryable",
    )
    retryable_error = LlmProxyExecutionError(
        error_code=PROXY_REQUEST_FAILED,
        error_message="proxy unavailable",
        retryable=True,
    )
    execution_session_id: str | None = None

    async def fail_runner(state):
        nonlocal execution_session_id
        raw_execution_session_id = state.get("execution_session_id")
        assert isinstance(raw_execution_session_id, str)
        execution_session_id = raw_execution_session_id
        raise retryable_error

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        with pytest.raises(LlmProxyExecutionError) as raised:
            await action_use_case.execute_action_runtime(
                request,
                emit_action_step=AsyncMock(),
                emit_error=AsyncMock(),
            )

    assert raised.value is retryable_error
    assert execution_session_id is not None
    session = load_execution_session(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        execution_session_id=execution_session_id,
    )
    assert session.status == "running"


@pytest.mark.asyncio
async def test_local_execution_session_complete_failure_does_not_hide_graph_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-session-complete-fail",
        suggestion_id="sug-session-complete-fail",
        user_id="user-session-complete-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-session-complete-fail",
        suggestion_id="sug-session-complete-fail",
        user_id="user-session-complete-fail",
    )

    with (
        patch(
            "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
            side_effect=RuntimeError("compile failed"),
        ),
        patch(
            "pantaray_agents.application.action.execution_service.complete_execution_session",
            side_effect=RuntimeError("session complete failed"),
        ),
    ):
        response = await action_use_case.process(request)

    assert response.error is not None
    assert response.error.error_code == "ACTION_GRAPH_BUILD_FAILED"


@pytest.mark.asyncio
async def test_runtime_failure_completes_local_session_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-runtime-session-fail",
        suggestion_id="sug-runtime-session-fail",
        user_id="user-runtime-session-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-runtime-session-fail",
        suggestion_id="sug-runtime-session-fail",
        user_id="user-runtime-session-fail",
    )
    completions: list[tuple[str, str]] = []

    async def fail_runner(_state):
        raise RuntimeError("boom")

    def fake_complete_local_execution_session_if_present(
        *,
        state: dict,
        status: str,
        completed_at: str,
    ) -> None:
        completions.append((str(state.get("execution_session_id")), status))
        assert completed_at

    monkeypatch.setattr(
        action_use_case._execution_service,  # noqa: SLF001
        "try_complete_local_execution_session_if_present",
        fake_complete_local_execution_session_if_present,
    )
    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    assert len(completions) == 1
    execution_session_id, status = completions[0]
    assert execution_session_id
    assert status == "failed"
    assert result.run_result.action_failure_code == "ACTION_GRAPH_EXECUTION_FAILED"
    assert result.execution_session_id == execution_session_id


@pytest.mark.asyncio
async def test_partial_checkpoint_identity_does_not_terminalize_unverified_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-counter-session-fail",
        suggestion_id="sug-counter-session-fail",
        user_id="user-counter-session-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-counter-session-fail",
        suggestion_id="sug-counter-session-fail",
        user_id="user-counter-session-fail",
    )
    completions: list[tuple[str, str]] = []
    partial_state = {"execution_session_id": "unverified-session"}

    def fake_complete_execution_session(
        *, execution_session_id: str, status: str, completed_at: str, **_kwargs: object
    ) -> None:
        completions.append((execution_session_id, status))
        assert completed_at

    monkeypatch.setattr(
        "pantaray_agents.application.action.execution_service.complete_execution_session",
        fake_complete_execution_session,
    )
    with pytest.raises(RuntimeError, match="checkpoint must preserve"):
        action_use_case._execution_service.attach_local_execution_context_if_required(  # noqa: SLF001
            request=request,
            started_at=request.user_step_created_at,
            state=partial_state,  # type: ignore[arg-type]
        )
    assert completions == []


def test_missing_retained_leaf_fails_verified_session_before_context_discard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)
    repo = _build_repo(
        action_id="act-missing-retained-leaf",
        suggestion_id="sug-missing-retained-leaf",
        user_id="user-missing-retained-leaf",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-missing-retained-leaf",
        suggestion_id="sug-missing-retained-leaf",
        user_id="user-missing-retained-leaf",
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: tmp_path / "python",
    )
    context = ensure_action_scratch_execution_context(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        user_id=request.user_id,
        action_id=request.action_id,
        started_at=request.user_step_created_at,
        allowed_tool_ids=("read",),
    )
    state = {
        "manifest_id": context.manifest_id,
        "execution_session_id": context.execution_session_id,
        "execution_network_policy": context.network_policy,
        "action_temp_dir": str(context.action_temp_dir),
        "app_runtime_python": str(context.app_runtime_python),
        "read_access_scope": context.read_access_scope,
        "context": {},
    }
    context.action_temp_dir.rmdir()

    with pytest.raises(ActionExecutionContextError, match="no canonical existing"):
        action_use_case._execution_service.attach_local_execution_context_if_required(  # noqa: SLF001
            request=request,
            started_at=request.user_step_created_at,
            state=state,  # type: ignore[arg-type]
        )

    session = load_execution_session(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
    )
    assert session.status == "failed"
    assert state == {"context": {}}


@pytest.mark.asyncio
async def test_counter_invariant_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-counter-fail",
        suggestion_id="sug-counter-fail",
        user_id="user-counter-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-counter-fail",
        suggestion_id="sug-counter-fail",
        user_id="user-counter-fail",
    )

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        side_effect=CounterInvariantError(
            "tool_steps_taken is missing in worker_state"
        ),
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    payload = result.run_result.error_payload
    assert payload is not None
    assert payload["error_code"] == "ACTION_COUNTER_INVARIANT_VIOLATION"
    assert payload["error_type"] == "internal_error"
    assert result.execution_session_id
    repo.save_action.assert_not_awaited()
    repo.update_action_status_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_tool_args_return_error_to_terminal_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)
    MockRepository.clear_data()
    repo = MockActionAgentRepository()
    action_use_case = _build_mock_action_use_case(repo)
    await repo.save_suggestion(
        {
            "suggestion_id": "sug-it-invalid",
            "user_id": "user-it-invalid",
            "answer": "Test suggestion content.",
            "thinking": "Test suggestion thinking.",
            "accepted_at": "2026-01-01T00:00:00+00:00",
        }
    )
    invalid_arguments = {"content": 1}
    action_use_case._agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]  # noqa: SLF001
        side_effect=[
            native_tool_turn("write_session_memory", invalid_arguments)
            for _ in range(5)
        ]
    )
    request = _request(
        action_id="act-it-invalid",
        suggestion_id="sug-it-invalid",
        user_id="user-it-invalid",
    )
    await repo.upsert_action_header(
        action_id=request.action_id,
        user_id=request.user_id,
        suggestion_id=request.suggestion_id,
        prompt_name="action/executing",
        prompt_version="1.0",
    )

    try:
        response = await action_use_case.process(request)

        assert response.status == "error"
        assert response.error is not None
        assert response.error.error_code == "ACTION_TOOL_ARGS_INVALID_RETRY_EXCEEDED"

        saved = await repo.get_action(
            user_id="user-it-invalid",
            action_id=request.action_id,
        )
        assert saved.data is not None
        assert saved.data.get("status") == "processing"
    finally:
        MockRepository.clear_data()


@pytest.mark.parametrize(
    ("proxy_reason", "expected_code"),
    [
        ("active_media_too_large", "ACTION_IMAGE_INPUT_TOO_LARGE"),
        ("media_too_large", "ACTION_IMAGE_INPUT_TOO_LARGE"),
        ("media_references_too_large", "ACTION_IMAGE_INPUT_TOO_LARGE"),
        ("invalid_image", "ACTION_IMAGE_INPUT_INVALID"),
    ],
)
@pytest.mark.asyncio
async def test_image_input_proxy_failure_gets_its_own_public_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    proxy_reason: str,
    expected_code: str,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-image-fail",
        suggestion_id="sug-image-fail",
        user_id="user-image-fail",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-image-fail",
        suggestion_id="sug-image-fail",
        user_id="user-image-fail",
    )

    async def fail_runner(_state):
        raise LlmProxyExecutionError(
            error_code=PROXY_INVALID_INPUT,
            error_message="LLM request could not fit the provider request-size limit.",
            retryable=False,
            media_failure_reason=proxy_reason,
        )

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    assert result.run_result.action_failure_code == expected_code
    message = result.run_result.failure_message_public
    assert message is not None
    assert message != "Action execution failed."


@pytest.mark.asyncio
async def test_image_input_failure_message_follows_the_request_language(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-image-ja",
        suggestion_id="sug-image-ja",
        user_id="user-image-ja",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-image-ja",
        suggestion_id="sug-image-ja",
        user_id="user-image-ja",
        language="ja",
    )

    async def fail_runner(_state):
        raise LlmProxyExecutionError(
            error_code=PROXY_INVALID_INPUT,
            error_message="LLM request could not fit the provider request-size limit.",
            retryable=False,
            media_failure_reason="active_media_too_large",
        )

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    assert result.run_result.failure_message_public == (
        "添付画像がモデルの上限を超えました。枚数を減らすか、小さい画像で送り直してください。"
    )


@pytest.mark.asyncio
async def test_unrelated_invalid_input_keeps_the_generic_proxy_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id="act-other-invalid",
        suggestion_id="sug-other-invalid",
        user_id="user-other-invalid",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id="act-other-invalid",
        suggestion_id="sug-other-invalid",
        user_id="user-other-invalid",
    )

    async def fail_runner(_state):
        raise LlmProxyExecutionError(
            error_code=PROXY_INVALID_INPUT,
            error_message="invalid request",
            retryable=False,
            media_failure_reason="media_projection_integrity",
        )

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    assert result.run_result.action_failure_code == "ACTION_LLM_RESPONSE_ERROR"
    assert result.run_result.failure_message_public == "Action execution failed."


async def _terminal_of_refused_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    slug: str,
    error: LlmProxyExecutionError,
    language: str | None = None,
):
    """Run one Action whose only LLM call is refused, and read its terminal."""
    _install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = _build_repo(
        action_id=f"act-{slug}",
        suggestion_id=f"sug-{slug}",
        user_id=f"user-{slug}",
    )
    action_use_case = _build_action_use_case(repo)
    request = _request(
        action_id=f"act-{slug}",
        suggestion_id=f"sug-{slug}",
        user_id=f"user-{slug}",
        **({"language": language} if language is not None else {}),
    )

    async def fail_runner(_state):
        raise error

    with patch(
        "pantaray_agents.agents.action_agent.agent.build_action_agent_graph",
        return_value=fail_runner,
    ):
        result = await action_use_case.execute_action_runtime(
            request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )
    return result.run_result


@pytest.mark.parametrize(
    ("slug", "error", "expected_code"),
    [
        (
            "no-connection",
            LlmProxyExecutionError(
                error_code=PROXY_CONNECTION_NOT_CONFIGURED,
                error_message="No LLM connection is configured.",
                retryable=False,
            ),
            "ACTION_CONNECTION_NOT_CONFIGURED",
        ),
        (
            "bad-key",
            LlmProxyExecutionError(
                error_code=PROXY_AUTHENTICATION_FAILED,
                error_message="The model provider authentication failed.",
                retryable=False,
                suggested_action="configure_connection",
            ),
            "ACTION_CONNECTION_REJECTED",
        ),
        (
            "lapsed-login",
            LlmProxyExecutionError(
                error_code=PROXY_AUTHENTICATION_FAILED,
                error_message="The model provider authentication failed.",
                retryable=False,
                suggested_action="reauthenticate",
            ),
            "ACTION_SIGN_IN_EXPIRED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_refused_route_names_the_credential_the_user_repairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    slug: str,
    error: LlmProxyExecutionError,
    expected_code: str,
) -> None:
    run_result = await _terminal_of_refused_route(
        monkeypatch, tmp_path, slug=slug, error=error
    )

    assert run_result.action_failure_code == expected_code
    assert run_result.failure_message_public not in (None, "Action execution failed.")
    # The diagnostic error keeps the folded code and the detail it was read from.
    assert run_result.error_payload is not None
    assert run_result.error_payload["error_code"] == "ACTION_LLM_RESPONSE_ERROR"


@pytest.mark.asyncio
async def test_refused_route_message_follows_the_request_language(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    run_result = await _terminal_of_refused_route(
        monkeypatch,
        tmp_path,
        slug="no-connection-ja",
        error=LlmProxyExecutionError(
            error_code=PROXY_CONNECTION_NOT_CONFIGURED,
            error_message="No LLM connection is configured.",
            retryable=False,
        ),
        language="ja",
    )

    assert run_result.failure_message_public == (
        "AI接続が設定されていません。設定のAI接続で接続方法を選んでください。"
    )


@pytest.mark.asyncio
async def test_exhausted_usage_tells_the_user_to_check_the_ai_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The folded code is the agent prefix plus a suffix, so the message keys on it."""
    run_result = await _terminal_of_refused_route(
        monkeypatch,
        tmp_path,
        slug="no-usage",
        error=LlmProxyExecutionError(
            error_code=PROXY_INSUFFICIENT_BALANCE,
            error_message="The model provider account has no usage left.",
            retryable=False,
        ),
    )

    assert run_result.action_failure_code == "ACTION_INSUFFICIENT_BALANCE"
    assert run_result.failure_message_public not in (None, "Action execution failed.")


@pytest.mark.asyncio
async def test_authentication_failure_without_a_remedy_stays_generic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A raiser that names no repair leaves the terminal nothing specific to say."""
    run_result = await _terminal_of_refused_route(
        monkeypatch,
        tmp_path,
        slug="auth-no-remedy",
        error=LlmProxyExecutionError(
            error_code=PROXY_AUTHENTICATION_FAILED,
            error_message="The model provider authentication failed.",
            retryable=False,
        ),
    )

    assert run_result.action_failure_code == "ACTION_LLM_RESPONSE_ERROR"
    assert run_result.failure_message_public == "Action execution failed."
