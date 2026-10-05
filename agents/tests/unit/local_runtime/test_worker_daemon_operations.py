from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    load_node_by_source,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    enqueue_memory_repair,
)
from pantaray_agents.local_runtime.runtime.bootstrap import (
    prepare_local_runtime_bootstrap_config,
)
from pantaray_agents.local_runtime.runtime.memory_repair_scheduler import (
    run_memory_catalog_repair_once,
)
from pantaray_agents.local_runtime.runtime.process_lock import (
    acquire_runtime_process_lock,
    release_runtime_process_lock,
    runtime_process_lock_path,
)
from pantaray_agents.local_runtime.runtime.runtime_lock_coordinator import (
    attach_runtime_lock_lease,
    release_runtime_lock_lease,
)
from pantaray_agents.local_runtime.runtime.worker_daemon import (
    stop_local_action_worker_daemon,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .migrated_db import prepare_test_database
from .worker_daemon_test_support import (
    set_minimum_local_runtime_env,
    start_worker,
)


@pytest.fixture(autouse=True)
def _reset_worker_daemon() -> Generator[None]:
    stop_local_action_worker_daemon()
    yield
    stop_local_action_worker_daemon()


def test_run_memory_catalog_repair_once_completes_due_job(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-1', 'ja', '2026-07-18T00:00:00Z',
                        '2026-07-18T00:00:00Z')
                """
            )
            revision = register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="periodic-repair-log",
                content="intact memory",
            )
            node = load_node_by_source(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="periodic-repair-log",
            )
            assert node is not None
            enqueue_memory_repair(
                connection=connection,
                user_id="user-1",
                node_id=node.node_id,
                detected_revision_id=revision.revision_id,
                reason="reference_resolution",
            )

    with caplog.at_level(
        logging.INFO,
        logger="pantaray_agents.local_runtime.runtime.memory_repair_scheduler",
    ):
        run_memory_catalog_repair_once(
            db_path=db_path,
            busy_timeout_ms=1_000,
            artifact_root=artifact_root,
        )

    with sqlite3.connect(db_path) as verification:
        repair = verification.execute(
            """
            SELECT state, attempt_count FROM memory_repair_queue
            WHERE user_id = 'user-1'
            """
        ).fetchone()
    assert repair == ("completed", 0)
    assert "memory_repairs_completed=1" in caplog.text


def test_start_worker_fails_closed_when_runtime_lock_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    alias_db_path = tmp_path / "alias.db"
    alias_db_path.symlink_to(db_path)
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=alias_db_path,
        tmp_path=tmp_path,
    )
    assert prepare_local_runtime_bootstrap_config().db_path == db_path
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)

    try:
        with pytest.raises(MigrationError, match="LOCAL_RUNTIME_ALREADY_ACTIVE"):
            start_worker()
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)


def test_start_worker_ignores_lock_file_left_by_a_killed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    # The killed runtime's pid has since been reused by an unrelated live process.
    reused_pid = os.getppid()
    lock_path = runtime_process_lock_path(db_path=db_path)
    lock_path.write_text(
        json.dumps({"owner_pid": reused_pid, "lock_id": "killed-runtime-lock"}),
        encoding="utf-8",
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO runtime_lock_resources(
                    resource_id, lock_path, lock_id, owner_pid, status,
                    created_at, updated_at, cleaned_at, cleanup_error, cleanup_attempts
                ) VALUES (
                    'killed-runtime-resource', ?, 'killed-runtime-lock', ?, 'active',
                    '2026-03-23T00:00:00Z', '2026-03-23T00:00:00Z', NULL, NULL, 0
                )
                """,
                (str(lock_path), reused_pid),
            )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        lambda _job_payload: None,
    )

    start_worker()
    try:
        with pytest.raises(MigrationError, match="LOCAL_RUNTIME_ALREADY_ACTIVE"):
            acquire_runtime_process_lock(db_path=db_path)
    finally:
        stop_local_action_worker_daemon()
    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )
    with sqlite3.connect(db_path) as connection:
        killed_row = connection.execute(
            """
            SELECT status
            FROM runtime_lock_resources
            WHERE resource_id = 'killed-runtime-resource'
            """
        ).fetchone()
    assert killed_row == ("cleaned",)


def test_runtime_process_lock_is_held_until_the_holding_process_dies(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n",
            str(runtime_process_lock_path(db_path=db_path)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(MigrationError, match="LOCAL_RUNTIME_ALREADY_ACTIVE"):
            acquire_runtime_process_lock(db_path=db_path)
    finally:
        holder.kill()
        holder.wait(timeout=10)
        if holder.stdout is not None:
            holder.stdout.close()

    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )


def test_release_runtime_lock_lease_returns_warning_when_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)
    lease = attach_runtime_lock_lease(
        db_path=db_path,
        busy_timeout_ms=1_000,
        runtime_lock=runtime_lock,
        event_type="test_runtime_lock_acquired",
        event_message="test runtime global lock acquired",
    ).lease

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.runtime_lock_coordinator.mark_runtime_lock_resource_cleaned",
        lambda **_: (_ for _ in ()).throw(
            RuntimeError("simulated persistence failure")
        ),
    )

    release_result = release_runtime_lock_lease(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lease=lease,
    )

    assert release_result.process_lock_released is True
    assert release_result.persistence_failed is True
    assert release_result.warning_message is not None
    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )
