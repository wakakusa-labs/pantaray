"""Process groups and temp dirs owned by a run rather than by the starting call."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.models import (
    ToolInvocationCompletionInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import complete_execution_session
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    cleanup_action_runtime_resources,
)
from pantaray_agents.local_runtime.tooling.resources.process_identity import (
    read_process_start_signature,
)
from pantaray_agents.local_runtime.tooling.resources.resource_cleanup import (
    process_exists,
)
from pantaray_agents.local_runtime.tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_periodic_reaper,
    reconcile_tool_runtime_resources_for_startup,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    ToolRuntimeResourceSessionConflictError,
    create_tool_runtime_resource,
)
from pantaray_agents.local_runtime.tooling.resources.resource_tracking import (
    register_path_resource,
    register_process_group_resource,
    release_run_owned_resource,
)

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_bash_invocation,
)

BUSY_TIMEOUT_MS = 1_000
ACTION_PROCESS_ID = "action-process"
SUBAGENT_PROCESS_ID = "subagent-process"
TIMESTAMP = "2026-03-23T00:00:01Z"


@pytest.fixture
def spawned_pids() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _spawn_process_group(spawned_pids: list[int]) -> int:
    """Start ``sleep`` in its own group whose leader exits, as the helper does.

    The leader's exit hands ``sleep`` to init, which reaps it once killed, so a
    killed process really disappears instead of lingering as this test's zombie.
    """

    leader = subprocess.run(
        ["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
        start_new_session=True,
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(leader.stdout)
    spawned_pids.append(pid)
    return pid


def _insert_run_processes(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        for process_id, kind, parent_process_id in (
            (ACTION_PROCESS_ID, "action", None),
            (SUBAGENT_PROCESS_ID, "action_subagent", ACTION_PROCESS_ID),
        ):
            connection.execute(
                """
                INSERT INTO processes(
                    process_id, user_id, kind, status, action_id, started_at,
                    updated_at, heartbeat_at, next_event_seq, parent_process_id
                ) VALUES (?, 'user-1', ?, 'running', 'action-1', ?, ?, ?, 1, ?)
                """,
                (process_id, kind, TIMESTAMP, TIMESTAMP, TIMESTAMP, parent_process_id),
            )


def _end_process(db_path: Path, process_id: str) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE processes SET status = 'completed', completed_at = ? "
            "WHERE process_id = ?",
            ("2026-03-23T00:00:09Z", process_id),
        )


def _complete_invocation(db_path: Path, invocation_id: str) -> None:
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        completion=ToolInvocationCompletionInput(
            invocation_id=invocation_id,
            status="completed",
            completed_at="2026-03-23T00:00:02Z",
            output_json={"process_id": "p1", "status": "running"},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )


def _resource_status(db_path: Path, resource_id: str) -> str:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM tool_runtime_resources WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()
    return str(row[0])


def _register_run_owned_process(
    *,
    tmp_path: Path,
    spawned_pids: list[int],
    owner_process_id: str,
) -> tuple[Path, object, int, str, Path, str]:
    """Start a run-owned process and per-process temp dir from a bash call that ends."""

    db_path, context = bootstrap_runtime_db(tmp_path)
    _insert_run_processes(db_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    process_temp_dir = tmp_path / "pantaray-command-temp" / "process-1"
    process_temp_dir.mkdir(parents=True)
    temp_resource_id = register_path_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id=context.execution_session_id,
        tool_invocation_id=invocation_id,
        action_id="action-1",
        resource_kind="temp_dir",
        resource_path=process_temp_dir,
        created_at=TIMESTAMP,
        owner_process_id=owner_process_id,
    )
    pid = _spawn_process_group(spawned_pids)
    process_resource_id = register_process_group_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id=context.execution_session_id,
        tool_invocation_id=invocation_id,
        action_id="action-1",
        pid=pid,
        created_at=TIMESTAMP,
        owner_process_id=owner_process_id,
    )
    assert temp_resource_id is not None and process_resource_id is not None
    _complete_invocation(db_path, invocation_id)
    return (
        db_path,
        context,
        pid,
        process_resource_id,
        process_temp_dir,
        temp_resource_id,
    )


def test_sweep_keeps_a_subagents_process_until_the_subagent_run_ends(
    tmp_path: Path,
    spawned_pids: list[int],
) -> None:
    db_path, context, pid, process_resource_id, process_temp_dir, temp_resource_id = (
        _register_run_owned_process(
            tmp_path=tmp_path,
            spawned_pids=spawned_pids,
            owner_process_id=SUBAGENT_PROCESS_ID,
        )
    )

    assert (
        reconcile_tool_runtime_resources_for_periodic_reaper(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
        )
        == 0
    )
    assert process_exists(pid)
    assert process_temp_dir.exists()

    # The subagent shares the Action's execution session, which keeps running.
    _end_process(db_path, SUBAGENT_PROCESS_ID)
    assert (
        reconcile_tool_runtime_resources_for_periodic_reaper(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
        )
        == 2
    )

    assert not process_exists(pid)
    assert not process_temp_dir.exists()
    assert _resource_status(db_path, process_resource_id) == "cleaned"
    assert _resource_status(db_path, temp_resource_id) == "cleaned"
    assert context.action_temp_dir.exists()


def test_sweep_reaps_run_owned_resources_and_the_session_root_after_the_run_ends(
    tmp_path: Path,
    spawned_pids: list[int],
) -> None:
    db_path, context, pid, process_resource_id, process_temp_dir, temp_resource_id = (
        _register_run_owned_process(
            tmp_path=tmp_path,
            spawned_pids=spawned_pids,
            owner_process_id=ACTION_PROCESS_ID,
        )
    )

    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-03-23T00:00:09Z",
    )
    reconcile_tool_runtime_resources_for_periodic_reaper(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    )

    assert not process_exists(pid)
    assert not process_temp_dir.exists()
    assert _resource_status(db_path, process_resource_id) == "cleaned"
    assert _resource_status(db_path, temp_resource_id) == "cleaned"
    # The per-process temp dir is not mistaken for a second session root.
    assert not context.action_temp_dir.exists()


def test_run_end_cleanup_reaps_run_owned_resources(
    tmp_path: Path,
    spawned_pids: list[int],
) -> None:
    db_path, context, pid, process_resource_id, process_temp_dir, temp_resource_id = (
        _register_run_owned_process(
            tmp_path=tmp_path,
            spawned_pids=spawned_pids,
            owner_process_id=SUBAGENT_PROCESS_ID,
        )
    )

    result = cleanup_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.failure_count == 0
    assert not process_exists(pid)
    assert not process_temp_dir.exists()
    assert _resource_status(db_path, process_resource_id) == "cleaned"
    assert _resource_status(db_path, temp_resource_id) == "cleaned"


def test_startup_recovery_reaps_run_owned_resources_of_a_live_run(
    tmp_path: Path,
    spawned_pids: list[int],
) -> None:
    db_path, _, pid, process_resource_id, process_temp_dir, temp_resource_id = (
        _register_run_owned_process(
            tmp_path=tmp_path,
            spawned_pids=spawned_pids,
            owner_process_id=ACTION_PROCESS_ID,
        )
    )

    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    )

    assert not process_exists(pid)
    assert not process_temp_dir.exists()
    assert _resource_status(db_path, process_resource_id) == "cleaned"
    assert _resource_status(db_path, temp_resource_id) == "cleaned"


def test_release_stops_the_process_and_removes_its_temp_dir(
    tmp_path: Path,
    spawned_pids: list[int],
) -> None:
    db_path, _, pid, process_resource_id, process_temp_dir, temp_resource_id = (
        _register_run_owned_process(
            tmp_path=tmp_path,
            spawned_pids=spawned_pids,
            owner_process_id=ACTION_PROCESS_ID,
        )
    )

    for resource_id in (process_resource_id, temp_resource_id):
        assert (
            release_run_owned_resource(
                db_path=db_path,
                busy_timeout_ms=BUSY_TIMEOUT_MS,
                resource_id=resource_id,
                released_at="2026-03-23T00:00:05Z",
            )
            == "cleaned"
        )

    assert not process_exists(pid)
    assert not process_temp_dir.exists()


def test_release_never_signals_a_reused_pid(
    tmp_path: Path,
    spawned_pids: list[int],
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    _insert_run_processes(db_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    pid = _spawn_process_group(spawned_pids)
    assert read_process_start_signature(pid=pid) != "Mon Jan  1 00:00:00 2024"
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="reused-pid",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="process_group",
            status="active",
            created_at=TIMESTAMP,
            pid=pid,
            pgid=os.getpgid(pid),
            process_start_signature="Mon Jan  1 00:00:00 2024",
            resource_path=None,
            owner_process_id=ACTION_PROCESS_ID,
        ),
    )

    status = release_run_owned_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource_id="reused-pid",
        released_at="2026-03-23T00:00:05Z",
    )

    assert status == "cleaned"
    assert process_exists(pid)


def test_run_owner_requires_the_starting_call(tmp_path: Path) -> None:
    """Without its call, a run-owned temp dir would look like the session root."""

    db_path, context = bootstrap_runtime_db(tmp_path)
    _insert_run_processes(db_path)

    with pytest.raises(ToolRuntimeResourceSessionConflictError):
        register_path_resource(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            execution_session_id=context.execution_session_id,
            tool_invocation_id=None,
            action_id="action-1",
            resource_kind="temp_dir",
            resource_path=tmp_path / "process-temp",
            created_at=TIMESTAMP,
            owner_process_id=ACTION_PROCESS_ID,
        )
