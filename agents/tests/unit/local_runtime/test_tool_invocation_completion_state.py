from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.tooling.models import (
    ToolInvocationCompletionInput,
    ToolInvocationTerminalStatus,
)
from pantaray_agents.local_runtime.tooling.repository import (
    ToolInvocationSessionConflictError,
    ToolInvocationTerminalStateError,
    complete_execution_session,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_startup,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    ToolRuntimeResourceReconciliationError,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    ActionStepToolResultOwner,
    ApprovalCommandSummaryToolResultOwner,
    FinalizedToolOutput,
    InvocationToolResultOwner,
    ToolResultCompletionScope,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    ToolOutputValidationError,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_apply_patch_invocation,
    register_running_bash_invocation,
)


def _finalize_invocation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    invocation_id: str,
    completed_at: str,
    output: object,
    search_text: str | None,
    stdout_text: str | None,
    stderr_text: str | None,
    status: ToolInvocationTerminalStatus,
    completion_scope: ToolResultCompletionScope = "invocation",
    file_reference_paths: tuple[str, ...] = (),
) -> FinalizedToolOutput:
    return finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=ToolResultFinalizationRequest(
            owner=InvocationToolResultOwner(
                invocation_id=invocation_id,
                completed_at=completed_at,
                status=status,
                completion_scope=completion_scope,
            ),
            output=output,
            search_text=search_text,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            file_reference_paths=file_reference_paths,
        ),
    )


def test_late_completion_reports_terminal_state_conflict_without_overwriting_output(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    with pytest.raises(ToolInvocationTerminalStateError) as raised:
        record_tool_invocation_completion(
            db_path=db_path,
            busy_timeout_ms=1_000,
            completion=ToolInvocationCompletionInput(
                invocation_id=invocation_id,
                status="completed",
                completed_at="2026-03-23T00:00:03Z",
                output_json={"text": "late result"},
                output_storage_kind="inline_json",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
                redaction_applied=False,
            ),
        )

    assert raised.value.current_status == "failed"
    assert raised.value.requested_status == "completed"
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        output_rows = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchall()

    assert invocation_row == ("failed",)
    assert len(output_rows) == 1
    assert "StartupRecoveryInterruptedToolInvocation" in str(output_rows[0][0])


def test_concurrent_completion_has_one_writer_and_one_terminal_conflict(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    barrier = threading.Barrier(2)

    def _complete() -> str:
        barrier.wait()
        try:
            record_tool_invocation_completion(
                db_path=db_path,
                busy_timeout_ms=5_000,
                completion=ToolInvocationCompletionInput(
                    invocation_id=invocation_id,
                    status="completed",
                    completed_at="2026-03-23T00:00:03Z",
                    output_json={"text": "result"},
                    output_storage_kind="inline_json",
                    search_text=None,
                    stdout_text=None,
                    stderr_text=None,
                    redaction_applied=False,
                ),
            )
        except ToolInvocationTerminalStateError:
            return "terminal_conflict"
        return "completed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: _complete(), range(2)))

    assert sorted(outcomes) == ["completed", "terminal_conflict"]
    with sqlite3.connect(db_path) as connection:
        output_count = connection.execute(
            "SELECT COUNT(*) FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert output_count == (1,)


def test_terminal_session_and_invocation_start_serialize_without_partial_row(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    barrier = threading.Barrier(2)

    def _start() -> str:
        barrier.wait()
        try:
            register_running_bash_invocation(
                db_path=db_path,
                context=context,
                invocation_id="invocation-session-race",
            )
        except ToolInvocationSessionConflictError:
            return "terminal_won"
        return "producer_won"

    def _complete_session() -> None:
        barrier.wait()
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=5_000,
            execution_session_id=context.execution_session_id,
            status="completed",
            completed_at="2026-03-23T00:00:03Z",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        start_future = executor.submit(_start)
        completion_future = executor.submit(_complete_session)
        start_outcome = start_future.result()
        completion_future.result()

    with sqlite3.connect(db_path) as connection:
        session_row = connection.execute(
            "SELECT status FROM execution_sessions WHERE execution_session_id = ?",
            (context.execution_session_id,),
        ).fetchone()
        invocation_rows = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            ("invocation-session-race",),
        ).fetchall()
    assert session_row == ("completed",)
    if start_outcome == "producer_won":
        assert invocation_rows == [("running",)]
    else:
        assert invocation_rows == []


def test_terminal_completion_conflict_removes_uncommitted_spill(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    with pytest.raises(ToolInvocationTerminalStateError):
        _finalize_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            completed_at="2026-03-23T00:00:03Z",
            output={"stdout": "x" * 21_000},
            search_text=None,
            stdout_text="x" * 21_000,
            stderr_text=None,
            status="completed",
        )

    assert list(context.tool_results_path.rglob("output-*.json")) == []


def test_schema_mismatch_persists_failed_invocation_without_running_leak(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )

    with pytest.raises(ToolOutputValidationError, match="does not match"):
        _finalize_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            completed_at="2026-03-23T00:00:03Z",
            output={"status": "success"},
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            status="completed",
        )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json, output_storage_kind
            FROM tool_outputs
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()

    assert invocation_row == ("failed",)
    assert output_row is not None
    assert output_row[1] == "inline_json"
    assert json.loads(str(output_row[0]))["error"]["error_type"] == (
        "ToolOutputValidationError"
    )


def test_failed_status_still_rejects_invalid_output_representation(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )

    with pytest.raises(ToolOutputValidationError, match="finite JSON or bytes"):
        _finalize_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            completed_at="2026-03-23T00:00:03Z",
            output={"unsupported"},
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            status="failed",
        )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        output_row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()

    assert invocation_row == ("failed",)
    assert output_row is not None
    assert "ToolOutputValidationError" in str(output_row[0])


def test_nonterminal_completion_status_is_rejected_before_projection(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )

    with pytest.raises(ValueError, match="status is not terminal"):
        _finalize_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            completed_at="2026-03-23T00:00:03Z",
            output={"stdout": "x" * 21_000},
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            status=cast(ToolInvocationTerminalStatus, "running"),
        )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        output_row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert invocation_row == ("running",)
    assert output_row is None
    assert list(context.tool_results_path.rglob("output-*.json")) == []


def test_json_null_completion_is_persisted_as_inline_json(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE tool_definitions SET output_schema_json = ? WHERE tool_id = 'bash'",
            ('{"type":"null"}',),
        )

    result = _finalize_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:03Z",
        output=None,
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        status="completed",
    )

    with sqlite3.connect(db_path) as connection:
        output_row = connection.execute(
            """
            SELECT output_json, output_storage_kind
            FROM tool_outputs
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
    assert result.output is None
    assert result.storage_kind == "inline_json"
    assert output_row == ("null", "inline_json")


def test_file_reference_failure_rolls_back_terminal_output_and_skips_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_apply_patch_invocation(
        db_path=db_path,
        context=context,
    )
    calls: list[str] = []

    def _record_finalize(**_kwargs: object) -> None:
        calls.append("resource_finalize")

    def _fail_file_reference(**_kwargs: object) -> None:
        calls.append("file_reference")
        raise RuntimeError("file reference failed")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.tool_result_finalization."
        "finalize_tool_runtime_resources_for_invocation",
        _record_finalize,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.repository.executions."
        "_record_tool_invocation_file_references_in_connection",
        _fail_file_reference,
    )

    with pytest.raises(RuntimeError, match="file reference failed"):
        _finalize_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            completed_at="2026-03-23T00:00:03Z",
            output={
                "status": "success",
                "applied_paths": ["todo.txt"],
                "diff": "",
            },
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            status="completed",
            completion_scope="execution",
            file_reference_paths=("todo.txt",),
        )

    assert calls == ["file_reference"]
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        output_count = connection.execute(
            "SELECT COUNT(*) FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert invocation_row == ("running",)
    assert output_count == (0,)


def test_post_commit_resource_failure_does_not_reclassify_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )

    def _fail_resource_reconciliation(**_kwargs: object) -> None:
        raise ToolRuntimeResourceReconciliationError("resource db unavailable")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.tool_result_finalization."
        "finalize_tool_runtime_resources_for_invocation",
        _fail_resource_reconciliation,
    )

    result = _finalize_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:03Z",
        output={
            "status": "success",
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
        },
        search_text=None,
        stdout_text="done",
        stderr_text="",
        status="completed",
        completion_scope="execution",
    )

    assert result.storage_kind == "inline_json"
    assert "startup recovery will retry" in caplog.text
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        output_count = connection.execute(
            "SELECT COUNT(*) FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert invocation_row == ("completed",)
    assert output_count == (1,)


def test_post_commit_descriptor_release_failure_does_not_reclassify_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    from pantaray_agents.local_runtime.tooling import tool_result_finalization

    release = tool_result_finalization.release_stored_tool_result

    def _close_then_report_error(stored) -> OSError:
        assert release(stored) is None
        return OSError("descriptor close failed")

    monkeypatch.setattr(
        tool_result_finalization,
        "release_stored_tool_result",
        _close_then_report_error,
    )

    result = _finalize_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:03Z",
        output={
            "status": "success",
            "stdout": "x" * 21_000,
            "stderr": "",
            "exit_code": 0,
        },
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        status="completed",
    )

    assert result.storage_kind == "action_file"
    assert "descriptor release failed after durable write" in caplog.text
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert row == ("completed",)


def test_action_artifact_owner_types_use_their_canonical_directory(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    common = {
        "manifest_id": context.manifest_id,
        "action_id": "action-1",
        "user_id": "user-1",
    }

    action_step = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ToolResultFinalizationRequest(
            owner=ActionStepToolResultOwner(step_id="step-1", **common),
            output={"content": "s" * 21_000},
        ),
    )
    approval = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ToolResultFinalizationRequest(
            owner=ApprovalCommandSummaryToolResultOwner(
                tool_request_id="request-1",
                **common,
            ),
            output={"command": "p" * 21_000},
        ),
    )

    assert isinstance(action_step.output, dict)
    assert isinstance(approval.output, dict)
    assert Path(str(action_step.output["path"])).parent.name == "step-1"
    assert Path(str(approval.output["path"])).parent.name == "request-1"


def test_binary_completion_stores_metadata_only_with_exact_payload(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    payload = b"\x00\x01binary\xffpayload"

    result = _finalize_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:03Z",
        output=payload,
        search_text="must not be duplicated",
        stdout_text="must not be duplicated",
        stderr_text="must not be duplicated",
        status="completed",
    )

    assert isinstance(result.output, dict)
    stored_path = Path(str(result.output["path"]))
    assert stored_path.read_bytes() == payload
    with sqlite3.connect(db_path) as connection:
        output_row = connection.execute(
            """
            SELECT output_json, search_text, stdout_text, stderr_text
            FROM tool_outputs
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
    assert output_row is not None
    assert json.loads(str(output_row[0])) == result.output
    assert output_row[1:] == (None, None, None)


def test_binary_completion_conflict_removes_uncommitted_spill(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    with pytest.raises(ToolInvocationTerminalStateError):
        _finalize_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            completed_at="2026-03-23T00:00:03Z",
            output=b"\x00uncommitted binary",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            status="completed",
        )

    assert list(context.tool_results_path.rglob("output-*.bin")) == []


def test_large_error_completion_is_stored_as_action_file_metadata(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )

    error_output: dict[str, JSONValue] = build_runtime_tool_error_output(
        error_type="RuntimeError",
        message="x" * 21_000,
    )
    result = _finalize_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:03Z",
        output=error_output,
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        status="failed",
    )

    assert isinstance(result.output, dict)
    assert result.output["storage"] == "action_file"
    stored_path = Path(str(result.output["path"]))
    assert "x" * 21_000 in stored_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        output_row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert output_row is not None
    assert json.loads(str(output_row[0])) == result.output
