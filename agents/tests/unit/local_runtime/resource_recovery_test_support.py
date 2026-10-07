from __future__ import annotations

from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import ToolInvocationStartInput
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database


def valid_apply_patch_request_json() -> dict[str, object]:
    return {"args": valid_apply_patch_args()}


def valid_apply_patch_args() -> dict[str, object]:
    return {
        "changes": [
            {
                "op": "update",
                "path": "todo.txt",
                "edits": [{"old_lines": ["old line"], "new_lines": ["new line"]}],
            }
        ]
    }


def bootstrap_runtime_db(tmp_path: Path) -> tuple[Path, object]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    return db_path, context


def register_running_bash_invocation(
    *,
    db_path: Path,
    context: object,
    invocation_id: str = "invocation-1",
) -> str:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=f"request-{invocation_id}",
            user_id="user-1",
            action_id="action-1",
            step_id=f"step-{invocation_id}",
            tool_id="bash",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="process_exec_local",
            network_policy="cloud-proxy-only",
            command_summary_json={
                "summary_kind": "bash",
                "command": "run bash",
                "cwd": ".",
                "timeout_ms": 5_000,
            },
            capability_snapshot_json={"required_capabilities": ["process_exec_local"]},
            request_json={"args": {"command": "sleep 30"}},
            status="running",
            started_at="2026-03-23T00:00:01Z",
        ),
    )
    return invocation_id


def register_running_apply_patch_invocation(*, db_path: Path, context: object) -> str:
    invocation_id = "invocation-apply-patch"
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id="request-apply-patch",
            user_id="user-1",
            action_id="action-1",
            step_id="step-apply-patch",
            tool_id="apply_patch",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="surgical_edit",
            network_policy="cloud-proxy-only",
            command_summary_json={"summary_kind": "apply_patch", "target_paths": []},
            capability_snapshot_json={"required_capabilities": ["scoped_write"]},
            request_json=valid_apply_patch_request_json(),
            status="running",
            started_at="2026-03-23T00:00:01Z",
        ),
    )
    return invocation_id
