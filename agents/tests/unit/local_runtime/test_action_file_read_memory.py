from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.context_search import (
    search_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    activate_embedding_generation_in_transaction,
    ensure_user_embedding_generation,
    load_active_embedding_generation,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    store_embedding_success,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling.action_file_read_memory import (
    build_action_file_read_memory_input,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ToolInvocationCompletionInput,
    ToolInvocationStartInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.schema.agent.base import JSONValue

from .embedding_test_support import TEST_EMBEDDING_SPECIFICATION
from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
ACTION_ID = "action-1"
INVOCATION_ID = "read-invocation-1"
COMPLETED_AT = "2026-08-01T09:12:00+00:00"
QUERY_EMBEDDING = (1.0, *(0.0 for _ in range(511)))


def test_completed_file_read_is_searchable_without_mutating_action_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _runtime_with_action(tmp_path)
    _start_read_invocation(db_path=db_path, context=context)
    events: list[str] = []
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.memory_catalog.observability."
        "emit_memory_catalog_event",
        lambda event, **_fields: events.append(event),
    )
    output = _file_output(
        content=(
            "The durable boundary uses catalog evidence from the real file. "
            'Literal [[ref:example note:"file text"]] remains file content.'
        )
    )

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        completion=ToolInvocationCompletionInput(
            invocation_id=INVOCATION_ID,
            status="completed",
            completed_at=COMPLETED_AT,
            output_json=output,
            output_storage_kind="inline_json",
            search_text=str(output["content"]),
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
            read_memory=build_action_file_read_memory_input(
                tool_id="read",
                status="completed",
                output=output,
            ),
        ),
    )

    ensure_user_embedding_generation(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, BUSY_TIMEOUT_MS)
        _index_pending_embeddings(connection)
        embedding_generation = load_active_embedding_generation(
            connection,
            user_id=USER_ID,
        )
        assert embedding_generation is not None
        results, epoch = search_memory_catalog(
            connection=connection,
            user_id=USER_ID,
            run_id="fact-run-1",
            query="durable boundary",
            query_embedding=QUERY_EMBEDDING,
            embedding_generation=embedding_generation,
            focus="all",
            center_time=None,
            radius_hours=None,
            limit=8,
        )
        action = connection.execute(
            "SELECT final_output FROM agent_actions WHERE action_id = ?",
            (ACTION_ID,),
        ).fetchone()
        step_count = connection.execute(
            "SELECT COUNT(*) FROM agent_action_steps WHERE action_id = ?",
            (ACTION_ID,),
        ).fetchone()[0]
        stored_output = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (INVOCATION_ID,),
        ).fetchone()
        link_count = connection.execute(
            "SELECT COUNT(*) FROM memory_links",
        ).fetchone()[0]

    assert results
    assert results[0]["source"] == "action_file_read"
    assert results[0]["record_id"] == INVOCATION_ID
    assert results[0]["match_kind"] == "corroborated"
    assert results[0]["source_path"] == "Project/config.md#L4-L4"
    assert results[0]["observed_at"] == COMPLETED_AT
    assert results[0]["context_handle"] == epoch.items[0].item.context_handle
    assert serialize_memory_epoch(epoch).items[0].source == "action_file_read"
    assert "durable boundary" in str(results[0]["content"])
    assert r"[\[ref:example" in str(results[0]["content"])
    assert action is not None and action["final_output"] == ""
    assert step_count == 0
    assert stored_output is not None
    assert json.loads(str(stored_output["output_json"])) == output
    assert link_count == 0
    assert events.count("memory_revision_activated") == 1


def _index_pending_embeddings(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT fragments.user_id, work.generation_id, work.chunk_index,
               fragments.fragment_id, fragments.content_sha256
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
            created_at=COMPLETED_AT,
        )
    building = connection.execute(
        """
        SELECT generation_id FROM memory_embedding_user_generations
        WHERE user_id = ? AND state = 'building'
        """,
        (USER_ID,),
    ).fetchone()
    if building is not None:
        activate_embedding_generation_in_transaction(
            connection,
            user_id=USER_ID,
            generation_id=int(building[0]),
            activated_at=COMPLETED_AT,
        )
    connection.commit()


def test_directory_read_is_not_registered_as_file_content_memory(
    tmp_path: Path,
) -> None:
    db_path, context = _runtime_with_action(tmp_path)
    _start_read_invocation(db_path=db_path, context=context)

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        completion=ToolInvocationCompletionInput(
            invocation_id=INVOCATION_ID,
            status="completed",
            completed_at=COMPLETED_AT,
            output_json={
                "kind": "directory",
                "path": "/Project",
                "entries": [{"name": "config.md", "kind": "file"}],
                "offset": 1,
                "next_offset": None,
                "truncated": False,
                "truncation_reason": None,
                "retry_hint": None,
            },
            output_storage_kind="inline_json",
            search_text="config.md",
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
            read_memory=None,
        ),
    )

    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_nodes WHERE source_type = 'action_file_read'"
        ).fetchone()[0]

    assert count == 0


def test_large_file_read_registers_memory_from_raw_output_before_spill(
    tmp_path: Path,
) -> None:
    db_path, context = _runtime_with_action(tmp_path)
    _start_read_invocation(db_path=db_path, context=context)
    output = _file_output(content="durable raw evidence " * 1_500)

    finalized = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        request=ToolResultFinalizationRequest(
            owner=InvocationToolResultOwner(
                invocation_id=INVOCATION_ID,
                completed_at=COMPLETED_AT,
                status="completed",
                completion_scope="invocation",
            ),
            output=output,
            search_text=str(output["content"]),
        ),
    )

    assert finalized.storage_kind == "action_file"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT revisions.inline_body
            FROM memory_nodes AS nodes
            JOIN memory_revisions AS revisions
              ON revisions.user_id = nodes.user_id
             AND revisions.revision_id = nodes.current_revision_id
            WHERE nodes.source_type = 'action_file_read'
              AND nodes.source_record_id = ?
            """,
            (INVOCATION_ID,),
        ).fetchone()
    assert row is not None
    assert "durable raw evidence" in str(row[0])


def test_redacted_file_read_is_not_registered_as_memory(tmp_path: Path) -> None:
    db_path, context = _runtime_with_action(tmp_path)
    _start_read_invocation(db_path=db_path, context=context)
    output = _file_output(content="redacted source content")

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        completion=ToolInvocationCompletionInput(
            invocation_id=INVOCATION_ID,
            status="completed",
            completed_at=COMPLETED_AT,
            output_json=output,
            output_storage_kind="inline_json",
            search_text=str(output["content"]),
            stdout_text=None,
            stderr_text=None,
            redaction_applied=True,
            read_memory=build_action_file_read_memory_input(
                tool_id="read",
                status="completed",
                output=output,
            ),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        memory_count = connection.execute(
            "SELECT COUNT(*) FROM memory_nodes WHERE source_type = 'action_file_read'"
        ).fetchone()[0]
        output_count = connection.execute(
            "SELECT COUNT(*) FROM tool_outputs WHERE invocation_id = ?",
            (INVOCATION_ID,),
        ).fetchone()[0]

    assert memory_count == 0
    assert output_count == 1


def test_agent_experience_read_keeps_audit_without_registering_memory(
    tmp_path: Path,
) -> None:
    db_path, context = _runtime_with_action(tmp_path)
    experience_root = tmp_path / "experience-revision" / "agent_experience"
    experience_root.mkdir(parents=True)
    experience_file = experience_root / "index.md"
    experience_file.write_text("# Agent Experience\n", encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO workspace_manifest_roots(
                    root_id, manifest_id, source_type, source_id, display_name,
                    canonical_real_path, real_path, can_read, can_apply_patch,
                    can_process_read, can_process_write, created_at
                ) VALUES (
                    'root:experience', ?, 'agent_experience', 'experience-revision-1',
                    'Agent Experience', ?, ?, 1, 0, 0, 0,
                    '2026-08-01T09:01:00Z'
                )
                """,
                (
                    context.manifest_id,
                    str(experience_root.resolve()),
                    str(experience_root.resolve()),
                ),
            )
    _start_read_invocation(db_path=db_path, context=context)
    output = _file_output(
        path=str(experience_file.resolve()),
        content="Reusable procedure that must not amplify itself.",
    )

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        completion=ToolInvocationCompletionInput(
            invocation_id=INVOCATION_ID,
            status="completed",
            completed_at=COMPLETED_AT,
            output_json=output,
            output_storage_kind="inline_json",
            search_text=str(output["content"]),
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
            read_memory=build_action_file_read_memory_input(
                tool_id="read",
                status="completed",
                output=output,
            ),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        memory_count = connection.execute(
            "SELECT COUNT(*) FROM memory_nodes WHERE source_type = 'action_file_read'"
        ).fetchone()[0]
        audit_row = connection.execute(
            """
            SELECT invocations.status, outputs.output_json
            FROM tool_invocations AS invocations
            JOIN tool_outputs AS outputs
              ON outputs.invocation_id = invocations.invocation_id
            WHERE invocations.invocation_id = ?
            """,
            (INVOCATION_ID,),
        ).fetchone()

    assert memory_count == 0
    assert audit_row is not None
    assert audit_row[0] == "completed"
    assert json.loads(str(audit_row[1])) == output


def _runtime_with_action(tmp_path: Path) -> tuple[Path, ActionExecutionContext]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES (?, 'ja', '2026-08-01T09:00:00Z', '2026-08-01T09:00:00Z')
                """,
                (USER_ID,),
            )
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id, user_id, status, created_at, updated_at
                ) VALUES ('suggestion-1', ?, 'processing',
                          '2026-08-01T09:00:00Z', '2026-08-01T09:00:00Z')
                """,
                (USER_ID,),
            )
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id, user_id, suggestion_id, initial_user_message_id,
                    execution_target_json, status, final_output,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES (?, ?, 'suggestion-1', 'message-1',
                          '{"kind":"scratch"}', 'processing', '',
                          'test/action', 'v1',
                          '2026-08-01T09:00:00Z', '2026-08-01T09:00:00Z')
                """,
                (ACTION_ID, USER_ID),
            )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        action_id=ACTION_ID,
        started_at="2026-08-01T09:01:00Z",
        allowed_tool_ids=("read",),
    )
    return db_path, context


def _start_read_invocation(*, db_path: Path, context: ActionExecutionContext) -> None:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=ToolInvocationStartInput(
            invocation_id=INVOCATION_ID,
            tool_request_id="read-request-1",
            user_id=USER_ID,
            action_id=ACTION_ID,
            step_id="read-step-1",
            tool_id="read",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="read_only",
            network_policy="cloud-proxy-only",
            command_summary_json=None,
            capability_snapshot_json={"required_capabilities": ["scoped_read"]},
            request_json={"args": {"path": "/Project/config.md"}},
            status="running",
            started_at="2026-08-01T09:11:00Z",
        ),
    )


def _file_output(
    *,
    content: str,
    path: str = "/Project/config.md",
) -> dict[str, JSONValue]:
    return {
        "kind": "file",
        "path": path,
        "content": content,
        "offset": 4,
        "column": 1,
        "end_line": 4,
        "end_column": len(content),
        "total_lines": 20,
        "next_offset": 5,
        "next_column": 1,
        "truncated": True,
        "truncation_reason": "line_count_budget",
        "retry_hint": "Continue at line 5.",
    }
