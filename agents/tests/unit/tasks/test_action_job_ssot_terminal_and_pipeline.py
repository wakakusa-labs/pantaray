from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pantaray_agents.action_status import ActionTerminalStatus
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobExecutionContext,
    ActionJobPreparation,
)
from pantaray_agents.local_runtime.runtime.action_subagent_parent_lifecycle import (
    ActionChildSettlementPendingError,
)
from pantaray_agents.local_runtime.runtime.action_terminal_repository import (
    InvalidActionMemoryDraftError,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    ActionCleanupPassResult,
)
from pantaray_agents.schema.agent.action import (
    ActionExecutionResult,
    ActionRunResult,
    ActionScratchExecutionTarget,
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.tasks.action_job import _run_action_job
from pantaray_agents.tasks.action_job_support import (
    NonRetryableTerminalPersistenceError,
    is_retryable_terminal_persistence_error,
)
from pantaray_agents.tasks.action_job_terminal import (
    ActionJobTerminalState,
    ActionJobTerminalWriter,
)
from pantaray_agents.tasks.process_event_errors import ProcessEventAppendError

RUNTIME_MODULE = "pantaray_agents.tasks.action_job_runtime"
TERMINAL_MODULE = "pantaray_agents.tasks.action_job_terminal"


def test_terminal_persistence_does_not_retry_unknown_migration_error() -> None:
    assert (
        is_retryable_terminal_persistence_error(MigrationError("job does not exist"))
        is False
    )


def _persist_result(
    *,
    action_status: ActionTerminalStatus,
    process_completed_sequence: int,
    action_failure_code: str | None = None,
    final_output: str | None = None,
    failure_stage: str | None = None,
    failure_message_public: str | None = None,
) -> object:
    return type(
        "_PersistedActionTerminalResultValue",
        (),
        {
            "action_status": action_status,
            "process_completed_sequence": process_completed_sequence,
            "action_failure_code": action_failure_code,
            "final_output": final_output,
            "failure_stage": failure_stage,
            "failure_message_public": failure_message_public,
        },
    )()


def _build_payload() -> dict[str, object]:
    return {
        "job_id": "job",
        "process_id": "proc",
        "action_id": "act",
        "user_id": "user",
        "continuation_ref": {
            "kind": "user_step",
            "user_step_id": "user-step-1",
        },
    }


def _run_result(
    *,
    action_id: str = "act",
    suggestion_id: str = "sug",
    user_id: str = "user",
    completed_at: str = "2024-01-01T00:00:10Z",
    status: ActionTerminalStatus,
    final_output: str = "",
    action_failure_code: str | None = None,
    error_payload: dict[str, object] | None = None,
    failure_stage: str | None = None,
    failure_message_public: str | None = None,
    final_prompt_text: str | None = "final prompt",
    prompt_name: str = "action/executing",
    prompt_version: str = "1.0",
    total_steps: int | None = 12,
    total_llm_steps: int | None = 7,
    total_tool_steps: int | None = 5,
    total_prompt_tokens: int | None = 123,
    total_completion_tokens: int | None = 45,
    approval_blockers: list[dict[str, object]] | None = None,
) -> ActionRunResult:
    return ActionRunResult(
        action_id=action_id,
        suggestion_id=suggestion_id,
        user_id=user_id,
        completed_at=completed_at,
        status=status,
        final_output=final_output,
        action_failure_code=action_failure_code,
        error_payload=error_payload,
        failure_stage=failure_stage,
        failure_message_public=failure_message_public,
        final_prompt_text=final_prompt_text,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        total_steps=total_steps,
        total_llm_steps=total_llm_steps,
        total_tool_steps=total_tool_steps,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        approval_blockers=approval_blockers or [],
    )


def _execution_result(
    *,
    run_result: ActionRunResult,
    execution_session_id: str | None = None,
    runtime_state_checkpoint: dict[str, object] | None = None,
) -> ActionExecutionResult:
    return ActionExecutionResult(
        run_result=run_result,
        execution_session_id=execution_session_id,
        runtime_state_checkpoint=runtime_state_checkpoint,
    )


def _empty_cleanup_result() -> ActionCleanupPassResult:
    return ActionCleanupPassResult(
        cleaned_count=0,
        affected_resource_count=0,
        execution_failure_count=0,
        persistence_failure_count=0,
        failed_resource_kinds=(),
        abandoned_count=0,
    )


def _enable_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Repository:
        def __init__(self, *, db_path: Path, busy_timeout_ms: int) -> None:
            assert db_path == Path("runtime.db")
            assert busy_timeout_ms == 1_000

        def prepare_execution(self, **_kwargs) -> ActionJobPreparation:  # noqa: ANN003
            return ActionJobPreparation(
                context=ActionJobExecutionContext(
                    suggestion_id="sug",
                    user_step_id="user-step-1",
                    user_step_number=1,
                    user_step_local_step_number=1,
                    user_step_short_id="S-1-USER",
                    user_step_created_at="2026-01-01T00:00:00Z",
                    user_message=ActionUserMessageInput(
                        message_id="11111111-1111-4111-8111-111111111111",
                        content="Perform the work",
                        suggestion_approval=SuggestionApprovalInput(
                            suggestion_id="sug",
                            approved_at="2026-01-01T00:00:00Z",
                        ),
                    ),
                    execution_target=ActionScratchExecutionTarget(),
                    command_id="11111111-1111-4111-8111-111111111111",
                    accepted_at="2026-01-01T00:00:00Z",
                )
            )

        def finalize_skipped_action_start(self, **_kwargs) -> None:  # noqa: ANN003
            raise AssertionError("runtime should not skip")

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )
    monkeypatch.setattr(f"{RUNTIME_MODULE}.ActionJobRuntimeRepository", _Repository)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_job_root_execution_session_id",
        lambda **_kwargs: None,
    )


def _install_runtime_recorders(
    monkeypatch: pytest.MonkeyPatch,
    calls: dict[str, object],
    *,
    fail_error_append: BaseException | None = None,
    pause_fenced: bool = False,
) -> None:
    def _mark_paused(**kwargs):  # noqa: ANN001
        order = calls.get("order")
        if isinstance(order, list):
            order.append(("append", "process_paused"))
        events = calls.setdefault("events", [])
        assert isinstance(events, list)
        events.append({"event": "process_paused", "data": kwargs["payload"]})
        # A fenced run is never parked; the pause owner reports it back instead.
        return None if pause_fenced else "1-paused"

    def _append_local_process_event(**kwargs):  # noqa: ANN001
        event_name = kwargs["event_type"]
        order = calls.get("order")
        if isinstance(order, list):
            order.append(("append", event_name))
        if event_name == "error" and fail_error_append is not None:
            raise fail_error_append
        events = calls.setdefault("events", [])
        assert isinstance(events, list)
        events.append({"event": event_name, "data": kwargs["payload"]})
        return f"1-{len(events)}"

    monkeypatch.setattr(f"{RUNTIME_MODULE}.mark_local_action_job_paused", _mark_paused)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.append_local_process_event", _append_local_process_event
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.append_local_action_step_event",
        lambda **kwargs: calls.update(action_step=kwargs) or "step-event",
    )


@pytest.mark.asyncio
async def test_action_job_error_persists_terminal_before_stream_end_and_uses_last_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"order": [], "events": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)

    async def _record_child_cleanup(**kwargs) -> ActionCleanupPassResult:  # noqa: ANN003
        assert kwargs["execution_session_id"] == "session-1"
        # Catches: the terminal not ending the run itself, whose browser page
        # outlives it when the run started no command.
        assert kwargs["run_process_id"] == "proc"
        calls["order"].append(("cleanup", "children"))
        return _empty_cleanup_result()

    receipt = object()

    def _record_root_cleanup(**_kwargs) -> object:  # noqa: ANN003
        calls["order"].append(("cleanup", "root"))
        return SimpleNamespace(current_resource=SimpleNamespace(status="cleaned"))

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_job_root_execution_session_id",
        lambda **_kwargs: calls["order"].append(("capture", "session")) or "session-1",
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.cancel_action_runtime_resources", _record_child_cleanup
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_session_temp_cleanup_receipt",
        lambda **_kwargs: receipt,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.cleanup_action_session_temp", _record_root_cleanup
    )

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        calls["order"].append(("persist_terminal", kwargs["command"].action_status))
        calls["persist_terminal_kwargs"] = kwargs
        return _persist_result(action_status="error", process_completed_sequence=17)

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            from pantaray_agents.application.action.ports import ActionToolStepEmission

            await emit_action_step(
                ActionToolStepEmission(
                    step_id="step-1",
                    step_number=1,
                    step_name="tool::read",
                    label="Read",
                    tool_args={"args": {"path": "notes.txt"}},
                    status="processing",
                    started_at="2024-01-01T00:00:01Z",
                    completed_at=None,
                )
            )
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    completed_at="2024-01-01T00:00:10Z",
                    status="error",
                    action_failure_code="ACTION_RESPONSE_ERROR",
                    error_payload={
                        "error_type": "llm_api_error",
                        "error_code": "ACTION_RESPONSE_ERROR",
                        "error_message": "proxy failed",
                        "error_details": {
                            "request_id": "req-1",
                            "profile_id": "action.planning",
                        },
                        "severity": "error",
                    },
                    failure_stage="running_failed",
                    failure_message_public="Action execution failed.",
                ),
            )

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["order"] == [
        ("capture", "session"),
        ("append", "error"),
        ("persist_terminal", "error"),
        ("cleanup", "children"),
        ("cleanup", "root"),
    ]
    command = calls["persist_terminal_kwargs"]["command"]
    assert command.failure_code == "ACTION_RESPONSE_ERROR"
    assert command.error_payload is not None
    assert command.error_payload["error_details"]["request_id"] == "req-1"
    error_payload = next(
        event["data"] for event in calls["events"] if event["event"] == "error"
    )
    assert error_payload["error_details"]["profile_id"] == "action.planning"
    assert command.final_prompt_text == "final prompt"
    assert command.total_steps == 12
    assert calls["action_step"]["process_id"] == "proc"
    assert calls["action_step"]["payload"]["action_id"] == "act"


@pytest.mark.asyncio
async def test_action_job_retries_terminal_persist_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {
        "persist_attempts": 0,
        "sleep_delays": [],
        "events": [],
        "persist_kwargs": [],
    }
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)

    async def _fake_sleep(delay: float) -> None:
        delays = calls["sleep_delays"]
        assert isinstance(delays, list)
        delays.append(delay)

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        calls["persist_attempts"] += 1
        persist_kwargs = calls["persist_kwargs"]
        assert isinstance(persist_kwargs, list)
        persist_kwargs.append(kwargs)
        if calls["persist_attempts"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return _persist_result(action_status="error", process_completed_sequence=23)

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    completed_at="2024-01-01T00:00:10Z",
                    status="error",
                ),
            )

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(f"{TERMINAL_MODULE}.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["persist_attempts"] == 2
    assert calls["sleep_delays"] == [1.0]
    last_kwargs = calls["persist_kwargs"][-1]
    assert last_kwargs["command"].action_status == "error"


@pytest.mark.asyncio
async def test_action_job_does_not_retry_non_retryable_terminal_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"persist_attempts": 0, "sleep_delays": [], "events": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)

    async def _fake_sleep(delay: float) -> None:
        delays = calls["sleep_delays"]
        assert isinstance(delays, list)
        delays.append(delay)

    async def _fake_persist_terminal_action_status_strict(**_kwargs) -> object:  # noqa: ANN001
        calls["persist_attempts"] += 1
        raise NonRetryableTerminalPersistenceError("invalid action terminal status")

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    completed_at="2024-01-01T00:00:10Z",
                    status="error",
                ),
            )

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(f"{TERMINAL_MODULE}.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    with pytest.raises(NonRetryableTerminalPersistenceError):
        await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["persist_attempts"] == 1
    assert calls["sleep_delays"] == []


@pytest.mark.asyncio
async def test_terminal_writer_converts_invalid_memory_draft_to_error_command() -> None:
    calls: list[dict[str, object]] = []

    async def _persist(**kwargs) -> object:  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            raise InvalidActionMemoryDraftError("invalid memory draft")
        return _persist_result(action_status="error", process_completed_sequence=31)

    state = ActionJobTerminalState()
    writer = ActionJobTerminalWriter(
        db_path=Path("runtime.db"),
        busy_timeout_ms=1_000,
        job_id="job",
        process_id="proc",
        user_id="user",
        suggestion_id="sug",
        action_id="act",
        command_id="command",
        accepted_at="2024-01-01T00:00:00Z",
        state=state,
        mark_local_action_job_paused=object(),
        persist_terminal_action_status_strict=_persist,
    )
    result = await writer.persist_terminal_until_success(
        requested_status="success",
        completed_at="2024-01-01T00:00:10Z",
        reason="agent_response_terminal",
        runtime_state_checkpoint=None,
        run_result=_run_result(
            status="success",
            final_output="done",
        ),
    )

    assert result == "error"
    assert state.terminal_written and state.terminal_status == "error"
    first, fallback = (call["command"] for call in calls)
    assert (first.action_status, fallback.action_status) == ("success", "error")
    assert first.process_completed_event_id == fallback.process_completed_event_id
    assert (
        fallback.failure_code,
        fallback.failure_stage,
        fallback.failure_message_public,
    ) == (
        "ACTION_PERSIST_TERMINAL_PAYLOAD_INVALID",
        "persist_final_state_failed",
        "Action execution failed.",
    )
    assert fallback.final_output is fallback.memory_draft_json is None
    assert fallback.error_payload is None


@pytest.mark.asyncio
async def test_terminal_writer_waits_for_pending_child_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    attempts = 0

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def _persist(**_kwargs) -> object:  # noqa: ANN003
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ActionChildSettlementPendingError("child is unsettled")
        return _persist_result(action_status="error", process_completed_sequence=41)

    monkeypatch.setattr(f"{TERMINAL_MODULE}.asyncio.sleep", _fake_sleep)
    state = ActionJobTerminalState()
    writer = ActionJobTerminalWriter(
        db_path=Path("runtime.db"),
        busy_timeout_ms=1_000,
        job_id="job",
        process_id="proc",
        user_id="user",
        suggestion_id="sug",
        action_id="act",
        command_id="command",
        accepted_at="2024-01-01T00:00:00Z",
        state=state,
        mark_local_action_job_paused=object(),
        persist_terminal_action_status_strict=_persist,
    )

    result = await writer.persist_terminal_until_success(
        requested_status="error",
        completed_at="2024-01-01T00:00:10Z",
        reason="agent_response_terminal",
        runtime_state_checkpoint=None,
    )

    assert (result, attempts, delays) == ("error", 3, [1.0, 2.0])
    assert state.terminal_written and state.terminal_status == "error"


@pytest.mark.asyncio
async def test_action_job_retries_terminal_delivery_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"persist_attempts": 0, "sleep_delays": [], "events": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)

    async def _fake_sleep(delay: float) -> None:
        delays = calls["sleep_delays"]
        assert isinstance(delays, list)
        delays.append(delay)

    async def _fake_persist_terminal_action_status_strict(**_kwargs) -> object:  # noqa: ANN001
        calls["persist_attempts"] += 1
        return _persist_result(action_status="error", process_completed_sequence=29)

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    completed_at="2024-01-01T00:00:10Z",
                    status="error",
                ),
            )

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(f"{TERMINAL_MODULE}.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["persist_attempts"] == 1
    assert calls["sleep_delays"] == []
    assert all(event["event"] != "stream_end" for event in calls["events"])


@pytest.mark.asyncio
async def test_action_job_success_enqueues_agent_experience_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"persist": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        persist = calls["persist"]
        assert isinstance(persist, list)
        persist.append(kwargs)
        return _persist_result(action_status="success", process_completed_sequence=37)

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    completed_at="2024-01-01T00:00:10Z",
                    status="success",
                    final_output="action final output",
                ),
            )

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["persist"], "action_status persistence should be attempted"
    assert calls["persist"][0]["command"].final_output == "action final output"


@pytest.mark.parametrize("pause_fenced", [False, True])
@pytest.mark.asyncio
async def test_action_job_processing_result_marks_local_runtime_paused(
    monkeypatch: pytest.MonkeyPatch, pause_fenced: bool
) -> None:
    calls: dict[str, object] = {"events": [], "persist": [], "cleanup_sessions": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls, pause_fenced=pause_fenced)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_job_root_execution_session_id",
        lambda **_kwargs: pytest.fail(
            "paused Action must not resolve cleanup identity"
        ),
    )

    async def _record_cleanup(**kwargs) -> ActionCleanupPassResult:  # noqa: ANN003
        sessions = calls["cleanup_sessions"]
        assert isinstance(sessions, list)
        sessions.append(kwargs["execution_session_id"])
        return _empty_cleanup_result()

    async def _fail_cleanup(**_kwargs) -> ActionCleanupPassResult:  # noqa: ANN003
        pytest.fail("paused Action must not clean up its execution session")

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.cancel_action_runtime_resources",
        _record_cleanup if pause_fenced else _fail_cleanup,
    )

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        persist = calls["persist"]
        assert isinstance(persist, list)
        persist.append(kwargs)
        return _persist_result(action_status="error", process_completed_sequence=99)

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return type(
                "_ProcessingExecutionResult",
                (),
                {
                    "run_result": type(
                        "_ProcessingRunResult",
                        (),
                        {
                            "action_id": req.action_id,
                            "suggestion_id": req.suggestion_id,
                            "user_id": req.user_id,
                            "completed_at": "2024-01-01T00:00:10Z",
                            "status": "processing",
                            "final_output": "",
                            "action_failure_code": None,
                            "failure_stage": None,
                            "failure_message_public": None,
                            "approval_blockers": [
                                type(
                                    "_ApprovalBlocker",
                                    (),
                                    {
                                        "model_dump": lambda self, mode="json": {
                                            "action_id": req.action_id,
                                            "approval_session_id": "approval-1",
                                            "tool_request_id": "tool-request-1",
                                            "tool_id": "bash",
                                            "intent_class": "process_exec_local",
                                            "command_summary": {
                                                "kind": "bash",
                                                "command": "pwd",
                                            },
                                        },
                                    },
                                )()
                            ],
                        },
                    )(),
                    "execution_session_id": "session-paused",
                    "runtime_state_checkpoint": None,
                },
            )()

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    if pause_fenced:
        # The Stop fence outranks the approval anchor, so the run converges on
        # the cancel terminal that also settles its children and its session.
        assert [
            call["command"].action_status
            for call in calls["persist"]  # type: ignore[union-attr]
        ] == ["canceled"]
        assert calls["cleanup_sessions"] == ["session-paused"]
    else:
        assert calls["persist"] == []
        assert calls["cleanup_sessions"] == []
    paused_event = next(
        event["data"] for event in calls["events"] if event["event"] == "process_paused"
    )
    assert paused_event["reason"] == "approval_pending"


@pytest.mark.asyncio
async def test_action_job_terminal_result_session_reaches_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {
        "persist_statuses": [],
        "events": [],
        "cleanup_sessions": [],
    }
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_job_root_execution_session_id",
        lambda **_kwargs: pytest.fail(
            "terminal result session must reach cleanup without fallback lookup"
        ),
    )

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        persist_statuses = calls["persist_statuses"]
        assert isinstance(persist_statuses, list)
        persist_statuses.append(kwargs["command"].action_status)
        return _persist_result(action_status="error", process_completed_sequence=53)

    class _FakeActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    completed_at="2024-01-01T00:00:10Z",
                    status="error",
                    action_failure_code="ACTION_PROCESSING_TIMEOUT",
                    failure_stage="running_failed",
                    failure_message_public="Action execution failed.",
                ),
                execution_session_id="session-1",
            )

    async def _fake_get_action_application_service() -> _FakeActionAgent:
        return _FakeActionAgent()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    async def _record_cleanup(**kwargs) -> ActionCleanupPassResult:  # noqa: ANN003
        calls["cleanup_sessions"].append(kwargs["execution_session_id"])
        return _empty_cleanup_result()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.cancel_action_runtime_resources", _record_cleanup
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_session_temp_cleanup_receipt",
        lambda **_kwargs: None,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["persist_statuses"] == ["error"]
    assert calls["events"] == []
    assert calls["cleanup_sessions"] == ["session-1"]


@pytest.mark.asyncio
async def test_action_job_logs_when_error_event_append_fails_but_still_terminalizes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: dict[str, object] = {"persist": [], "events": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(
        monkeypatch,
        calls,
        fail_error_append=ProcessEventAppendError("sqlite unavailable for error event"),
    )

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        persist = calls["persist"]
        assert isinstance(persist, list)
        persist.append(kwargs)
        return _persist_result(action_status="error", process_completed_sequence=41)

    class _FailingActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            raise RuntimeError("worker boom")

    async def _fake_get_action_application_service() -> _FailingActionAgent:
        return _FailingActionAgent()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )

    def _fail_session_lookup(**_kwargs) -> None:  # noqa: ANN003
        raise sqlite3.OperationalError("read-only lookup unavailable")

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_job_root_execution_session_id",
        _fail_session_lookup,
    )

    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError, match="worker boom"):
        await _run_action_job(_build_payload())  # type: ignore[arg-type]

    assert calls["persist"], "terminal persistence should still happen"
    assert calls["persist"][0]["command"].action_status == "error"
    assert (
        "Failed to append action job error event before terminalization" in caplog.text
    )
    assert "Action terminal session lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_action_job_persists_structured_error_from_typed_result_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {"persist": [], "events": [], "cleanup_sessions": []}
    _enable_mock_mode(monkeypatch)
    _install_runtime_recorders(monkeypatch, calls)

    async def _fake_persist_terminal_action_status_strict(**kwargs) -> object:  # noqa: ANN001
        persist = calls["persist"]
        assert isinstance(persist, list)
        persist.append(kwargs)
        command = kwargs["command"]
        return _persist_result(
            action_status="error",
            process_completed_sequence=67,
            action_failure_code=command.failure_code,
            failure_stage=command.failure_stage,
            failure_message_public=command.failure_message_public,
        )

    class _FailingActionAgent:
        async def execute_action_runtime(self, req, *, emit_action_step, emit_error):  # noqa: ANN001,ARG002
            return _execution_result(
                run_result=_run_result(
                    action_id=req.action_id,
                    suggestion_id=req.suggestion_id,
                    user_id=req.user_id,
                    status="error",
                    action_failure_code="ACTION_COUNTER_INVARIANT_VIOLATION",
                    error_payload={
                        "error_type": "internal_error",
                        "error_code": "ACTION_COUNTER_INVARIANT_VIOLATION",
                        "error_message": (
                            "Action processing encountered an internal error."
                        ),
                    },
                    failure_stage="running_failed",
                    failure_message_public="Action execution failed.",
                ),
                execution_session_id="session-1",
            )

    async def _fake_get_action_application_service() -> _FailingActionAgent:
        return _FailingActionAgent()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.persist_terminal_action_status_strict",
        _fake_persist_terminal_action_status_strict,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.deps.get_action_application_service",
        _fake_get_action_application_service,
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_job_root_execution_session_id",
        lambda **_kwargs: pytest.fail("typed result must carry the exact session"),
    )

    async def _record_cleanup(**kwargs) -> ActionCleanupPassResult:  # noqa: ANN003
        calls["cleanup_sessions"].append(kwargs["execution_session_id"])
        return _empty_cleanup_result()

    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.cancel_action_runtime_resources", _record_cleanup
    )
    monkeypatch.setattr(
        f"{RUNTIME_MODULE}.load_action_session_temp_cleanup_receipt",
        lambda **_kwargs: None,
    )

    await _run_action_job(_build_payload())  # type: ignore[arg-type]

    command = calls["persist"][0]["command"]
    assert command.failure_code == "ACTION_COUNTER_INVARIANT_VIOLATION"
    assert command.failure_stage == "running_failed"
    assert command.failure_message_public == "Action execution failed."
    error_event = next(event for event in calls["events"] if event["event"] == "error")
    assert error_event["data"]["error_code"] == "ACTION_COUNTER_INVARIANT_VIOLATION"
    assert (
        error_event["data"]["error_message"]
        == "Action processing encountered an internal error."
    )
    assert calls["cleanup_sessions"] == ["session-1"]
    assert len(calls["persist"]) == 1
