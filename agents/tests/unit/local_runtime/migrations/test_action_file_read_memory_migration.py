from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.context_search import (
    search_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    activate_embedding_generation_in_transaction,
    load_active_embedding_generation,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    store_embedding_success,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationSpec,
    verify_database_integrity,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.models import ToolInvocationStartInput
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)

from .support import (
    _configure_connection,
    _migrations_through,
    apply_migrations,
    load_default_migrations,
)

PREVIOUS_MIGRATION = "0065_fact_activity_checkpoint.sql"
ACTION_FILE_READ_MIGRATION = "0066_action_file_read_memory.sql"
BUSY_TIMEOUT_MS = 1_000
EXECUTION_SESSION_ID = "historical-execution-session-1"
MANIFEST_ID = "historical-manifest-1"
QUERY_EMBEDDING = (1.0, *(0.0 for _ in range(511)))


@pytest.mark.parametrize("include_legacy_line_count", [True, False])
def test_action_file_read_migration_preserves_catalog_and_backfills_reads(
    tmp_path: Path,
    *,
    include_legacy_line_count: bool,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, PREVIOUS_MIGRATION),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    _insert_action(db_path)
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=ToolInvocationStartInput(
            invocation_id="historical-read-1",
            tool_request_id="historical-request-1",
            user_id="user-1",
            action_id="action-1",
            step_id="historical-step-1",
            tool_id="read",
            manifest_id=MANIFEST_ID,
            execution_session_id=EXECUTION_SESSION_ID,
            cwd=".",
            timeout_ms=5_000,
            intent_class="read_only",
            network_policy="cloud-proxy-only",
            command_summary_json=None,
            capability_snapshot_json={"required_capabilities": ["scoped_read"]},
            request_json={"args": {"path": "/Project/history.md"}},
            status="running",
            started_at="2026-08-01T09:02:00Z",
        ),
    )
    _insert_historical_completion(
        db_path,
        include_line_count=include_legacy_line_count,
    )
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=BUSY_TIMEOUT_MS)
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            existing_revision = register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="existing-log",
                content="existing catalog content",
            )

    migration = next(
        item for item in migrations if item.name == ACTION_FILE_READ_MIGRATION
    )
    _simulate_interrupted_action_file_read_migration(
        db_path=db_path,
        migration=migration,
    )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=migrations,
    )
    verify_database_integrity(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=BUSY_TIMEOUT_MS)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT fragments.user_id, work.generation_id, work.chunk_index,
                   fragments.fragment_id,
                   fragments.content_sha256
            FROM memory_embedding_work AS work
            JOIN memory_fragments AS fragments
              ON fragments.user_id = work.user_id
             AND fragments.fragment_id = work.fragment_id
            WHERE work.state = 'pending'
            """
        ).fetchall()
        for row in rows:
            store_embedding_success(
                connection,
                user_id=str(row["user_id"]),
                generation_id=int(row["generation_id"]),
                fragment_id=str(row["fragment_id"]),
                chunk_index=int(row["chunk_index"]),
                fragment_content_sha256=str(row["content_sha256"]),
                vector=QUERY_EMBEDDING,
                created_at="2026-08-01T09:04:00Z",
            )
        building = connection.execute(
            """
            SELECT generation_id FROM memory_embedding_user_generations
            WHERE user_id = 'user-1' AND state = 'building'
            """
        ).fetchone()
        if building is not None:
            activate_embedding_generation_in_transaction(
                connection,
                user_id="user-1",
                generation_id=int(building[0]),
                activated_at="2026-08-01T09:04:00Z",
            )
        embedding_generation = load_active_embedding_generation(
            connection,
            user_id="user-1",
        )
        assert embedding_generation is not None
        results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="fact-run-after-upgrade",
            query="historical evidence",
            query_embedding=QUERY_EMBEDDING,
            embedding_generation=embedding_generation,
            focus="all",
            center_time=None,
            radius_hours=None,
            limit=8,
        )
        preserved = connection.execute(
            "SELECT current_revision_id FROM memory_nodes WHERE source_record_id = ?",
            ("existing-log",),
        ).fetchone()
        journal_statuses = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM migration_journal
            WHERE migration_name = ?
            GROUP BY status
            """,
            (ACTION_FILE_READ_MIGRATION,),
        ).fetchall()

    assert results
    assert results[0]["source"] == "action_file_read"
    assert results[0]["record_id"] == "historical-read-1"
    assert results[0]["match_kind"] == "corroborated"
    assert results[0]["observed_at"] == "2026-08-01T09:03:00Z"
    assert preserved is not None
    assert preserved["current_revision_id"] == existing_revision.revision_id
    assert {row["status"]: row["count"] for row in journal_statuses} == {
        "completed": 1,
        "failed": 1,
    }


def _insert_action(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-1', 'ja', '2026-08-01T09:00:00Z',
                        '2026-08-01T09:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id, user_id, status, created_at, updated_at
                ) VALUES ('suggestion-1', 'user-1', 'processing',
                          '2026-08-01T09:00:00Z', '2026-08-01T09:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id, user_id, suggestion_id, status, final_output,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES ('action-1', 'user-1', 'suggestion-1', 'processing', '',
                          'test/action', 'v1',
                          '2026-08-01T09:00:00Z', '2026-08-01T09:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO execution_sessions(
                    execution_session_id, user_id, action_id,
                    parent_execution_session_id, exec_mode, cwd_path,
                    action_temp_dir, app_runtime_python, network_policy,
                    read_access_scope, capability_snapshot_json,
                    tool_allowlist_json, status, started_at
                ) VALUES (
                    ?, 'user-1', 'action-1', NULL, 'brokered_file_ops',
                    '/tmp/historical-workspace', '/tmp/historical-workspace/temp',
                    '/usr/bin/python3', 'cloud-proxy-only', 'workspace',
                    '{"allowed_capabilities":["scoped_read"]}', '["read"]',
                    'running', '2026-08-01T09:01:00Z'
                )
                """,
                (EXECUTION_SESSION_ID,),
            )
            connection.execute(
                """
                INSERT INTO workspace_manifests(
                    manifest_id, user_id, action_id, execution_session_id,
                    scratch_root_path, created_at, status
                ) VALUES (
                    ?, 'user-1', 'action-1', ?, '/tmp/historical-workspace',
                    '2026-08-01T09:01:00Z', 'ready'
                )
                """,
                (MANIFEST_ID, EXECUTION_SESSION_ID),
            )


def _insert_historical_completion(
    db_path: Path,
    *,
    include_line_count: bool,
) -> None:
    output = {
        "kind": "file",
        "path": "/Project/history.md",
        "content": "Historical evidence remains searchable after upgrade.",
        "offset": 1,
        "end_line": 1,
        "next_offset": None if include_line_count else 2,
        "truncated": not include_line_count,
    }
    if include_line_count:
        output["line_count"] = 1
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                UPDATE agent_actions
                SET status = 'success', final_output = 'historical completion',
                    updated_at = '2026-08-01T09:03:00Z'
                WHERE action_id = 'action-1'
                """
            )
            connection.execute(
                """
                UPDATE execution_sessions
                SET status = 'completed', completed_at = '2026-08-01T09:03:00Z'
                WHERE execution_session_id = ?
                """,
                (EXECUTION_SESSION_ID,),
            )
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'completed', completed_at = '2026-08-01T09:03:00Z'
                WHERE invocation_id = 'historical-read-1'
                """
            )
            connection.execute(
                """
                INSERT INTO tool_outputs(
                    output_id, invocation_id, search_text, stdout_text,
                    stderr_text, output_json, redaction_applied, created_at
                ) VALUES (
                    'historical-read-1', 'historical-read-1', ?, NULL,
                    NULL, ?, 0, '2026-08-01T09:03:00Z'
                )
                """,
                (output["content"], json.dumps(output, sort_keys=True)),
            )


def _simulate_interrupted_action_file_read_migration(
    *,
    db_path: Path,
    migration: MigrationSpec,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=BUSY_TIMEOUT_MS)
        connection.execute(
            """
            INSERT INTO migration_journal(
                migration_run_id,
                migration_name,
                component,
                from_version,
                to_version,
                started_at,
                status,
                app_version,
                checksum
            ) VALUES (
                'interrupted-0066', ?, 'local_runtime', 65, 66,
                '2026-08-01T09:04:00Z', 'running', 'test', ?
            )
            """,
            (migration.name, migration.checksum_sha256),
        )
        connection.executescript(migration.sql)
