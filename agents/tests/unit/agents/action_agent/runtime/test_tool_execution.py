from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import create_state_token_sink
from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    FinalizedToolExecutionError,
    ToolCompletionAuditPersistenceError,
    ToolExecutionResult,
    UnprojectedToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    ToolValidationError,
    run_tool,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import (
    APPLY_PATCH_TOOL,
    BASH_TOOL,
    MEMORY_SQL_TOOL,
    READ_TOOL,
    THINKING_TOOL,
    WRITE_SESSION_MEMORY_TOOL,
)
from pantaray_agents.agents.core import CountingSink, LlmUsage, TokenBudgetExceeded
from pantaray_agents.application.action.ports import ActionStepEventPersistenceError
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    UnprojectedBrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    create_capability_grant,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.sandbox.runtime_policy import (
    BrokerLocalBudget,
    RuntimeBudgetResolution,
    SandboxLaunchBudget,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    ToolOutputValidationError,
)
from pantaray_agents.schema.action_conversation import ActionStepEventData
from pantaray_agents.schema.repositories.repository import RepositoryErrorKind
from pantaray_agents.utils.trace_context import TraceContextManager


@pytest.fixture(autouse=True)
def _action_process_trace() -> Iterator[None]:
    with TraceContextManager(extra={"process_id": "action-process-1"}):
        yield


def _insert_test_action(
    *, db_path: Path, user_id: str, action_id: str, created_at: str
) -> None:
    insert_agent_action(
        db_path=db_path,
        user_id=user_id,
        action_id=action_id,
        created_at=created_at,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO processes(process_id,user_id,kind,status,action_id,
               started_at,updated_at,heartbeat_at,next_event_seq)
               VALUES ('action-process-1',?,'action','running',?,?,?,?,1)""",
            (user_id, action_id, created_at, created_at, created_at),
        )


def _base_state() -> dict:
    return create_initial_state(
        user_id="user-123",
        suggestion_id="sug-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )


def _session_memory_args() -> dict:
    return {"content": "# Notes\n\n- the build uses uv\n"}


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.emit_action_step = AsyncMock()
    runtime.services = SimpleNamespace(token_accounting=MagicMock())
    return runtime


def _install_local_runtime_context(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    allowed_tool_ids: tuple[str, ...],
) -> dict:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=allowed_tool_ids,
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    state = _base_state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy
    state["action_temp_dir"] = str(context.action_temp_dir)
    state["app_runtime_python"] = str(context.app_runtime_python)
    return state


@pytest.mark.asyncio
async def test_processing_event_failure_survives_invocation_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    event_error = ActionStepEventPersistenceError(
        event=ActionStepEventData(
            action_id="act-123",
            process_id="process-1",
            step_kind="tool",
            step_id="step-1",
            step_number=1,
            tool_id=WRITE_SESSION_MEMORY_TOOL.tool_id,
            label=WRITE_SESSION_MEMORY_TOOL.name,
            status="processing",
            started_at="2025-01-01T00:00:00Z",
            completed_at=None,
        ),
        error_kind=RepositoryErrorKind.TRANSIENT,
        retryable=True,
    )
    runtime.emit_action_step.side_effect = event_error
    cleanup_error = ToolCompletionAuditPersistenceError(
        tool_id=WRITE_SESSION_MEMORY_TOOL.tool_id,
        tool_invocation_id="invocation-1",
    )
    finalize_invocation = MagicMock(side_effect=cleanup_error)
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "execution.finalize_action_tool_invocation",
        finalize_invocation,
    )

    with pytest.raises(ActionStepEventPersistenceError) as caught:
        await run_tool(
            MagicMock(),
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=runtime,
            step_number=1,
            actor="supervisor",
            step_id="step-1",
        )

    assert caught.value is event_error
    finalize_invocation.assert_called_once()


@pytest.mark.asyncio
async def test_run_tool_classifies_completion_audit_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    agent = MagicMock()
    runtime = _runtime()
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )

    def fail_completion_audit(**_kwargs) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "finalization_boundary.finalize_local_tool_result",
        fail_completion_audit,
    )

    with pytest.raises(ToolCompletionAuditPersistenceError) as caught:
        await run_tool(
            agent,
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=runtime,
            step_number=0,
            actor="supervisor",
        )

    assert caught.value.tool_id == WRITE_SESSION_MEMORY_TOOL.tool_id
    assert caught.value.tool_invocation_id
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_run_tool_does_not_recomplete_broker_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = ToolCompletionAuditPersistenceError(
        tool_id=READ_TOOL.tool_id,
        tool_invocation_id="invocation-1",
    )

    async def fail_broker_completion(*_args: object, **_kwargs: object) -> None:
        raise expected

    finalize_invocation = MagicMock()
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "execution.run_broker_tool_wrapper",
        fail_broker_completion,
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "execution.finalize_action_tool_invocation",
        finalize_invocation,
    )
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(READ_TOOL.tool_id,),
    )

    with pytest.raises(ToolCompletionAuditPersistenceError) as caught:
        await run_tool(
            MagicMock(),
            READ_TOOL,
            args={"path": "sample.txt"},
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=0,
            actor="supervisor",
        )

    assert caught.value is expected
    finalize_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_thinking_budget_exceeded_finalizes_invocation_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["token_budget"] = 1
    state["tokens_used"] = 0
    state["total_prompt_tokens"] = 0
    state["total_completion_tokens"] = 0
    sink = create_state_token_sink(state)
    agent = MagicMock()
    runner = MagicMock()

    async def exceed_budget(*, sink, **_kwargs):
        sink.record(
            LlmUsage(prompt_tokens=2, completion_tokens=0),
            stage="tool::thinking",
            may_raise=True,
        )
        raise AssertionError("token accounting must raise")

    runner.generate_text = AsyncMock(side_effect=exceed_budget)
    agent.create_tool_llm_runner.return_value = runner
    runtime = _runtime()

    with pytest.raises(TokenBudgetExceeded):
        await run_tool(
            agent,
            THINKING_TOOL,
            args={"query": "inspect the image"},
            state=state,  # type: ignore[arg-type]
            sink=sink,
            runtime=runtime,
            step_number=1,
            actor="supervisor",
        )

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        invocation_row = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert invocation_row == ("failed",)
    assert output_row is not None
    assert json.loads(str(output_row[0]))["error"]["error_type"] == (
        "TokenBudgetExceeded"
    )


@pytest.mark.asyncio
async def test_run_tool_finalizes_invocation_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancelled tool call must leave its audit row terminal."""

    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    sink = create_state_token_sink(state)
    agent = MagicMock()
    runner = MagicMock()
    running = asyncio.Event()

    async def block_until_cancelled(**_kwargs: object) -> str:
        running.set()
        await asyncio.Event().wait()
        raise AssertionError("tool must not complete")

    runner.generate_text = AsyncMock(side_effect=block_until_cancelled)
    agent.create_tool_llm_runner.return_value = runner

    task = asyncio.ensure_future(
        run_tool(
            agent,
            THINKING_TOOL,
            args={"query": "inspect the state"},
            state=state,  # type: ignore[arg-type]
            sink=sink,
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )
    )
    await running.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        invocation_row = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert invocation_row == ("canceled",)
    assert output_row is not None
    assert json.loads(str(output_row[0]))["error"]["error_type"] == "CancelledError"


@pytest.mark.asyncio
async def test_run_tool_records_validation_reject_to_local_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    agent = MagicMock()
    runtime = MagicMock()
    state = _base_state()
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy

    with pytest.raises(ToolValidationError):
        await run_tool(
            agent,
            WRITE_SESSION_MEMORY_TOOL,
            args={"use_goal_workers": "yes"},
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=runtime,
            step_number=0,
            actor="supervisor",
        )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT intent_class, network_policy, status
            FROM tool_invocations
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert invocation_row == ("surgical_edit", "cloud-proxy-only", "failed")
    assert output_row is not None


@pytest.mark.asyncio
async def test_validation_reject_classifies_terminal_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )

    def fail_finalization(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("validation terminal write failed")

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "finalization_boundary.finalize_local_tool_result",
        fail_finalization,
    )

    with pytest.raises(ToolCompletionAuditPersistenceError) as caught:
        await run_tool(
            MagicMock(),
            WRITE_SESSION_MEMORY_TOOL,
            args={"use_goal_workers": "yes"},
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )

    assert caught.value.tool_id == WRITE_SESSION_MEMORY_TOOL.tool_id
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_implementation_failure_classifies_terminal_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution."
        "run_validated_tool_impl",
        AsyncMock(side_effect=RuntimeError("implementation failed")),
    )

    def fail_finalization(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("failure terminal write failed")

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "finalization_boundary.finalize_local_tool_result",
        fail_finalization,
    )

    with pytest.raises(ToolCompletionAuditPersistenceError) as caught:
        await run_tool(
            MagicMock(),
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )

    assert caught.value.tool_id == WRITE_SESSION_MEMORY_TOOL.tool_id
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_output_validation_failure_classifies_terminal_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution."
        "run_validated_tool_impl",
        AsyncMock(
            return_value=UnprojectedToolExecutionResult(
                step_id="invalid-output-step",
                tool_id=WRITE_SESSION_MEMORY_TOOL.tool_id,
                status="success",
                started_at="2026-08-12T00:00:00+00:00",
                completed_at="2026-08-12T00:00:01+00:00",
                output={"invalid": "plan output"},
            )
        ),
    )

    def fail_completion_record(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("validation failure write failed")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.tool_result_finalization."
        "record_tool_invocation_completion",
        fail_completion_record,
    )

    with pytest.raises(ToolCompletionAuditPersistenceError) as caught:
        await run_tool(
            MagicMock(),
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )

    assert caught.value.tool_id == WRITE_SESSION_MEMORY_TOOL.tool_id
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_run_tool_does_not_treat_unexpected_validation_setup_error_as_user_validation_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    agent = MagicMock()
    runtime = MagicMock()
    state = _base_state()
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy

    def _raise_runtime_error(*_args, **_kwargs) -> None:
        raise RuntimeError("schema setup failed")

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.validate_tool_args",
        _raise_runtime_error,
    )

    with pytest.raises(RuntimeError, match="schema setup failed"):
        await run_tool(
            agent,
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=runtime,
            step_number=0,
            actor="supervisor",
        )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT 1 FROM tool_invocations ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    assert invocation_row is None


@pytest.mark.asyncio
async def test_run_tool_fails_closed_when_local_execution_context_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = MagicMock()
    runtime = MagicMock()
    state = _base_state()

    with pytest.raises(RuntimeError):
        await run_tool(
            agent,
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=runtime,
            step_number=0,
            actor="supervisor",
        )


def test_runtime_checkpoint_preserves_execution_session_identity() -> None:
    state = _base_state()
    state["manifest_id"] = "manifest-act-123"
    state["execution_session_id"] = "exec-123"
    state["execution_network_policy"] = "cloud-proxy-only"
    state["action_temp_dir"] = "/tmp/act-123"
    state["app_runtime_python"] = "/usr/bin/python3"
    state["read_access_scope"] = "workspace"

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="act-123",
        expected_suggestion_id="sug-123",
        expected_user_id="user-123",
    )

    assert restored["manifest_id"] == "manifest-act-123"
    assert restored["execution_session_id"] == "exec-123"
    assert restored["execution_network_policy"] == "cloud-proxy-only"


@pytest.mark.asyncio
async def test_run_tool_records_broker_output_and_file_reference(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=("read",),
    )
    target_file = context.workspace_path / "notes.txt"
    target_file.write_text("line-1\nline-2\n", encoding="utf-8")
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    agent = MagicMock()
    runtime = _runtime()
    state = _base_state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy

    result = await run_tool(
        agent,
        READ_TOOL,
        args={"path": "notes.txt"},
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=runtime,
        step_number=1,
        actor="supervisor",
    )

    assert runtime.emit_action_step.call_args.args[0].tool_args == {
        "args": {"path": "notes.txt"}
    }
    assert result.status == "success"
    assert result.finalized_output.output["content"] == "line-1\nline-2\n"
    with sqlite3.connect(db_path) as connection:
        output_row = connection.execute(
            """
            SELECT search_text
            FROM tool_outputs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        file_ref_row = connection.execute(
            """
            SELECT local_path
            FROM file_references
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert output_row == ("line-1\nline-2\n",)
    assert file_ref_row == (str(target_file.resolve()),)


@pytest.mark.asyncio
async def test_run_tool_preserves_large_read_page_in_history_and_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=("read",),
    )
    content = "large tool output\n" * 1_500
    (context.workspace_path / "large.txt").write_text(content, encoding="utf-8")
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    state = _base_state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy
    result = await run_tool(
        MagicMock(),
        READ_TOOL,
        args={"path": "large.txt"},
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=MagicMock(),
        step_number=1,
        actor="goal_worker",
    )

    assert isinstance(result.finalized_output.output, dict)
    output = result.finalized_output.output
    assert result.finalized_output.storage_kind == "inline_json"
    assert output["content"]
    assert content.startswith(output["content"])
    assert output["truncated"] is True
    assert output["next_offset"] is not None
    with sqlite3.connect(db_path) as connection:
        output_row = connection.execute(
            """
            SELECT output_json, search_text, stdout_text, stderr_text
            FROM tool_outputs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        read_memory_count = connection.execute(
            "SELECT COUNT(*) FROM memory_nodes WHERE source_type = 'action_file_read'"
        ).fetchone()
    assert output_row is not None
    assert json.loads(str(output_row[0])) == result.finalized_output.output
    assert output_row[1:] == (output["content"], None, None)
    assert read_memory_count == (1,)


@pytest.mark.asyncio
async def test_run_tool_spills_large_non_broker_output_before_returning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(MEMORY_SQL_TOOL.tool_id,),
    )
    raw_output = {
        "columns": ["c"],
        "rows": [{"c": "x" * 21_000}],
        "row_count": 1,
        "truncated": False,
        "notes": [],
    }
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_validated_tool_impl",
        AsyncMock(
            return_value=UnprojectedToolExecutionResult(
                step_id="step-1",
                tool_id=MEMORY_SQL_TOOL.tool_id,
                status="success",
                started_at="2025-01-01T00:00:00Z",
                completed_at="2025-01-01T00:00:01Z",
                output=raw_output,
            )
        ),
    )

    result = await run_tool(
        MagicMock(),
        MEMORY_SQL_TOOL,
        args={"sql": "SELECT * FROM agent_actions"},
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=_runtime(),
        step_number=1,
        actor="supervisor",
    )

    assert isinstance(result.finalized_output.output, dict)
    assert result.finalized_output.output["storage"] == "action_file"
    stored_path = Path(str(result.finalized_output.output["path"]))
    assert json.loads(stored_path.read_text(encoding="utf-8")) == raw_output


@pytest.mark.asyncio
async def test_run_tool_surfaces_output_schema_mismatch_after_failed_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_validated_tool_impl",
        AsyncMock(
            return_value=UnprojectedToolExecutionResult(
                step_id="step-invalid-output",
                tool_id=WRITE_SESSION_MEMORY_TOOL.tool_id,
                status="success",
                started_at="2025-01-01T00:00:00Z",
                completed_at="2025-01-01T00:00:01Z",
                output={"content": "not the tool output"},
            )
        ),
    )

    with pytest.raises(FinalizedToolExecutionError) as raised:
        await run_tool(
            MagicMock(),
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )

    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT invocation_id, status
            FROM tool_invocations
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json, output_storage_kind
            FROM tool_outputs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert invocation_row is not None
    assert invocation_row[1] == "failed"
    assert raised.value.tool_invocation_id == invocation_row[0]
    assert isinstance(raised.value.cause, ToolOutputValidationError)
    assert "does not match" in str(raised.value.cause)
    assert output_row is not None
    assert output_row[1] == "inline_json"
    assert json.loads(str(output_row[0]))["error"]["error_type"] == (
        "ToolOutputValidationError"
    )


@pytest.mark.asyncio
async def test_run_tool_preserves_impl_validation_error_after_failed_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    validation_error = ToolValidationError(
        "selected requirement belongs to another goal",
        details={"requirement_id": "R2"},
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_validated_tool_impl",
        AsyncMock(side_effect=validation_error),
    )

    with pytest.raises(ToolValidationError) as raised:
        await run_tool(
            MagicMock(),
            WRITE_SESSION_MEMORY_TOOL,
            args=_session_memory_args(),
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )

    assert raised.value is validation_error
    assert validation_error.tool_invocation_id is not None
    assert validation_error.finalized_output is not None
    assert validation_error.finalized_output.output["error"]["error_type"] == (
        "ToolValidationError"
    )

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        invocation_row = connection.execute(
            """
            SELECT invocation_id, status
            FROM tool_invocations
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert invocation_row == (validation_error.tool_invocation_id, "failed")


@pytest.mark.asyncio
async def test_run_tool_normalizes_broker_output_validation_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(READ_TOOL.tool_id,),
    )
    output_error = ToolOutputValidationError("broker output does not match schema")
    output_error.tool_invocation_id = "broker-invocation"
    output_error.finalized_output = FinalizedToolOutput(
        output={"status": "error"},
        storage_kind="inline_json",
        owner_kind="tool_invocation",
        search_text=None,
        stdout_text=None,
        stderr_text=None,
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_broker_tool_wrapper",
        AsyncMock(side_effect=output_error),
    )

    with pytest.raises(FinalizedToolExecutionError) as raised:
        await run_tool(
            MagicMock(),
            READ_TOOL,
            args={"path": "README.md"},
            state=state,  # type: ignore[arg-type]
            sink=CountingSink(),
            runtime=_runtime(),
            step_number=1,
            actor="supervisor",
        )

    assert raised.value.cause is output_error
    assert raised.value.tool_invocation_id == "broker-invocation"
    assert raised.value.finalized_output is output_error.finalized_output


@pytest.mark.asyncio
async def test_run_tool_projects_non_broker_binary_output_before_returning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(WRITE_SESSION_MEMORY_TOOL.tool_id,),
    )
    payload = b"\x00local tool binary\xff"
    raw_result = UnprojectedToolExecutionResult(
        step_id="step-binary",
        tool_id=WRITE_SESSION_MEMORY_TOOL.tool_id,
        status="success",
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:01Z",
        output=payload,
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_validated_tool_impl",
        AsyncMock(return_value=raw_result),
    )

    result = await run_tool(
        MagicMock(),
        WRITE_SESSION_MEMORY_TOOL,
        args=_session_memory_args(),
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=_runtime(),
        step_number=1,
        actor="supervisor",
    )

    assert isinstance(result, ToolExecutionResult)
    assert isinstance(result.finalized_output.output, dict)
    assert result.finalized_output.output["media_type"] == "application/octet-stream"
    assert Path(str(result.finalized_output.output["path"])).read_bytes() == payload
    assert raw_result.output == payload


@pytest.mark.asyncio
async def test_run_tool_projects_broker_binary_output_before_returning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(READ_TOOL.tool_id,),
    )
    payload = b"\x00broker tool binary\xff"
    raw_outcome = UnprojectedBrokerToolOutcome(
        status="success",
        output=payload,
    )
    # No read returns bytes, so the outcome is substituted after the read runs.
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker.run_read",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker.read_tool_outcome",
        lambda _result: raw_outcome,
    )

    result = await run_tool(
        MagicMock(),
        READ_TOOL,
        args={"path": "raw.bin"},
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=MagicMock(),
        step_number=1,
        actor="goal_worker",
    )

    assert isinstance(result, ToolExecutionResult)
    assert isinstance(result.finalized_output.output, dict)
    assert result.finalized_output.output["media_type"] == "application/octet-stream"
    assert Path(str(result.finalized_output.output["path"])).read_bytes() == payload
    assert raw_outcome.output == payload


@pytest.mark.asyncio
async def test_run_tool_does_not_execute_broker_without_invocation_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_runtime_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        allowed_tool_ids=(READ_TOOL.tool_id,),
    )
    executor = MagicMock()
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker.claim_broker_execution_start",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker.run_read",
        executor,
    )

    result = await run_tool(
        MagicMock(),
        READ_TOOL,
        args={"path": "raw.bin"},
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=MagicMock(),
        step_number=1,
        actor="goal_worker",
    )

    assert result.status == "error"
    assert isinstance(result.finalized_output.output, dict)
    error = result.finalized_output.output["error"]
    assert isinstance(error, dict)
    assert "invocation audit" in str(error["message"])
    executor.assert_not_called()


@pytest.mark.asyncio
async def test_run_tool_returns_processing_with_pending_approval_without_invocation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-prompt",
            user_id="user-123",
            scope_type="global",
            scope_ref=None,
            approval_mode="prompt_each_time",
            applies_to=("workspace_edit_and_command",),
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        ),
    )
    target_file = context.workspace_path / "notes.txt"
    target_file.write_text("line-1\n", encoding="utf-8")
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    agent = MagicMock()
    runtime = MagicMock()
    state = _base_state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy

    result = await run_tool(
        agent,
        APPLY_PATCH_TOOL,
        args={
            "changes": [
                {
                    "op": "update",
                    "path": "notes.txt",
                    "edits": [{"old_lines": ["line-1"], "new_lines": ["line-2"]}],
                }
            ]
        },
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=runtime,
        step_number=1,
        actor="goal_worker",
    )

    assert result.status == "processing"
    assert result.finalized_output.output["kind"] == "approval_required"
    assert result.finalized_output.output["approval_status"] == "pending"
    with sqlite3.connect(db_path) as connection:
        invocation_count = connection.execute(
            "SELECT COUNT(*) FROM tool_invocations"
        ).fetchone()
        approval_row = connection.execute(
            """
            SELECT status, tool_request_id
            FROM approval_sessions
            ORDER BY requested_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert invocation_count == (0,)
    assert approval_row is not None
    assert approval_row[0] == "pending"
    assert approval_row[1].startswith("act-123:")
    assert approval_row[1].endswith(":1")


@pytest.mark.asyncio
async def test_run_tool_records_actual_bash_cwd_and_runtime_timeout_in_local_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_test_action(
        db_path=db_path,
        user_id="user-123",
        action_id="act-123",
        created_at="2025-01-01T00:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        allowed_tool_ids=("bash",),
    )
    executable_path = context.workspace_path / "scripts" / "pwd"
    executable_path.parent.mkdir(parents=True, exist_ok=True)
    executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable_path.chmod(
        executable_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-123",
            user_id="user-123",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("workspace_edit_and_command",),
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=1_000,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-123",
            user_id="user-123",
            preference_id="pref-123",
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at="2025-01-01T00:00:01Z",
        ),
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker.run_command_via_sandbox",
        AsyncMock(
            return_value=UnprojectedBrokerToolOutcome(
                status="success",
                output={
                    "status": "success",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            )
        ),
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker_command_validation.resolve_runtime_budget",
        lambda **_kwargs: RuntimeBudgetResolution(
            broker_local=BrokerLocalBudget(
                child_count_limit=8,
                open_file_lease_limit=8,
            ),
            sandbox_launch=SandboxLaunchBudget(
                timeout_ms=12_345,
                stdout_max_bytes=1_048_576,
                stderr_max_bytes=1_048_576,
                temp_storage_limit_bytes=67_108_864,
            ),
        ),
    )

    agent = MagicMock()
    runtime = MagicMock()
    state = _base_state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id
    state["execution_network_policy"] = context.network_policy

    result = await run_tool(
        agent,
        BASH_TOOL,
        args={"command": "pwd", "cwd": "scripts"},
        state=state,  # type: ignore[arg-type]
        sink=CountingSink(),
        runtime=runtime,
        step_number=1,
        actor="goal_worker",
    )
    assert result.status == "success"

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT cwd, timeout_ms
            FROM tool_invocations
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert invocation_row == (str(executable_path.parent), 12_345)
