from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pantaray_agents.local_runtime.runtime.action_subagent_approval import (
    ActionSubagentApprovalDecisionError,
    apply_action_subagent_approval_decision,
)
from pantaray_agents.local_runtime.runtime.action_subagent_cancel import (
    ActionSubagentCancelRequest,
    request_action_subagent_cancellation,
)
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ActionSubagentMessageRequest,
    send_action_subagent_message,
)
from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    ACTION_SUBAGENT_REPORT_MAX_BYTES,
    ActionSubagentReportError,
    build_action_subagent_success_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    read_action_subagent_wait_snapshot,
)
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    bind_local_runtime_db_execution_context,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    UnprojectedBrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ValidatedCommandRequest,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    create_capability_grant,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.sandbox.sandbox_denial import (
    WRITE_FOLDER_REQUEST_HINT,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.agent.action_subagent import ActionSubagentWaitRequest
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs import action_subagent as subagent_job
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.utils.llm_types import GenerateContentConfig
from pantaray_llm.contracts.conversation import LlmTurnToolResultItem
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolUseRequest,
)
from pantaray_llm.errors import (
    PROXY_LLM_TOOL_CALL_INVALID,
    LlmProxyExecutionError,
)
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

TIMESTAMP = "2026-09-01T00:00:00Z"
THINKING_MARKER = "PRIVATE_THINKING_MUST_NOT_PERSIST"


class _Models:
    def __init__(self, client: _Client) -> None:
        self.client = client

    async def generate_content(self, **kwargs: object) -> object:
        config = kwargs["config"]
        assert isinstance(config, GenerateContentConfig)
        self.client.profiles.append(str(config.inference_profile))
        self.client.prompts.append(str(kwargs["contents"]))
        self.client.system_instructions.append(str(config.system_instruction))
        self.client.tool_uses.append(config.tool_use)
        if self.client.on_call is not None:
            self.client.on_call(len(self.client.prompts))
        if self.client.failures:
            self.client.failures -= 1
            raise LlmProxyExecutionError(
                error_code="PROXY_UPSTREAM_UNAVAILABLE",
                error_message="temporary upstream failure",
                retryable=self.client.retryable,
            )
        call_index = len(self.client.prompts) - 1
        tool_call = (
            self.client.calls[min(call_index, len(self.client.calls) - 1)]
            if self.client.calls
            else LlmToolCall(
                call_id="call-1",
                name="submit_subagent_report",
                arguments={
                    "report": self.client.reports[
                        min(len(self.client.prompts), len(self.client.reports)) - 1
                    ]
                },
            )
        )
        return SimpleNamespace(
            tool_calls=tool_call if isinstance(tool_call, tuple) else (tool_call,),
            dropped_tool_call_names=(),
            tool_continuation=None,
            usage_metadata=None,
            thinking=THINKING_MARKER,
        )


class _Client:
    def __init__(
        self,
        *,
        reports: tuple[JSONValue, ...] = ("Child report",),
        failures: int = 0,
        retryable: bool = True,
        on_call: Callable[[int], None] | None = None,
        # One response per entry; a tuple is one response with several calls.
        calls: tuple[LlmToolCall | tuple[LlmToolCall, ...], ...] = (),
    ) -> None:
        self.reports = reports
        self.failures = failures
        self.retryable = retryable
        self.on_call = on_call
        self.calls = calls
        self.profiles: list[str] = []
        self.prompts: list[str] = []
        self.system_instructions: list[str] = []
        self.tool_uses: list[LlmToolUseRequest | None] = []
        self.aio = SimpleNamespace(models=_Models(self))


def _running_child(
    tmp_path: Path,
    *,
    profile_id: str,
    claim_workspace: bool = False,
    task: str = "Inspect the assigned boundary",
) -> tuple[Path, ActionSubagentJobPayload]:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    payload = build_action_subagent_job_payload(
        {
            "job_id": "child-job",
            "process_id": "child-process",
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": "parent-process",
            "inference_profile_id": profile_id,
            "action_context": "# Workspace Paths\nparent context",
            "task": task,
            "context_refs": ["conversation:step-1"],
            "resource_claim_ids": ["child-claim"] if claim_workspace else [],
        }
    )
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, 1_000)
        with connection:
            connection.execute(
                "INSERT INTO users(user_id,ui_language,created_at,updated_at) "
                "VALUES ('user-1','ja',?,?)",
                (TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO agent_actions(action_id,user_id,initial_user_message_id,"
                "execution_target_json,status,final_output,prompt_name,prompt_version,"
                "created_at,updated_at) VALUES "
                "('action-1','user-1','message-1','{\"kind\":\"scratch\"}',"
                "'processing','','action','1',?,?)",
                (TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO processes(process_id,user_id,kind,status,action_id,"
                "current_job_id,started_at,updated_at,heartbeat_at,next_event_seq) VALUES "
                "('parent-process','user-1','action','running','action-1',"
                "'parent-job',?,?,?,1)",
                (TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,"
                "scheduled_at,started_at,logical_key) VALUES "
                "('parent-job','user-1','execute_action','parent-process','running',"
                "1,?,?, 'action-1')",
                (TIMESTAMP, TIMESTAMP),
            )
            enqueue_action_subagent_job_in_connection(
                connection=connection,
                payload=payload,
                scheduled_at=TIMESTAMP,
            )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=TIMESTAMP,
        allowed_tool_ids=("read", "list", "glob", "grep", "apply_patch", "bash"),
    )
    if claim_workspace:
        with sqlite3.connect(db_path) as connection:
            configure_connection(connection, 1_000)
            with connection:
                connection.execute(
                    "INSERT INTO action_subagent_resource_claims(claim_id,user_id,"
                    "action_id,parent_process_id,child_process_id,resource_kind,"
                    "root_identity,normalized_key,acquired_at) VALUES('child-claim',"
                    "'user-1','action-1','parent-process','child-process',"
                    "'workspace_path',?,?,?)",
                    (
                        context.manifest_id,
                        str(context.workspace_path.resolve()),
                        TIMESTAMP,
                    ),
                )
    _claim_child(db_path)
    return db_path, payload


def _claim_child(db_path: Path) -> None:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type="execute_action_subagent",
        owner_user_id="user-1",
        claimed_by="test-worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None


def _workspace(db_path: Path) -> Path:
    with sqlite3.connect(db_path) as connection:
        return Path(
            connection.execute(
                "SELECT cwd_path FROM execution_sessions WHERE action_id='action-1'"
            ).fetchone()[0]
        )


def _request_cancel(db_path: Path, payload: ActionSubagentJobPayload) -> None:
    request_action_subagent_cancellation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ActionSubagentCancelRequest(
            user_id=payload["user_id"],
            action_id=payload["action_id"],
            parent_process_id=payload["parent_process_id"],
            parent_job_id="parent-job",
            child_process_id=payload["process_id"],
        ),
    )


def _assert_canceled(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        statuses = connection.execute(
            "SELECT job.status,process.status,attempt.status FROM jobs AS job "
            "JOIN processes AS process ON process.process_id=job.process_id "
            "JOIN job_attempts AS attempt ON attempt.job_id=job.job_id "
            "WHERE job.job_id='child-job'"
        ).fetchone()
        terminal = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id='child-process' AND event_name='stream_end'"
        ).fetchone()
    assert statuses == ("canceled", "canceled", "canceled")
    assert terminal is not None and json.loads(terminal[0]) == {"outcome": "canceled"}


@pytest.mark.parametrize(
    ("profile_id", "proxy_failures", "terminal_failures"),
    (
        (SUBAGENT_MODEL_SETTINGS[0].profile_id, 1, 1),
        (SUBAGENT_MODEL_SETTINGS[1].profile_id, 0, 0),
    ),
)
def test_configured_profiles_retry_exactly_and_persist_only_private_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_id: str,
    proxy_failures: int,
    terminal_failures: int,
) -> None:
    db_path, payload = _running_child(tmp_path, profile_id=profile_id)
    client = _Client(failures=proxy_failures)
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    real_finalize = subagent_job.finalize_action_subagent_terminal
    finalize_calls = 0

    def flaky_finalize(**kwargs: object) -> None:
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls <= terminal_failures:
            raise sqlite3.OperationalError("database is locked")
        real_finalize(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        subagent_job, "finalize_action_subagent_terminal", flaky_finalize
    )
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert client.profiles == [profile_id] * (proxy_failures + 1)
    assert finalize_calls == terminal_failures + 1
    with sqlite3.connect(db_path) as connection:
        job = connection.execute(
            "SELECT status,completed_at,error_code FROM jobs WHERE job_id='child-job'"
        ).fetchone()
        process = connection.execute(
            "SELECT status,current_job_id,terminal_event_id,completed_at,"
            "result_collected_at FROM processes WHERE process_id='child-process'"
        ).fetchone()
        attempt = connection.execute(
            "SELECT status,completed_at,error_code,error_message FROM job_attempts"
        ).fetchone()
        event = connection.execute(
            "SELECT event_id,event_name,payload_json,created_at FROM process_events"
        ).fetchone()
        public_count = connection.execute(
            "SELECT COUNT(*) FROM agent_process_events"
        ).fetchone()[0]
        step_count = connection.execute(
            "SELECT COUNT(*) FROM agent_action_steps"
        ).fetchone()[0]
        action = connection.execute(
            "SELECT status,final_output FROM agent_actions WHERE action_id='action-1'"
        ).fetchone()

    assert job is not None and process is not None and attempt is not None
    assert event is not None
    assert job == ("completed", event[3], None)
    assert process == ("completed", None, event[0], event[3], None)
    assert attempt == ("completed", event[3], None, None)
    assert event[1] == "stream_end"
    assert json.loads(event[2]) == {"outcome": "success", "report": "Child report"}
    assert THINKING_MARKER not in event[2]
    assert (public_count, step_count, action) == (0, 0, ("processing", ""))


def test_unknown_profile_fails_before_proxy_and_closes_private_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(tmp_path, profile_id="removed.profile")

    def build_proxy() -> object:
        raise AssertionError("proxy must not be built")

    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", build_proxy)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        with pytest.raises(
            subagent_job.ActionSubagentJobFailed,
            match="ACTION_SUBAGENT_PROFILE_UNAVAILABLE",
        ):
            subagent_job.run_action_subagent_job(payload)

    with sqlite3.connect(db_path) as connection:
        event = connection.execute(
            "SELECT payload_json FROM process_events WHERE process_id='child-process'"
        ).fetchone()
        statuses = connection.execute(
            "SELECT j.status,p.status,a.status FROM jobs AS j "
            "JOIN processes AS p ON p.process_id=j.process_id "
            "JOIN job_attempts AS a ON a.job_id=j.job_id"
        ).fetchone()
    assert event is not None
    assert json.loads(event[0]) == {
        "outcome": "failure",
        "error_code": "ACTION_SUBAGENT_PROFILE_UNAVAILABLE",
    }
    assert statuses == ("failed", "failed", "failed")


def test_cancel_before_proxy_closes_without_building_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    _request_cancel(db_path, payload)

    def build_proxy() -> object:
        raise AssertionError("canceled child must not build a proxy client")

    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", build_proxy)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    _assert_canceled(db_path)


def test_cancel_after_llm_response_discards_late_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )

    def cancel_on_response(call_number: int) -> None:
        if call_number == 1:
            _request_cancel(db_path, payload)

    client = _Client(on_call=cancel_on_response)
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 1
    _assert_canceled(db_path)


@pytest.mark.parametrize(
    "retryable",
    (True, False),
    ids=("before-internal-retry", "failed-attempt-terminal-race"),
)
def test_cancel_during_proxy_failure_stops_and_replays_canceled_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retryable: bool,
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    client = _Client(
        failures=1,
        retryable=retryable,
        on_call=lambda _call_number: _request_cancel(db_path, payload),
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 1
    _assert_canceled(db_path)


def test_cancel_after_tool_return_preserves_tool_then_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    real_append = subagent_job.append_action_subagent_tool_transcript

    def append_then_cancel(**kwargs: object) -> None:
        real_append(**kwargs)  # type: ignore[arg-type]
        _request_cancel(db_path, payload)

    client = _Client(reports=(1,))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    monkeypatch.setattr(
        subagent_job,
        "append_action_subagent_tool_transcript",
        append_then_cancel,
    )
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    with sqlite3.connect(db_path) as connection:
        event_names = connection.execute(
            "SELECT event_name FROM process_events ORDER BY event_seq"
        ).fetchall()
    assert event_names == [("action_subagent_tool",), ("stream_end",)]
    _assert_canceled(db_path)


def test_cancel_between_tool_and_next_proxy_stops_second_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    real_load = subagent_job.load_action_subagent_transcript
    prompt_builds = 0

    def load_then_cancel(**kwargs: object) -> tuple[object, ...]:
        nonlocal prompt_builds
        transcript = real_load(**kwargs)  # type: ignore[arg-type]
        prompt_builds += 1
        if prompt_builds == 2:
            _request_cancel(db_path, payload)
        return transcript

    client = _Client(reports=(1, "must not be submitted"))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    monkeypatch.setattr(
        subagent_job,
        "load_action_subagent_transcript",
        load_then_cancel,
    )
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 1
    _assert_canceled(db_path)


def test_message_between_turns_rebuilds_the_durable_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )

    def accept_message(call_number: int) -> None:
        if call_number == 1:
            send_action_subagent_message(
                db_path=db_path,
                busy_timeout_ms=1_000,
                request=ActionSubagentMessageRequest(
                    "user-1",
                    "action-1",
                    "parent-process",
                    "parent-job",
                    "child-process",
                    "message-between-turns",
                    "Use the corrected evidence",
                ),
            )

    client = _Client(reports=("界" * 90_000, "Child report"), on_call=accept_message)
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 2
    # The first turn already carries the assigned task behind the context.
    assert _items(client, 0).count("Inspect the assigned boundary") == 1
    assert _items(client, 1).count("Use the corrected evidence") == 1
    assert _results(client, 1) == [("submit_subagent_report", "error")]
    assert "界" not in _items(client, 1)
    # Nothing is carried between turns, so the child asks the adapter to build no
    # continuation either.
    assert all(
        request is not None
        and request.continuation_mode == "disabled"
        and request.continuation is None
        and request.tool_result is None
        for request in client.tool_uses
    )
    with sqlite3.connect(db_path) as connection:
        events = connection.execute(
            "SELECT event_name FROM process_events ORDER BY event_seq"
        ).fetchall()
    assert [event[0] for event in events] == [
        "action_subagent_message",
        "action_subagent_tool",
        "stream_end",
    ]


def test_invalid_report_repairs_stop_after_three_llm_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    client = _Client(reports=(1, 2, 3, "must not be requested"))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)

    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        with pytest.raises(
            subagent_job.ActionSubagentJobFailed,
            match=subagent_job.ACTION_SUBAGENT_EXECUTION_FAILED,
        ):
            subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 3
    with sqlite3.connect(db_path) as connection:
        events = connection.execute(
            "SELECT event_name FROM process_events ORDER BY event_seq"
        ).fetchall()
    assert events == [
        ("action_subagent_tool",),
        ("action_subagent_tool",),
        ("stream_end",),
    ]


@pytest.mark.parametrize(
    ("content", "prompt_fragment"),
    (
        ("broker evidence", "broker evidence"),
        (
            ("x" * 250 + "\n") * 100,
            '"content":"' + "x" * 250,
        ),
    ),
    ids=("complete", "paged"),
)
def test_broker_read_is_durable_in_the_next_fresh_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
    prompt_fragment: str,
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    (_workspace(db_path) / "evidence.txt").write_text(content, encoding="utf-8")
    client = _Client(
        calls=(
            LlmToolCall(
                call_id="call-read", name="read", arguments={"path": "evidence.txt"}
            ),
            LlmToolCall(
                call_id="call-report",
                name="submit_subagent_report",
                arguments={"report": "Child report"},
            ),
        )
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)

    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 2
    assert content not in client.prompts[0]
    assert _results(client, 1) == [("read", "completed")]
    assert _items(client, 1).count(prompt_fragment) == 1
    assert all(
        request is not None
        and request.continuation_mode == "disabled"
        and request.continuation is None
        and request.tool_result is None
        for request in client.tool_uses
    )
    with sqlite3.connect(db_path) as connection:
        invocation = connection.execute(
            "SELECT invocations.tool_id,invocations.status,invocations.started_at,"
            "outputs.output_storage_kind FROM tool_invocations AS invocations "
            "JOIN tool_outputs AS outputs "
            "ON outputs.invocation_id=invocations.invocation_id"
        ).fetchone()
    assert invocation is not None
    assert invocation[:2] == ("read", "completed")
    assert invocation[2] != "unknown"
    assert invocation[3] == "inline_json"


def test_broker_attachment_read_fails_instead_of_reporting_unusable_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    (_workspace(db_path) / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(32))
    client = _Client(
        calls=(
            LlmToolCall(
                call_id="call-read", name="read", arguments={"path": "diagram.png"}
            ),
            LlmToolCall(
                call_id="call-report",
                name="submit_subagent_report",
                arguments={"report": "Child report"},
            ),
        )
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)

    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 2
    assert "Image read successfully" not in _items(client, 1)
    assert _results(client, 1) == [("read", "error")]
    assert _items(client, 1).count("SUBAGENT_ATTACHMENT_READ_UNSUPPORTED") == 1
    with sqlite3.connect(db_path) as connection:
        invocation = connection.execute(
            "SELECT tool_id,status FROM tool_invocations"
        ).fetchone()
    assert invocation == ("read", "completed")


def test_report_bound_is_utf8_bytes_and_terminal_failure_rolls_back(
    tmp_path: Path,
) -> None:
    report = "🧪" * (ACTION_SUBAGENT_REPORT_MAX_BYTES // 4)
    assert build_action_subagent_success_result(report)["report"] == report
    with pytest.raises(ActionSubagentReportError, match="UTF-8 bytes"):
        build_action_subagent_success_result(report + "a")

    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_child_terminal BEFORE UPDATE OF status ON jobs "
            "WHEN NEW.job_id='child-job' BEGIN "
            "SELECT RAISE(ABORT, 'forced terminal failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced terminal failure"):
        finalize_action_subagent_terminal(
            db_path=db_path,
            busy_timeout_ms=1_000,
            payload=payload,
            result=build_action_subagent_success_result("report"),
        )
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM process_events").fetchone()[0] == 0
        )
        assert connection.execute(
            "SELECT status,terminal_event_id FROM processes "
            "WHERE process_id='child-process'"
        ).fetchone() == ("running", None)
        assert connection.execute(
            "SELECT status FROM job_attempts WHERE job_id='child-job'"
        ).fetchone() == ("running",)


_APPLY_PATCH_CALL = LlmToolCall(
    call_id="call-patch",
    name="apply_patch",
    arguments={
        "changes": [
            {
                "op": "add",
                "path": "gated.txt",
                "new_lines": ["approved"],
                "trailing_newline": True,
            }
        ]
    },
)
_REPORT_CALL = LlmToolCall(
    call_id="call-report",
    name="submit_subagent_report",
    arguments={"report": "Child report"},
)


def _pause_gated_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, ActionSubagentJobPayload, str, str]:
    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    client = _Client(calls=(_APPLY_PATCH_CALL,))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)
    assert len(client.prompts) == 1
    with sqlite3.connect(db_path) as connection:
        session = connection.execute(
            "SELECT approval_session_id,tool_request_id,status,tool_id,claimed_at "
            "FROM approval_sessions"
        ).fetchone()
    assert session is not None
    assert tuple(session[2:]) == ("pending", "apply_patch", None)
    return db_path, payload, str(session[0]), str(session[1])


def _decide(db_path: Path, session_id: str, request_id: str, decision: str) -> None:
    apply_action_subagent_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        child_process_id="child-process",
        approval_session_id=session_id,
        tool_request_id=request_id,
        decision=decision,  # type: ignore[arg-type]
    )


def _resume_child(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, payload: ActionSubagentJobPayload
) -> _Client:
    _claim_child(db_path)
    client = _Client(calls=(_REPORT_CALL,))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)
    return client


def _child_events(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event_name FROM process_events WHERE process_id='child-process' "
                "ORDER BY event_seq"
            ).fetchall()
        ]


def test_gated_tool_pause_writes_one_anchor_without_a_false_error_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, _payload, session_id, request_id = _pause_gated_child(
        monkeypatch, tmp_path
    )

    with sqlite3.connect(db_path) as connection:
        anchor = connection.execute(
            "SELECT event_name,"
            "json_extract(payload_json,'$.approval_blockers[0].tool_id'),"
            "json_extract(payload_json,'$.approval_blockers[0].approval_session_id'),"
            "json_extract(payload_json,'$.approval_blockers[0].tool_request_id'),"
            "json_extract(payload_json,'$.approval_blockers[0].intent_class'),"
            "json_extract(payload_json,'$.tool_arguments.changes[0].path') "
            "FROM process_events WHERE process_id='child-process'"
        ).fetchall()
        statuses = connection.execute(
            "SELECT job.status,process.status,attempt.status FROM jobs AS job "
            "JOIN processes AS process ON process.process_id=job.process_id "
            "JOIN job_attempts AS attempt ON attempt.job_id=job.job_id "
            "WHERE job.job_id='child-job'"
        ).fetchone()
        invocations = connection.execute(
            "SELECT COUNT(*) FROM tool_invocations"
        ).fetchone()[0]
    assert anchor == [
        (
            "process_paused",
            "apply_patch",
            session_id,
            request_id,
            "surgical_edit",
            "gated.txt",
        )
    ]
    assert statuses == ("paused", "paused", "completed")
    assert invocations == 0
    assert not (_workspace(db_path) / "gated.txt").exists()


@pytest.mark.parametrize(
    ("decision", "patched", "invocations", "claimed", "fragment"),
    (
        (
            "approved_once",
            "approved\n",
            [("apply_patch", "completed")],
            1,
            '"result":{"status":"success"',
        ),
        ("denied", None, [], 0, '"kind":"approval_denied"'),
    ),
    ids=("approve", "deny"),
)
def test_decided_gated_tool_settles_the_saved_request_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: str,
    patched: str | None,
    invocations: list[tuple[str, str]],
    claimed: int,
    fragment: str,
) -> None:
    db_path, payload, session_id, request_id = _pause_gated_child(monkeypatch, tmp_path)
    _decide(db_path, session_id, request_id, decision)

    client = _resume_child(monkeypatch, db_path, payload)

    gated = _workspace(db_path) / "gated.txt"
    assert (gated.read_text(encoding="utf-8") if gated.exists() else None) == patched
    # Whichever way it was decided, the saved request is settled once and the
    # child reads one result for it.
    assert _results(client, 0) == [("apply_patch", "completed")]
    assert _items(client, 0).count(fragment) == 1
    assert _child_events(db_path) == [
        "process_paused",
        "action_subagent_tool",
        "stream_end",
    ]
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT tool_id,status FROM tool_invocations").fetchall()
            == invocations
        )
        assert connection.execute(
            "SELECT status,claimed_at IS NOT NULL FROM approval_sessions"
        ).fetchone() == (decision, claimed)


def test_cancel_before_the_gated_pause_commits_settles_canceled_and_interrupts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    real_pause = subagent_job.pause_action_subagent_for_approval

    def cancel_then_pause(**kwargs: object) -> object:
        _request_cancel(db_path, payload)
        return real_pause(**kwargs)  # type: ignore[arg-type]

    client = _Client(calls=(_APPLY_PATCH_CALL,))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    monkeypatch.setattr(
        subagent_job, "pause_action_subagent_for_approval", cancel_then_pause
    )
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert _child_events(db_path) == ["stream_end"]
    assert not (_workspace(db_path) / "gated.txt").exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM approval_sessions"
        ).fetchone() == ("interrupted",)
    _assert_canceled(db_path)


_BASH_CALL = LlmToolCall(
    call_id="call-bash", name="bash", arguments={"command": "pwd", "cwd": "."}
)


def test_wait_reports_an_approval_paused_child_as_nonterminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, _payload, _session_id, _request_id = _pause_gated_child(
        monkeypatch, tmp_path
    )

    snapshot = read_action_subagent_wait_snapshot(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ActionSubagentWaitRequest(
            user_id="user-1",
            action_id="action-1",
            parent_process_id="parent-process",
            parent_job_id="parent-job",
            child_process_ids=("child-process",),
        ),
    )

    assert snapshot.results == (
        {"child_process_id": "child-process", "status": "nonterminal"},
    )
    assert snapshot.terminal_child_process_ids == ()


def _allow_workspace_commands(db_path: Path) -> None:
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-global",
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("workspace_edit_and_command",),
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=1_000,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-process-exec",
            user_id="user-1",
            preference_id="pref-global",
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at=TIMESTAMP,
        ),
    )


def test_failed_command_output_stays_bounded_in_the_child_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    _allow_workspace_commands(db_path)
    raw_output = "e" * (10 * ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT)

    async def failed_command(**_kwargs: object) -> UnprojectedBrokerToolOutcome:
        return UnprojectedBrokerToolOutcome(
            status="error",
            output={
                "status": "error",
                "exit_code": 1,
                "stdout": "",
                "stderr": raw_output,
                "error": {
                    "type": "CommandExecutionError",
                    "message": raw_output,
                    "exit_code": 1,
                },
            },
            stdout_text="",
            stderr_text=raw_output,
        )

    monkeypatch.setattr(broker_module, "run_command_via_sandbox", failed_command)
    client = _Client(calls=(_BASH_CALL, _REPORT_CALL))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 2
    assert _results(client, 1) == [("bash", "error")]
    assert raw_output not in _items(client, 1)
    with sqlite3.connect(db_path) as connection:
        transcript = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id='child-process' AND event_name='action_subagent_tool'"
        ).fetchone()
    assert transcript is not None
    assert raw_output not in transcript[0]
    assert transcript[0].count("[truncated]") == 2
    # The entry stays bounded however large the command output was.
    assert len(transcript[0]) < 3 * ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT


def test_a_likely_denied_write_does_not_tell_the_child_to_request_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A subagent's folder request is refused without asking, so the hint the
    # broker adds for the parent Action would only send it into that refusal.
    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    _allow_workspace_commands(db_path)
    stderr = "Error: Operation not permitted (os error 1)\n"

    async def denied_command(**_kwargs: object) -> UnprojectedBrokerToolOutcome:
        return UnprojectedBrokerToolOutcome(
            status="error",
            output={
                "status": "error",
                "exit_code": 1,
                "stdout": "",
                "stderr": stderr,
                "error": {
                    "type": "CommandExecutionError",
                    "message": stderr,
                    "llm_feedback": WRITE_FOLDER_REQUEST_HINT,
                    "exit_code": 1,
                },
            },
            stdout_text="",
            stderr_text=stderr,
        )

    monkeypatch.setattr(broker_module, "run_command_via_sandbox", denied_command)
    client = _Client(calls=(_BASH_CALL, _REPORT_CALL))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert _results(client, 1) == [("bash", "error")]
    assert "Operation not permitted" in _items(client, 1)
    assert "additional_write_folders" not in _items(client, 1)


def _enqueue_sibling_child(db_path: Path) -> None:
    sibling = build_action_subagent_job_payload(
        {
            "job_id": "sibling-job",
            "process_id": "sibling-process",
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": "parent-process",
            "inference_profile_id": SUBAGENT_MODEL_SETTINGS[0].profile_id,
            "action_context": "# Workspace Paths\nparent context",
            "task": "Inspect the sibling boundary",
            "context_refs": [],
            "resource_claim_ids": [],
        }
    )
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, 1_000)
        with connection:
            enqueue_action_subagent_job_in_connection(
                connection=connection, payload=sibling, scheduled_at=TIMESTAMP
            )


def _sibling_snapshot(db_path: Path) -> tuple[object, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT job.status,job.cancel_requested_at,process.status,"
            "process.terminal_event_id,(SELECT COUNT(*) FROM process_events "
            "WHERE process_id='sibling-process') FROM jobs AS job "
            "JOIN processes AS process ON process.process_id=job.process_id "
            "WHERE job.job_id='sibling-job'"
        ).fetchone()


@pytest.mark.parametrize("winner", ("cancel", "decision"))
def test_paused_approval_race_settles_one_winner_without_cross_child_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, winner: str
) -> None:
    db_path, payload, session_id, request_id = _pause_gated_child(monkeypatch, tmp_path)
    _enqueue_sibling_child(db_path)
    sibling_before = _sibling_snapshot(db_path)

    if winner == "cancel":
        _request_cancel(db_path, payload)
        with pytest.raises(ActionSubagentApprovalDecisionError):
            _decide(db_path, session_id, request_id, "approved_once")
        expected_session = "interrupted"
    else:
        _decide(db_path, session_id, request_id, "approved_once")
        _request_cancel(db_path, payload)
        expected_session = "approved_once"

    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            "SELECT job.status,process.status,"
            "(SELECT status FROM approval_sessions) FROM jobs AS job "
            "JOIN processes AS process ON process.process_id=job.process_id "
            "WHERE job.job_id='child-job'"
        ).fetchone()
        terminal = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id='child-process' AND event_name='stream_end'"
        ).fetchone()
    assert state == ("canceled", "canceled", expected_session)
    assert json.loads(terminal[0]) == {"outcome": "canceled"}
    assert _child_events(db_path)[-1] == "stream_end"
    assert _sibling_snapshot(db_path) == sibling_before


def _conversation(request: LlmToolUseRequest | None) -> tuple[str, ...]:
    """One turn's items as JSON, which is how a cache prefix is compared."""

    assert request is not None and request.conversation is not None
    return tuple(item.model_dump_json() for item in request.conversation)


def _items(client: _Client, index: int) -> str:
    """Everything one turn sent beside its own message, as one string."""

    return "\n".join(_conversation(client.tool_uses[index]))


def _results(client: _Client, index: int) -> list[tuple[str, object]]:
    """Every tool result that turn carried, as (tool name, status), in order."""

    request = client.tool_uses[index]
    assert request is not None and request.conversation is not None
    return [
        (item.name, cast(dict[str, JSONValue], item.output)["status"])
        for item in request.conversation
        if isinstance(item, LlmTurnToolResultItem)
    ]


def test_the_transcript_is_sent_as_appended_conversation_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    (_workspace(db_path) / "evidence.txt").write_text("broker evidence", "utf-8")

    def message_after_first_turn(call_number: int) -> None:
        if call_number == 1:
            send_action_subagent_message(
                db_path=db_path,
                busy_timeout_ms=1_000,
                request=ActionSubagentMessageRequest(
                    "user-1",
                    "action-1",
                    "parent-process",
                    "parent-job",
                    "child-process",
                    "message-between-turns",
                    "Use the corrected evidence",
                ),
            )

    read_call = LlmToolCall(
        call_id="provider-id", name="read", arguments={"path": "evidence.txt"}
    )
    client = _Client(
        calls=(
            read_call,
            read_call,
            LlmToolCall(
                call_id="provider-id",
                name="submit_subagent_report",
                arguments={"report": "Child report"},
            ),
        ),
        on_call=message_after_first_turn,
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)

    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.tool_uses) == 3
    first, second, third = (
        _conversation(client.tool_uses[index]) for index in range(3)
    )
    # What earns the cache read: the later turn is the earlier one plus items.
    assert second[: len(first)] == first
    assert third[: len(second)] == second
    assert (len(first), len(second), len(third)) == (1, 4, 6)
    # The request's own message is the parent's context alone, byte for byte on
    # every turn, so the durable transcript never re-renders into it.
    assert len(set(client.prompts)) == 1

    conversation = client.tool_uses[2].conversation
    assert conversation is not None
    kinds = [item.type for item in conversation]
    # The assigned task comes first. The parent's message landed while the first
    # turn was still in flight, so it is ordered by the row it took, ahead of
    # the call that turn went on to make.
    assert kinds == [
        "user",
        "user",
        "assistant",
        "tool_result",
        "assistant",
        "tool_result",
    ]
    calls = [
        call for item in conversation if item.type == "assistant" for call in item.calls
    ]
    results = [item for item in conversation if item.type == "tool_result"]
    assert [call.name for call in calls] == ["read", "read"]
    assert [call.arguments for call in calls] == [{"path": "evidence.txt"}] * 2
    # Paired by the transcript's own row identity, not by the id the provider
    # gave a call it has long since forgotten.
    assert [call.call_id for call in calls] == [result.call_id for result in results]
    assert len({call.call_id for call in calls}) == 2
    assert all(call.call_id.startswith("subagent-call-") for call in calls)
    assert all(
        result.name == "read"
        and isinstance(result.output, dict)
        and result.output["status"] == "completed"
        and "broker evidence" in json.dumps(result.output["result"])
        for result in results
    )
    assert (
        conversation[0]
        .content[0]
        .text.startswith("# Assigned Task\nInspect the assigned boundary\n")
    )
    assert conversation[1].content[0].text == "Use the corrected evidence"


def test_the_repair_notice_is_appended_without_touching_what_was_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    (_workspace(db_path) / "evidence.txt").write_text("broker evidence", "utf-8")

    def reject_second_turn(call_number: int) -> None:
        if call_number == 2:
            raise LlmProxyExecutionError(
                error_code=PROXY_LLM_TOOL_CALL_INVALID,
                error_message="two tool calls were returned",
                retryable=False,
                recovery="repair_next_turn",
                tool_call_violation_reason="multiple_calls",
            )

    read_call = LlmToolCall(
        call_id="provider-id", name="read", arguments={"path": "evidence.txt"}
    )
    client = _Client(
        calls=(
            read_call,
            read_call,
            read_call,
            LlmToolCall(
                call_id="provider-id",
                name="submit_subagent_report",
                arguments={"report": "Child report"},
            ),
        ),
        on_call=reject_second_turn,
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)

    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.tool_uses) == 4
    rejected, repaired, after = (
        _conversation(client.tool_uses[index]) for index in (1, 2, 3)
    )
    # The notice is the one item the repaired turn adds, and it is feedback for
    # that attempt alone: it is never recorded, so the turn after it is an
    # append to the request the rejected turn sent.
    assert repaired[: len(rejected)] == rejected
    assert len(repaired) == len(rejected) + 1
    assert after[: len(rejected)] == rejected
    assert [
        any("# Previous Error" in item for item in turn)
        for turn in (rejected, repaired, after)
    ] == [False, True, False]
    # The request's own message never carries it either.
    assert len(set(client.prompts)) == 1
    notice = client.tool_uses[2].conversation[-1]
    assert notice.type == "user"
    assert notice.content[0].text.startswith("# Previous Error\n")
    assert "multiple_calls" in notice.content[0].text


def test_children_of_one_parent_send_its_context_and_differ_only_in_the_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同じ親の子どうしは、system と親の先頭まで同じバイト列で、task だけが違う。"""

    sent = []
    for task in ("Fix module a", "Fix module b"):
        db_path, payload = _running_child(
            tmp_path / task.replace(" ", "-"),
            profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
            task=task,
        )
        client = _Client()
        monkeypatch.setattr(
            subagent_job, "build_local_llm_proxy_client", lambda client=client: client
        )
        with bind_local_runtime_db_execution_context(
            db_path=db_path, busy_timeout_ms=1_000
        ):
            subagent_job.run_action_subagent_job(payload)
        sent.append((client, payload))

    (first, first_payload), (second, _) = sent
    assert first.system_instructions == [subagent_job._subagent_system_instruction()]
    assert first.system_instructions == second.system_instructions
    # The request's own message is the parent's context, byte for byte.
    assert first.prompts == second.prompts == [str([first_payload["action_context"]])]
    first_items, second_items = _items(first, 0), _items(second, 0)
    assert "Fix module a" in first_items and "Fix module a" not in second_items
    assert "Fix module b" in second_items


def test_read_only_calls_of_one_turn_all_run_and_keep_their_own_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """読み取りは 1 ターンでまとめて走り、結果は宣言順にそれぞれの呼び出しへ対応する。"""

    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    workspace = _workspace(db_path)
    (workspace / "a.txt").write_text("alpha contents", "utf-8")
    (workspace / "b.txt").write_text("bravo contents", "utf-8")
    client = _Client(
        calls=(
            (
                LlmToolCall(call_id="p1", name="read", arguments={"path": "a.txt"}),
                LlmToolCall(call_id="p2", name="read", arguments={"path": "b.txt"}),
                # The report must run alone, so this turn answers it as not run.
                _REPORT_CALL,
            ),
            _REPORT_CALL,
        )
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    first = client.tool_uses[0]
    assert first is not None and first.max_parallel_tool_calls > 1
    conversation = client.tool_uses[1].conversation
    assert conversation is not None
    calls = [
        call for item in conversation if item.type == "assistant" for call in item.calls
    ]
    results = {
        item.call_id: item
        for item in conversation
        if isinstance(item, LlmTurnToolResultItem)
    }
    assert [(call.name, call.arguments.get("path")) for call in calls] == [
        ("read", "a.txt"),
        ("read", "b.txt"),
        ("submit_subagent_report", None),
    ]
    outputs = [json.dumps(results[call.call_id].output) for call in calls]
    assert "alpha contents" in outputs[0] and "bravo" not in outputs[0]
    assert "bravo contents" in outputs[1] and "alpha" not in outputs[1]
    assert "TOOL_CALL_NOT_RUN" in outputs[2]
    assert "must be the only call of its turn" in outputs[2]
    with sqlite3.connect(db_path) as connection:
        terminal = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id='child-process' AND event_name='stream_end'"
        ).fetchone()
    assert json.loads(terminal[0]) == {"outcome": "success", "report": "Child report"}


def test_two_changing_calls_of_one_turn_run_one_after_another_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """変更系 2 件は親と同じく宣言順に 1 件ずつ走る。"""

    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    _allow_workspace_commands(db_path)
    running: list[str] = []
    started: list[str] = []

    async def command(
        *, request: ValidatedCommandRequest, **_kwargs: object
    ) -> UnprojectedBrokerToolOutcome:
        script = request.argv[-1]
        assert not running, "a changing call started while another was running"
        running.append(script)
        started.append(script)
        await asyncio.sleep(0.01)
        running.pop()
        return UnprojectedBrokerToolOutcome(
            status="success",
            output={
                "status": "success",
                "exit_code": 0,
                "stdout": script,
                "stderr": "",
            },
        )

    monkeypatch.setattr(broker_module, "run_command_via_sandbox", command)
    client = _Client(
        calls=(
            (
                LlmToolCall(
                    call_id="c1",
                    name="bash",
                    arguments={"command": "echo one", "cwd": "."},
                ),
                LlmToolCall(
                    call_id="c2",
                    name="bash",
                    arguments={"command": "echo two", "cwd": "."},
                ),
            ),
            _REPORT_CALL,
        )
    )
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(started) == 2
    assert "echo one" in started[0] and "echo two" in started[1]
    assert _results(client, 1) == [("bash", "completed"), ("bash", "completed")]


def _patch_call(call_id: str, path: str) -> LlmToolCall:
    return LlmToolCall(
        call_id=call_id,
        name="apply_patch",
        arguments={
            "changes": [
                {
                    "op": "add",
                    "path": path,
                    "new_lines": [path],
                    "trailing_newline": True,
                }
            ]
        },
    )


@pytest.mark.parametrize("position", ("first", "middle"))
@pytest.mark.parametrize("decision", ("approved_once", "denied"))
def test_a_pause_inside_a_turn_answers_every_call_and_runs_none_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    position: str,
    decision: str,
) -> None:
    """ターンの途中で承認待ちになっても、どの呼び出しにも結果か未実行の通知が付く。"""

    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    workspace = _workspace(db_path)
    (workspace / "notes.txt").write_text("read before the edit", "utf-8")
    # Without a standing approval every edit asks, so the first edit pauses.
    gated = _patch_call("gated", "gated.txt")
    later = _patch_call("later", "later.txt")
    turn = (
        (gated, later)
        if position == "first"
        else (
            LlmToolCall(
                call_id="earlier", name="read", arguments={"path": "notes.txt"}
            ),
            gated,
            later,
        )
    )
    client = _Client(calls=(turn,))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)
    assert len(client.prompts) == 1
    with sqlite3.connect(db_path) as connection:
        session = connection.execute(
            "SELECT approval_session_id,tool_request_id,tool_id FROM approval_sessions"
        ).fetchone()
    assert session is not None and session[2] == "apply_patch"

    _decide(db_path, str(session[0]), str(session[1]), decision)
    resumed = _resume_child(monkeypatch, db_path, payload)

    conversation = resumed.tool_uses[0].conversation
    assert conversation is not None
    calls = [
        call for item in conversation if item.type == "assistant" for call in item.calls
    ]
    results = {
        item.call_id: json.dumps(item.output)
        for item in conversation
        if isinstance(item, LlmTurnToolResultItem)
    }
    seen = {
        (call.name, json.dumps(call.arguments, sort_keys=True)): results[call.call_id]
        for call in calls
    }
    # Every call of the turn is answered exactly once in what the model reads.
    assert len(calls) == len(turn) == len(seen)
    for requested in turn:
        answer = seen[(requested.name, json.dumps(requested.arguments, sort_keys=True))]
        if requested is gated:
            assert ('"kind": "approval_denied"' in answer) == (decision == "denied")
            assert "TOOL_CALL_NOT_RUN" not in answer
        elif requested is later:
            assert "TOOL_CALL_NOT_RUN" in answer
            assert "waited for the user's approval" in answer
        else:
            assert "read before the edit" in answer
    assert (workspace / "gated.txt").exists() == (decision == "approved_once")
    assert not (workspace / "later.txt").exists()
    # Nothing runs twice: the read once, the gated edit once if allowed.
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT tool_id,COUNT(*) FROM tool_invocations GROUP BY tool_id "
            "ORDER BY tool_id"
        ).fetchall() == [
            *([("apply_patch", 1)] if decision == "approved_once" else []),
            *([("read", 1)] if position == "middle" else []),
        ]


@pytest.mark.parametrize("report_position", ("first", "last"))
def test_a_report_mixed_with_other_calls_waits_for_a_turn_of_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, report_position: str
) -> None:
    """報告が他の呼び出しと同じターンなら終わらせず、他を実行して報告は出し直させる。"""

    db_path, payload = _running_child(
        tmp_path,
        profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id,
        claim_workspace=True,
    )
    _allow_workspace_commands(db_path)
    launched: list[str] = []

    async def command(
        *, request: ValidatedCommandRequest, **_kwargs: object
    ) -> UnprojectedBrokerToolOutcome:
        launched.append(request.argv[-1])
        return UnprojectedBrokerToolOutcome(
            status="success",
            output={"status": "success", "exit_code": 0, "stdout": "", "stderr": ""},
        )

    monkeypatch.setattr(broker_module, "run_command_via_sandbox", command)
    mixed = (
        (_REPORT_CALL, _BASH_CALL)
        if report_position == "first"
        else (_BASH_CALL, _REPORT_CALL)
    )
    client = _Client(calls=(mixed, _REPORT_CALL))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    # The mixed turn did not end the run; the report alone did, one turn later.
    assert len(client.prompts) == 2
    assert len(launched) == 1 and "pwd" in launched[0]
    conversation = client.tool_uses[1].conversation
    assert conversation is not None
    answers = {
        item.name: json.dumps(item.output)
        for item in conversation
        if isinstance(item, LlmTurnToolResultItem)
    }
    assert set(answers) == {"bash", "submit_subagent_report"}
    assert '"status": "completed"' in answers["bash"]
    assert "TOOL_CALL_NOT_RUN" in answers["submit_subagent_report"]
    assert "must be the only call of its turn" in answers["submit_subagent_report"]
    with sqlite3.connect(db_path) as connection:
        terminal = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id='child-process' AND event_name='stream_end'"
        ).fetchone()
    assert json.loads(terminal[0]) == {"outcome": "success", "report": "Child report"}


def test_two_reports_in_one_turn_end_nothing_until_one_comes_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """報告を 2 件出したターンでは終わらず、単独で出し直した報告で終わる。"""

    db_path, payload = _running_child(
        tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id
    )
    first = LlmToolCall(
        call_id="r1", name="submit_subagent_report", arguments={"report": "Draft"}
    )
    corrected = LlmToolCall(
        call_id="r2", name="submit_subagent_report", arguments={"report": "Corrected"}
    )
    client = _Client(calls=((first, corrected), corrected))
    monkeypatch.setattr(subagent_job, "build_local_llm_proxy_client", lambda: client)
    with bind_local_runtime_db_execution_context(
        db_path=db_path, busy_timeout_ms=1_000
    ):
        subagent_job.run_action_subagent_job(payload)

    assert len(client.prompts) == 2
    answers = [
        json.dumps(item.output)
        for item in client.tool_uses[1].conversation or ()
        if isinstance(item, LlmTurnToolResultItem)
    ]
    assert len(answers) == 2
    assert all(
        "TOOL_CALL_NOT_RUN" in answer and "send exactly one, alone" in answer
        for answer in answers
    )
    with sqlite3.connect(db_path) as connection:
        terminal = connection.execute(
            "SELECT payload_json FROM process_events "
            "WHERE process_id='child-process' AND event_name='stream_end'"
        ).fetchone()
    assert json.loads(terminal[0]) == {"outcome": "success", "report": "Corrected"}
