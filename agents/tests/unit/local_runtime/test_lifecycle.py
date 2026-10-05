from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.app_runtime_verification import (
    LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV,
)
from pantaray_agents.local_runtime.memory_catalog.errors import MemoryCutoverError
from pantaray_agents.local_runtime.runtime.bootstrap import (
    LLM_PROXY_URL_ENV,
    LOCAL_ARTIFACT_ROOT_ENV,
    LOCAL_DB_BUSY_TIMEOUT_MS_ENV,
    LOCAL_DB_PATH_ENV,
    WEB_TOOLS_PROXY_URL_ENV,
)
from pantaray_agents.local_runtime.runtime.lifecycle import (
    start_local_runtime_if_enabled,
    stop_local_runtime_if_enabled,
)
from pantaray_agents.local_runtime.runtime.local_api_auth import (
    LocalApiTokenUnavailableError,
    read_local_api_token,
)
from pantaray_agents.local_runtime.runtime.process_lock import (
    acquire_runtime_process_lock,
    release_runtime_process_lock,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    HELPER_INSTANCE_ID_ENV,
    MAIN_PROCESS_PID_ENV,
)
from pantaray_agents.local_runtime.runtime.runtime_lock_coordinator import (
    RuntimeProcessLockLease,
)
from pantaray_agents.local_runtime.runtime.worker_daemon import (
    start_local_action_worker_daemon,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)

from .migrated_db import prepare_test_database


def _set_minimum_local_runtime_env(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    manifest_path = db_path.parent / "app-runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "python_path": str(Path(sys.executable).resolve()),
                "python_version": platform.python_version(),
                "python_sha256": hashlib.sha256(
                    Path(sys.executable).read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(LOCAL_DB_PATH_ENV, str(db_path))
    monkeypatch.setenv(LOCAL_DB_BUSY_TIMEOUT_MS_ENV, "1000")
    monkeypatch.setenv(LOCAL_ARTIFACT_ROOT_ENV, str(db_path.parent / "artifacts"))
    monkeypatch.setenv(HELPER_INSTANCE_ID_ENV, "helper-test-instance")
    monkeypatch.setenv(LLM_PROXY_URL_ENV, "https://llm-proxy.example.com")
    monkeypatch.setenv(MAIN_PROCESS_PID_ENV, "99999")
    monkeypatch.setenv(WEB_TOOLS_PROXY_URL_ENV, "https://search-proxy.example.com")
    monkeypatch.setenv(LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV, str(manifest_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def test_start_local_runtime_holds_single_process_lock_across_bootstrap_and_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = Path(tempfile.mkdtemp(prefix="prlf-", dir="/tmp"))
    db_path = runtime_dir / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        lambda _job_payload: None,
    )

    start_local_runtime_if_enabled()
    try:
        with pytest.raises(MigrationError, match="LOCAL_RUNTIME_ALREADY_ACTIVE"):
            acquire_runtime_process_lock(db_path=db_path)
        with sqlite3.connect(db_path) as connection:
            active_row = connection.execute(
                """
                SELECT status
                FROM runtime_lock_resources
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        assert active_row == ("active",)
    finally:
        stop_local_runtime_if_enabled()

    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )
    with sqlite3.connect(db_path) as connection:
        cleaned_row = connection.execute(
            """
            SELECT status
            FROM runtime_lock_resources
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert cleaned_row == ("cleaned",)
    shutil.rmtree(runtime_dir, ignore_errors=True)


def test_production_startup_completes_catalog_cutover_before_worker_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = Path(tempfile.mkdtemp(prefix="prcf-", dir="/tmp"))
    db_path = runtime_dir / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-1', 'ja', '2026-07-23T00:00:00Z',
                    '2026-07-23T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO activity_logs(
                log_id, user_id, period_start, period_end, description, status,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES (
                'log-1', 'user-1', '2026-07-23T00:00:00Z',
                '2026-07-23T00:04:00Z', 'Observed activity.', 'success',
                'activity_description', '1.0', '2026-07-23T00:04:00Z',
                '2026-07-23T00:04:00Z'
            )
            """
        )

    def _start_worker_after_cutover(
        *, runtime_process_lock: RuntimeProcessLockLease
    ) -> None:
        with sqlite3.connect(db_path) as connection:
            state = connection.execute(
                "SELECT state FROM memory_catalog_cutovers"
            ).fetchone()
        assert state == ("completed",)
        start_local_action_worker_daemon(runtime_process_lock=runtime_process_lock)

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.lifecycle.start_local_action_worker_daemon",
        _start_worker_after_cutover,
    )
    start_local_runtime_if_enabled()
    try:
        with sqlite3.connect(db_path) as connection:
            cutover = connection.execute(
                "SELECT state FROM memory_catalog_cutovers"
            ).fetchone()
            node = connection.execute(
                """
                SELECT lifecycle, integrity
                FROM memory_nodes
                WHERE source_type = 'activity_log'
                  AND source_record_id = 'log-1'
                """
            ).fetchone()
        assert cutover == ("completed",)
        assert node == ("active", "healthy")
    finally:
        stop_local_runtime_if_enabled()
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_production_startup_does_not_start_services_when_cutover_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    worker_started = False
    socket_started = False

    def _fail_cutover(**_: object) -> None:
        raise MemoryCutoverError("cutover failed")

    def _start_worker(**_: object) -> None:
        nonlocal worker_started
        worker_started = True

    def _start_socket() -> None:
        nonlocal socket_started
        socket_started = True

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.initialization.run_memory_catalog_cutover",
        _fail_cutover,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.lifecycle.start_local_action_worker_daemon",
        _start_worker,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.lifecycle.start_local_control_socket_server",
        _start_socket,
    )

    with pytest.raises(MemoryCutoverError, match="cutover failed"):
        start_local_runtime_if_enabled()

    assert worker_started is False
    assert socket_started is False
    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )


def test_local_api_token_is_minted_per_start_and_destroyed_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = Path(tempfile.mkdtemp(prefix="prlt-", dir="/tmp"))
    db_path = runtime_dir / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        lambda _job_payload: None,
    )

    start_local_runtime_if_enabled()
    try:
        first_token = read_local_api_token()
    finally:
        stop_local_runtime_if_enabled()

    with pytest.raises(LocalApiTokenUnavailableError):
        read_local_api_token()

    start_local_runtime_if_enabled()
    try:
        assert read_local_api_token() != first_token
    finally:
        stop_local_runtime_if_enabled()
        shutil.rmtree(runtime_dir, ignore_errors=True)
