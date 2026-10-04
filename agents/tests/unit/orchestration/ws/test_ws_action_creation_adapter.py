from __future__ import annotations

from typing import Literal
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ActionMessageConflictError,
    DeferredActionMessageResult,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
)
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws.action import ActionFlowMixin
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.orchestration.ws.handler_process_control import (
    WSHandlerProcessControlMixin,
)
from pantaray_agents.schema.action_conversation import ActionStatus
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.schema.websocket import AckEventMessage
from pantaray_agents.schema.websocket.client_messages import ExecuteActionMessage
from pantaray_agents.schema.websocket.server_messages import ErrorMessage
from pantaray_agents.utils.timestamps import normalize_iso8601_utc_z_milliseconds

COMMAND_ID = "11111111-1111-4111-8111-111111111111"
APPROVED_AT = "2026-08-16T01:02:03Z"
DEMO_REF = {
    "project_id": "project-1",
    "display_name": "Demo App",
    "paths": ["/workspace/demo-app"],
    "start": 35,
    "end": 43,
}


class _SessionStore:
    def __init__(self) -> None:
        self.cleared_processes: list[str] = []

    def ensure_process(self, session_id: str, process_id: str) -> None:
        assert session_id == "session-1"
        assert process_id == "process-1"

    def clear_process(self, session_id: str, process_id: str) -> None:
        assert session_id == "session-1"
        self.cleared_processes.append(process_id)


class _FailingSessionStore(_SessionStore):
    def ensure_process(self, session_id: str, process_id: str) -> None:
        raise RuntimeError("session process limit reached")


class _SuggestionRepository:
    def __init__(self) -> None:
        self.row: dict[str, object] = {
            "suggestion_id": "suggestion-1",
            "user_id": "user-1",
            "status": "success",
            "has_suggestion": True,
            "interaction_contract": "action_offer",
            "user_reaction": None,
            "accepted_at": None,
            "answer": "Apply the approved change",
            "suggestion_summary": "Keep the edit narrow",
            "target_context_json": {
                "organization_name": "Wakakusa",
                "project_name": "Pantaray",
            },
            "latest_public_event_sequence": 0,
        }
        self.history_row: dict[str, object] = {"last_sequence": 0}

    async def get_suggestion_state(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[dict[str, object]]:
        assert user_id == "user-1"
        assert suggestion_id == "suggestion-1"
        return RepositoryResult(data=self.row)

    async def get_suggestion_history_row(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[dict[str, object]]:
        assert user_id == "user-1"
        assert suggestion_id == "suggestion-1"
        return RepositoryResult(data=self.history_row)


class _Handler(ActionFlowMixin):
    def __init__(self) -> None:
        self.user_id = "user-1"
        self.session_id = "session-1"
        self.session_store: _SessionStore | _FailingSessionStore = _SessionStore()
        self._repo = _SuggestionRepository()
        self._action_processes: set[str] = set()
        self._process_metadata: dict[str, dict[str, object]] = {}
        self.replayed: list[dict[str, object]] = []
        self.attached: list[dict[str, object]] = []
        self.errors: list[tuple[ErrorMessage, dict[str, object]]] = []
        self.session_errors: list[tuple[ErrorMessage, dict[str, object]]] = []
        self.attach_error: Exception | None = None

    async def _get_action_state_repository(self) -> _SuggestionRepository:
        return self._repo

    async def _replay_action_requested(self, **kwargs: object) -> None:
        self.replayed.append(dict(kwargs))

    async def _attach_action_process_to_current_session(self, **kwargs: object) -> None:
        if self.attach_error is not None:
            self._bind_action_process(
                str(kwargs["process_id"]),
                str(kwargs["suggestion_id"]),
                str(kwargs["action_id"]),
            )
            raise self.attach_error
        self.attached.append(dict(kwargs))

    async def _send_action_error(self, message: ErrorMessage, **kwargs: object) -> None:
        self.errors.append((message, dict(kwargs)))

    async def send_error(self, message: ErrorMessage, **kwargs: object) -> None:
        self.session_errors.append((message, dict(kwargs)))


class _ProcessControlHandler(WSHandlerProcessControlMixin, _Handler):
    async def _is_suggestion_id_accessible(self, suggestion_id: str) -> bool:
        return suggestion_id == "suggestion-1"


def _configure_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.is_local_runtime_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.now_utc_iso",
        lambda: APPROVED_AT,
    )


def _result(
    *, inserted: bool = True, action_status: ActionStatus = "queued"
) -> SubmitActionMessageResult:
    return StartedActionMessageResult(
        disposition="started",
        action_id="action-1",
        message_id=COMMAND_ID,
        user_step_id="user-step-1",
        action_status=action_status,
        process_id="process-1",
        job_id="job-1",
        inserted=inserted,
    )


@pytest.mark.parametrize("approval_mode", ["prompt_each_time", "always_allow"])
@pytest.mark.asyncio
async def test_execute_action_adapts_suggestion_to_the_canonical_creation_command(
    monkeypatch: pytest.MonkeyPatch,
    approval_mode: Literal["prompt_each_time", "always_allow"],
) -> None:
    _configure_runtime(monkeypatch)
    captured: list[SubmitActionMessageCommand] = []

    def fake_submit_action_message(
        command: SubmitActionMessageCommand,
    ) -> SubmitActionMessageResult:
        captured.append(command)
        return _result()

    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        fake_submit_action_message,
    )
    handler = _Handler()

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode=approval_mode,
            images=(
                {
                    "kind": "image",
                    "storage_path": "user-1/2026-09-11/11111111-1111-4111-8111-111111111111.png",
                },
            ),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
            language="ja",
            # Spans point into the trimmed supplement.
            supplement="  Run only the focused regression in Demo App.",
            supplement_project_refs=(DEMO_REF,),
            files=(
                {
                    "attachment_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                    "name": "spec.docx",
                    "byte_size": 4_096,
                },
            ),
        )
    )

    assert len(captured) == 1
    command = captured[0]
    assert command.user_id == "user-1"
    assert command.target == NewActionTarget(
        suggestion_id="suggestion-1", approval_mode=approval_mode
    )
    message = command.message
    assert message.message_id == COMMAND_ID
    assert message.content == "Apply the approved change"
    assert message.supplement == "Run only the focused regression in Demo App."
    assert message.supplement_project_refs[0].display_name == "Demo App"
    assert render_action_user_request_text(message).endswith(
        "Referenced workspace projects:\n- Demo App: /workspace/demo-app\n\n"
        "Attached files:\n"
        "The user attached these files to this message. Each is saved at the path "
        "shown, relative to your workspace cwd. Open one with the `read` tool at "
        "that path; the pages of a PDF, Word, PowerPoint or Excel file can also "
        "be viewed with `render_pdf_page`. These "
        "formats are readable: do not tell the user they are unsupported, and do "
        "not ask them to paste the contents.\n"
        "- spec.docx (Word document, 4.0 KB): "
        "attachments/0f8fad5b-d9cb-469f-a165-70867728950e/spec.docx"
    )
    assert message.language == "ja"
    assert (
        message.images[0].storage_path
        == "user-1/2026-09-11/11111111-1111-4111-8111-111111111111.png"
    )
    assert message.suggestion_approval.approved_at == APPROVED_AT
    assert message.suggestion_approval.summary == "Keep the edit narrow"
    assert message.suggestion_approval.organization_name == "Wakakusa"
    assert message.suggestion_approval.project_name == "Pantaray"
    assert handler.replayed == [
        {
            "suggestion_id": "suggestion-1",
            "command_id": COMMAND_ID,
            "accepted_at": APPROVED_AT,
        }
    ]
    assert handler.attached[0]["action_id"] == "action-1"
    assert handler.attached[0]["logical_run_id"] == "process-1"
    assert handler.errors == []
    assert handler.session_errors == []


@pytest.mark.asyncio
async def test_execute_action_stamps_a_pending_approval_in_canonical_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The approval time is stored with the Suggestion, so it must use the
    # canonical storage form rather than whatever the clock formats.
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.is_local_runtime_enabled",
        lambda: True,
    )
    calls: list[SubmitActionMessageCommand] = []
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda command: calls.append(command) or _result(),
    )

    await _Handler().execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
        )
    )

    approval = calls[0].message.suggestion_approval
    assert approval is not None
    assert (
        normalize_iso8601_utc_z_milliseconds(approval.approved_at)
        == approval.approved_at
    )


@pytest.mark.asyncio
async def test_execute_action_replay_uses_the_same_function_without_a_second_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    calls: list[SubmitActionMessageCommand] = []
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda command: calls.append(command) or _result(inserted=False),
    )
    handler = _ProcessControlHandler()
    handler._repo.row.update(
        user_reaction="accepted",
        action_status="processing",
        action_command_id=COMMAND_ID,
    )

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
            supplement="Keep the accepted constraints.",
        )
    )

    assert len(calls) == 1
    assert calls[0].message.supplement == "Keep the accepted constraints."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_status", "projected_process_id", "expected_event"),
    [
        ("success", "process-1", "process_completed"),
        ("error", "process-1", "process_completed"),
        ("canceled", "process-1", "process_completed"),
        ("success", "continued-process", "error"),
    ],
)
async def test_terminal_replay_uses_submit_identity_without_reloading_detail(
    monkeypatch: pytest.MonkeyPatch,
    action_status: ActionStatus,
    projected_process_id: str,
    expected_event: str,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda _command: _result(inserted=False, action_status=action_status),
    )
    repository = _SuggestionRepository()
    terminal_row = {
        **repository.row,
        "action_id": "action-1",
        "action_process_id": projected_process_id,
        "action_status": action_status,
    }
    repository.row.update(action_status="processing")
    repository.get_suggestion_state = AsyncMock(
        side_effect=[
            RepositoryResult(data=repository.row),
            RepositoryResult(data=repository.row),
            RepositoryResult(data=terminal_row),
        ]
    )
    repository.get_suggestion_history_row = AsyncMock(
        side_effect=AssertionError("terminal detail must not be reloaded")
    )
    store = InMemorySessionStore(max_age_seconds=3600)
    store.create_session("session-1", user_id="user-1")
    websocket = AsyncMock(client_state=WebSocketState.CONNECTED)
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=store,
        session_id="session-1",
        user_id="user-1",
    )
    monkeypatch.setattr(
        handler,
        "_get_action_state_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        handler,
        "_is_suggestion_id_accessible",
        AsyncMock(return_value=True),
    )

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
        )
    )

    terminal_event = websocket.send_json.await_args.args[0]
    assert terminal_event["event"] == expected_event
    if expected_event == "error":
        assert terminal_event["meta"]["failure_kind"] == (
            "replay_process_completed_identity_mismatch"
        )
        return
    assert terminal_event["data"] == {
        "kind": "action",
        "process_id": "process-1",
        "suggestion_id": "suggestion-1",
        "action_id": "action-1",
        "command_id": COMMAND_ID,
        "status": action_status,
    }
    process = store.get("session-1").processes["process-1"]
    assert process.completed_at is not None
    assert terminal_event["event_id"] in store.get("session-1").events

    await handler.handle_ack(
        AckEventMessage(
            session_id="session-1",
            process_id="process-1",
            event_id=terminal_event["event_id"],
        )
    )
    assert store.get_process_metadata("session-1", "process-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "expected_replay_count"),
    [("pending", 1), ("not_executed", 0)],
)
async def test_execute_action_does_not_replay_or_attach_deferred_submission(
    monkeypatch: pytest.MonkeyPatch,
    disposition: Literal["pending", "not_executed"],
    expected_replay_count: int,
) -> None:
    _configure_runtime(monkeypatch)
    result = DeferredActionMessageResult(
        disposition=disposition,
        action_id="action-1",
        message_id=COMMAND_ID,
        user_step_id="user-step-1",
        action_status="processing" if disposition == "pending" else "canceled",
        process_id=None,
        job_id=None,
        inserted=False,
    )
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda _command: result,
    )
    handler = _Handler()

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
        )
    )

    assert handler.attached == []
    assert len(handler.replayed) == expected_replay_count
    assert handler.errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_enabled", "create_failure", "expected_code"),
    [
        (False, None, "LOCAL_RUNTIME_REQUIRED"),
        (True, ActionMessageConflictError("conflict"), "WS_ACTION_NOT_ALLOWED"),
        (
            True,
            ActionFileAttachmentUnavailableError("staged file is gone"),
            "WS_ACTION_ATTACHMENT_UNAVAILABLE",
        ),
        (True, RuntimeError("database unavailable"), "WS_DEPENDENCY_UNAVAILABLE"),
    ],
)
async def test_execute_action_fails_closed_before_relay_attachment(
    monkeypatch: pytest.MonkeyPatch,
    runtime_enabled: bool,
    create_failure: Exception | None,
    expected_code: str,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.is_local_runtime_enabled",
        lambda: runtime_enabled,
    )

    def fake_submit_action_message(_command: object) -> SubmitActionMessageResult:
        if create_failure is not None:
            raise create_failure
        return _result()

    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        fake_submit_action_message,
    )
    handler = _Handler()

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
        )
    )

    assert handler.attached == []
    assert handler.errors
    message, action_error_kwargs = handler.errors[-1]
    assert message.error_code == expected_code
    assert action_error_kwargs["action_stage"] == "preflight_rejected"
    assert handler.session_errors == []


@pytest.mark.asyncio
async def test_execute_action_rejects_legacy_oversized_suggestion_as_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda _command: pytest.fail("invalid command must not reach persistence"),
    )
    handler = _Handler()
    handler._repo.row["answer"] = "x" * (ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1)

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
        )
    )

    assert handler.attached == []
    assert handler.errors[-1][0].error_code == "WS_ACTION_NOT_ALLOWED"
    assert handler.session_errors == []


@pytest.mark.asyncio
async def test_execute_action_rejects_a_project_ref_outside_its_supplement_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda _command: pytest.fail("invalid command must not reach persistence"),
    )
    handler = _Handler()

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
            supplement="Check Demo App.",
            supplement_project_refs=(DEMO_REF,),
        )
    )

    assert handler.attached == []
    assert handler.errors[-1][0].error_code == "WS_ACTION_NOT_ALLOWED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("session_limit", "WS_PROCESS_LIMIT_EXCEEDED"),
        ("relay_attach", "ACTION_RELAY_ATTACH_FAILED"),
    ],
)
async def test_post_commit_attach_failure_is_session_error_not_action_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message",
        lambda _command: _result(),
    )
    handler = _Handler()
    if failure == "session_limit":
        handler.session_store = _FailingSessionStore()
    else:
        handler.attach_error = RuntimeError("relay unavailable")

    await handler.execute_action(
        ExecuteActionMessage(
            approval_mode="prompt_each_time",
            images=(),
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
        )
    )

    assert handler.errors == []
    assert len(handler.session_errors) == 1
    assert handler.session_store.cleared_processes == ["process-1"]
    assert handler._action_processes == set()
    assert handler._process_metadata == {}
    message, send_kwargs = handler.session_errors[0]
    assert message.error_code == expected_code
    assert send_kwargs == {
        "meta": {
            "kind": "session",
            "stage": "action_live_attach_failed",
            "error_code": expected_code,
        },
        "persist_public_event": False,
    }


@pytest.mark.parametrize(
    "path",
    [
        "other-user/2026-09-11/11111111-1111-4111-8111-111111111111.png",
        "user-1/../../private.png",
    ],
)
@pytest.mark.asyncio
async def test_suggestion_images_reject_foreign_or_escaping_paths(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _configure_runtime(monkeypatch)
    submit = AsyncMock()
    monkeypatch.setattr(
        "pantaray_agents.orchestration.ws.action.submit_action_message", submit
    )
    handler = _Handler()
    await handler.execute_action(
        ExecuteActionMessage(
            suggestion_id="suggestion-1",
            command_id=COMMAND_ID,
            approval_mode="always_allow",
            images=({"kind": "image", "storage_path": path},),
        )
    )
    submit.assert_not_called()
    assert handler.errors
    assert not handler.attached
