from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import build_action_request
from tests.unit.agents.action_runtime_failure_test_support import (
    build_action_use_case,
    build_repo,
    fake_load_config,
    install_local_runtime_test_db,
    request,
)
from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.application.action.use_case_service import (
    ActionUseCaseDeps,
    ActionUseCaseService,
)
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.repositories.repository import (
    RepositoryErrorKind,
    RepositoryResult,
)
from pantaray_agents.schema.repository_errors import (
    FetchContextError,
    is_retryable_repository_exception,
)


@pytest.mark.asyncio
async def test_retryable_checkpoint_failure_preserves_defer_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = build_repo(
        action_id="act-prepare-fail",
        suggestion_id="sug-prepare-fail",
        user_id="user-prepare-fail",
    )
    repo.get_runtime_resume_context_for_user_step = AsyncMock(
        return_value=RepositoryResult(
            error="checkpoint unavailable",
            error_kind=RepositoryErrorKind.TRANSIENT,
            retryable=True,
        )
    )
    action_use_case = build_action_use_case(repo)
    action_request = request(
        action_id="act-prepare-fail",
        suggestion_id="sug-prepare-fail",
        user_id="user-prepare-fail",
    )

    with pytest.raises(FetchContextError) as raised:
        await action_use_case.execute_action_runtime(
            action_request,
            emit_action_step=AsyncMock(),
            emit_error=AsyncMock(),
        )

    assert raised.value.error_kind is RepositoryErrorKind.TRANSIENT
    assert is_retryable_repository_exception(raised.value)


@pytest.mark.asyncio
async def test_invalid_action_header_token_budget_returns_typed_prepare_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = build_repo(
        action_id="act-invalid-token-budget",
        suggestion_id="sug-invalid-token-budget",
        user_id="user-invalid-token-budget",
    )
    repo.get_action = AsyncMock(
        return_value=RepositoryResult(
            data={
                "action_id": "act-invalid-token-budget",
                "suggestion_id": "sug-invalid-token-budget",
                "user_id": "user-invalid-token-budget",
                "status": "processing",
                "token_budget": 0,
            }
        )
    )
    action_use_case = build_action_use_case(repo)
    action_request = request(
        action_id="act-invalid-token-budget",
        suggestion_id="sug-invalid-token-budget",
        user_id="user-invalid-token-budget",
    )

    result = await action_use_case.execute_action_runtime(
        action_request,
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
    )

    error = result.run_result.error_payload
    assert error is not None
    assert error["error_code"] == "ACTION_RUNTIME_PREPARE_FAILED"
    assert error["error_details"] == {
        "stage": "prepare",
        "exception_type": "RuntimeError",
    }


@pytest.mark.asyncio
async def test_process_fails_closed_when_runtime_checkpoint_row_is_not_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)
    insert_agent_action(
        db_path=Path(os.environ["LOCAL_DB_PATH"]),
        user_id="user-bad-checkpoint-row",
        suggestion_id="sug-bad-checkpoint-row",
        action_id="act-bad-checkpoint-row",
    )

    repo = MagicMock()
    repo.get_action = AsyncMock(
        return_value=RepositoryResult(
            data={
                "action_id": "act-bad-checkpoint-row",
                "suggestion_id": "sug-bad-checkpoint-row",
                "user_id": "user-bad-checkpoint-row",
                "status": "processing",
            }
        )
    )
    repo.get_runtime_resume_context_for_user_step = AsyncMock(
        return_value=RepositoryResult(data=["bad-row"])
    )

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}}, repository=repo
        )
    action_use_case = ActionUseCaseService(ActionUseCaseDeps(agent=agent))

    action_request = build_action_request(
        action_id="act-bad-checkpoint-row",
        suggestion_id="sug-bad-checkpoint-row",
        user_id="user-bad-checkpoint-row",
    )

    with patch(
        "pantaray_agents.application.action.execution_service.ensure_action_scratch_execution_context"
    ) as ensure_execution_context:
        response = await action_use_case.process(action_request)

    assert response.status == "error"
    assert response.error is not None
    assert response.error.error_code == "ACTION_RESUME_CHECKPOINT_INVALID"
    ensure_execution_context.assert_not_called()


def test_resolve_initial_state_treats_missing_runtime_checkpoint_row_as_fresh_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = MagicMock()
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}}, repository=repo
        )
    action_use_case = ActionUseCaseService(ActionUseCaseDeps(agent=agent))

    action_request = build_action_request(
        action_id="act-missing-checkpoint-row",
        suggestion_id="sug-missing-checkpoint-row",
        user_id="user-missing-checkpoint-row",
    )

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=action_request,
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
        checkpoint_row=RepositoryResult(data=None),
    )

    assert resolved["status"] == "processing"
    assert resolved["phase"] == "init"
    assert resolved["step"] == 1
    assert resolved["action_id"] == "act-missing-checkpoint-row"
    assert resolved["suggestion_id"] == "sug-missing-checkpoint-row"
    assert resolved["user_id"] == "user-missing-checkpoint-row"
    assert resolved["cancel_check_max_consecutive_failures"] == 3
    assert resolved["cancel_check_failure_grace_seconds"] == 60


def test_resolve_initial_state_fails_when_approval_resume_checkpoint_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = MagicMock()
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}}, repository=repo
        )
    action_use_case = ActionUseCaseService(ActionUseCaseDeps(agent=agent))

    resolved = action_use_case._resume_service.resolve_initial_state(  # noqa: SLF001
        request=build_action_request(
            action_id="act-missing-approval-checkpoint",
            suggestion_id="sug-missing-approval-checkpoint",
            user_id="user-missing-approval-checkpoint",
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
        checkpoint_row=RepositoryResult(data=None),
    )

    assert resolved["status"] == "error"
    error = resolved["errors"][-1]
    assert error["error_code"] == "ACTION_RESUME_APPROVAL_STATE_INVALID"
    assert "approval resume checkpoint not found" in str(error["error_message"])


@pytest.mark.asyncio
async def test_execution_service_uses_approval_resume_checkpoint_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install_local_runtime_test_db(monkeypatch, tmp_path=tmp_path)

    repo = build_repo(
        action_id="act-approval-resume",
        suggestion_id="sug-approval-resume",
        user_id="user-approval-resume",
    )
    repo.get_runtime_checkpoint_for_approval_resume.return_value = RepositoryResult(
        data=None
    )
    action_use_case = build_action_use_case(repo)

    result = (
        await action_use_case._execution_service.load_runtime_checkpoint_for_request(  # noqa: SLF001
            build_action_request(
                action_id="act-approval-resume",
                suggestion_id="sug-approval-resume",
                user_id="user-approval-resume",
                approval_resume_session_id="approval-1",
                approval_resume_tool_request_id="request-1",
            )
        )
    )

    checkpoint, intervening_user_steps = result
    assert checkpoint.data is None
    assert intervening_user_steps == ()
    repo.get_runtime_checkpoint_for_approval_resume.assert_awaited_once_with(
        user_id="user-approval-resume",
        action_id="act-approval-resume",
        approval_session_id="approval-1",
        tool_request_id="request-1",
    )
