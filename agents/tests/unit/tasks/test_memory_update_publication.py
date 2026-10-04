from __future__ import annotations

import errno
import json
import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from pantaray_agents.local_runtime.memory_catalog import (
    publication as publication_module,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
    AgentExperienceContent,
    AgentExperienceScope,
    experience_entry_path,
    render_action_evidence_anchor,
    render_agent_experience_markdown,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_delta import (
    rebuild_agent_experience_index,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_validation import (
    validate_agent_experience_evidence_links,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_domain_publication import (
    FactArtifactPublication,
    fact_publication_from_intent,
    publish_fact_artifact,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_recovery import (
    recover_artifact_revision_intents,
    recover_pending_artifact_intent_for_source,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_repair import (
    write_artifact_repair_projection,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    derived_experience_ids,
    memory_run_binding_from_payload,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    DraftLink,
    MemoryDocument,
    MemoryDraftCheckpoint,
    MemoryRevision,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
)
from pantaray_agents.local_runtime.runtime.memory_update_queue import (
    build_local_memory_update_enqueue_request,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.tasks.memory_update_completion import publish_memory_update_run
from pantaray_agents.tasks.memory_update_context import (
    PreparedMemoryUpdateRun,
    prepare_memory_update_run,
)
from pantaray_agents.tasks.types import MemoryUpdateJobPayload

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
JOB_ID = "job-memory-1"
PROCESS_ID = "process-memory-1"
NOW = "2026-01-05T00:00:00Z"
PROMPT_NAME = "memory_update"
PROMPT_VERSION = "1.0"


def _payload(
    job_id: str = JOB_ID, process_id: str = PROCESS_ID
) -> MemoryUpdateJobPayload:
    return {
        "job_id": job_id,
        "process_id": process_id,
        "user_id": USER_ID,
        "enqueued_at": NOW,
        "short_insight_ids": ["insight-1"],
        "summary_ids": [],
        "action_terminals": [],
    }


def _runtime(tmp_path: Path) -> LocalMemoryFileEditorRuntime:
    return LocalMemoryFileEditorRuntime(
        artifact_root=tmp_path / "artifacts",
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )


def _bootstrap(runtime: LocalMemoryFileEditorRuntime) -> None:
    apply_migrations(
        db_path=runtime.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(runtime.db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES (?, 'ja', ?, ?)
                """,
                (USER_ID, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO agent_insights(
                    insight_id, user_id, status, short_term_insight_data, facts,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES ('insight-1', ?, 'success', 'Prefers uv over pip.', '',
                          'insight', '1.0', ?, ?)
                """,
                (USER_ID, NOW, NOW),
            )


def _claim(
    runtime: LocalMemoryFileEditorRuntime, payload: MemoryUpdateJobPayload
) -> None:
    enqueue_local_job_with_connection(
        db_path=str(runtime.db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        request=build_local_memory_update_enqueue_request(payload),
    )
    claimed = claim_next_pending_job(
        db_path=str(runtime.db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
        owner_user_id=USER_ID,
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None


async def _brief(source: MemorySource, memory_text: str) -> str:
    return f"## Profile Brief for {source}\n- {memory_text[:20]}"


def _experience_entry(
    experience_id: str,
    scope: AgentExperienceScope = AgentExperienceScope(kind="user", key=None),
) -> str:
    return render_agent_experience_markdown(
        AgentExperienceContent(
            experience_id=experience_id,
            scope=scope,
            applies_when="A shared terminal boundary fails.",
            observed_approach="Traced every caller before changing the boundary.",
            outcome="effective",
            next_time_rule="Trace every caller first.",
            observed_result="One root fix restored every caller.",
            supersedes_experience_id=None,
        )
    )


async def _publish(
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
    prepared: PreparedMemoryUpdateRun,
) -> None:
    await publish_memory_update_run(
        runtime=runtime,
        payload=payload,
        prepared=prepared,
        prompt_name=PROMPT_NAME,
        prompt_version=PROMPT_VERSION,
        build_profile_brief=_brief,
        applied_memory_request_ids=(),
    )


def _rows(runtime: LocalMemoryFileEditorRuntime, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(runtime.db_path) as connection:
        return connection.execute(sql).fetchall()


def _published_events(runtime: LocalMemoryFileEditorRuntime) -> list[str]:
    return [
        json.loads(str(row[0]))["source"]
        for row in _rows(
            runtime,
            """
            SELECT payload_json FROM process_events
            WHERE event_name = 'memory_category_published'
            ORDER BY created_at, event_id
            """,
        )
    ]


@pytest.mark.asyncio
async def test_a_run_that_changes_nothing_publishes_nothing(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        await _publish(runtime, payload, prepared)

    assert _rows(runtime, "SELECT revision_id FROM memory_revisions") == []
    assert _rows(runtime, "SELECT revision_id FROM memory_revision_intents") == []
    assert _rows(runtime, "SELECT node_id FROM memory_nodes") == []
    assert _published_events(runtime) == []
    completed = _rows(
        runtime,
        """
        SELECT payload_json FROM process_events
        WHERE event_name = 'memory_update_completed'
        """,
    )
    assert len(completed) == 1
    assert json.loads(str(completed[0][0])) == {"published_sources": []}


@pytest.mark.asyncio
async def test_each_changed_category_publishes_its_own_head(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="facts/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "facts/index.md"
            ),
            content="# Facts\n\n- Prefers uv over pip.\n",
        )
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="insights/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "insights/index.md"
            ),
            content="# Insights\n\n- Values fast tooling.\n",
        )
        experience_id = derived_experience_ids(JOB_ID)[0]
        prepared.router.append_document(
            expected_revision=prepared.router.draft.draft_revision,
            path=experience_entry_path(experience_id),
            content=_experience_entry(experience_id),
        )
        await _publish(runtime, payload, prepared)

    assert sorted(_published_events(runtime)) == [
        "agent_experience",
        "fact",
        "long_term_insight",
    ]
    heads = _rows(
        runtime,
        """
        SELECT source_type, lifecycle FROM memory_nodes ORDER BY source_type
        """,
    )
    assert heads == [
        ("agent_experience", "active"),
        ("fact", "active"),
        ("long_term_insight", "active"),
    ]
    # The unified run owns no legacy run row, but the domain heads a downstream
    # agent reads still have to be current.
    facts = _rows(
        runtime,
        "SELECT status, facts_profile_brief IS NOT NULL FROM agent_facts",
    )
    assert facts == [("success", 1)]
    state = _rows(
        runtime,
        """
        SELECT source_insight_id, source_run_id FROM agent_long_term_insight_state
        """,
    )
    assert state == [(None, JOB_ID)]
    assert _rows(runtime, "SELECT fact_run_id FROM agent_fact_structuring_runs") == []
    assert (
        _rows(runtime, "SELECT insight_update_id FROM agent_insight_update_runs") == []
    )
    assert _rows(runtime, "SELECT job_id FROM agent_experience_extraction_runs") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "repository"])
async def test_an_experience_scope_key_need_not_be_a_registered_folder(
    tmp_path: Path, kind: Literal["workspace", "repository"]
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        experience_id = derived_experience_ids(JOB_ID)[0]
        prepared.router.append_document(
            expected_revision=prepared.router.draft.draft_revision,
            path=experience_entry_path(experience_id),
            content=_experience_entry(
                experience_id,
                AgentExperienceScope(kind=kind, key="/Users/me/src/unregistered"),
            ),
        )
        await _publish(runtime, payload, prepared)

    assert _published_events(runtime) == ["agent_experience"]


@pytest.mark.asyncio
async def test_a_rerun_does_not_republish_a_category_that_already_activated(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)

    # First attempt: only the Fact category lands before the run is interrupted.
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        fact_route = next(
            route for route in prepared.router.routes if route.source == "fact"
        )
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="facts/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "facts/index.md"
            ),
            content="# Facts\n\n- Prefers uv over pip.\n",
        )
        publish_fact_artifact(
            db_path=runtime.db_path,
            busy_timeout_ms=runtime.busy_timeout_ms,
            artifact_root=runtime.artifact_root,
            publication=FactArtifactPublication(
                user_id=USER_ID,
                fact_id=prepared.fact_id,
                memory_run=memory_run_binding_from_payload(payload),
                draft=fact_route.session.draft,
                profile_brief="## Profile Brief\n- interrupted run",
                prompt_name=PROMPT_NAME,
                prompt_version=PROMPT_VERSION,
                source_insight_ids=("insight-1",),
                created_at=NOW,
            ),
        )

    fact_revision = _rows(runtime, "SELECT revision_id FROM memory_revisions")
    assert len(fact_revision) == 1

    # Second attempt of the same job: Fact is skipped, Insight is finished.
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        assert prepared.editable_sources == ("long_term_insight", "agent_experience")
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="insights/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "insights/index.md"
            ),
            content="# Insights\n\n- Values fast tooling.\n",
        )
        await _publish(runtime, payload, prepared)

    assert _published_events(runtime) == ["fact", "long_term_insight"]
    assert len(_rows(runtime, "SELECT revision_id FROM memory_revisions")) == 2
    completed = _rows(
        runtime,
        """
        SELECT payload_json FROM process_events
        WHERE event_name = 'memory_update_completed'
        """,
    )
    assert json.loads(str(completed[0][0])) == {
        "published_sources": ["fact", "long_term_insight"]
    }


@pytest.mark.asyncio
async def test_a_fact_head_a_memory_run_published_can_still_be_repaired(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="facts/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "facts/index.md"
            ),
            content="# Facts\n\n- Prefers uv over pip.\n",
        )
        await _publish(runtime, payload, prepared)

    revision_row = _rows(
        runtime,
        """
        SELECT revisions.user_id, revisions.revision_id, revisions.node_id,
               revisions.artifact_root_path, revisions.content_sha256,
               revisions.created_at
        FROM memory_revisions AS revisions
        JOIN memory_nodes AS nodes ON nodes.node_id = revisions.node_id
        WHERE nodes.source_type = 'fact'
        """,
    )[0]
    revision = MemoryRevision(
        user_id=str(revision_row[0]),
        revision_id=str(revision_row[1]),
        node_id=str(revision_row[2]),
        body_kind="artifact_tree",
        inline_body=None,
        artifact_root_path=str(revision_row[3]),
        fragment_schema_version=1,
        content_sha256=str(revision_row[4]),
        profile_brief="## Profile Brief\n- repaired",
        created_at=str(revision_row[5]),
    )

    # The repair path rewrites the same agent_facts row the run upserted; a run
    # that skipped that upsert would leave repair with nothing to update.
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            write_artifact_repair_projection(
                connection=connection,
                revision=revision,
                source="fact",
                source_record_id=prepared.fact_id,
            )

    assert _rows(
        runtime, "SELECT structured_fact_sha256, facts_profile_brief FROM agent_facts"
    ) == [(revision.content_sha256, "## Profile Brief\n- repaired")]


def _set_run_runtime_state(
    runtime: LocalMemoryFileEditorRuntime,
    *,
    job_status: str,
    process_status: str,
    current_job_id: str | None,
) -> None:
    with sqlite3.connect(runtime.db_path) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?", (job_status, JOB_ID)
            )
            connection.execute(
                """
                UPDATE processes SET status = ?, current_job_id = ?
                WHERE process_id = ?
                """,
                (process_status, current_job_id, PROCESS_ID),
            )


@pytest.mark.asyncio
async def test_startup_recovery_leaves_a_deferred_runs_fact_intent_for_its_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)

    def fail_materialization(**_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="facts/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "facts/index.md"
            ),
            content="# Facts\n\n- Prefers uv over pip.\n",
        )
        with monkeypatch.context() as materialize_failure:
            materialize_failure.setattr(
                publication_module,
                "materialize_artifact_revision",
                fail_materialization,
            )
            with pytest.raises(OSError):
                await _publish(runtime, payload, prepared)

    assert len(_rows(runtime, "SELECT revision_id FROM memory_revision_intents")) == 1

    # The failure deferred the job back to the queue, so a startup sweep must
    # leave the Fact intent to the attempt that will republish it.
    _set_run_runtime_state(
        runtime, job_status="queued", process_status="enqueued", current_job_id=None
    )
    assert (
        recover_artifact_revision_intents(
            db_path=runtime.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=runtime.artifact_root,
        )
        == 0
    )
    assert len(_rows(runtime, "SELECT revision_id FROM memory_revision_intents")) == 1
    assert _rows(runtime, "SELECT deletion_id FROM memory_artifact_deletions") == []

    # The job's own next attempt reclaims it under the user memory lock.
    _set_run_runtime_state(
        runtime, job_status="running", process_status="running", current_job_id=JOB_ID
    )
    assert recover_pending_artifact_intent_for_source(
        db_path=runtime.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=runtime.artifact_root,
        user_id=USER_ID,
        source="fact",
        source_record_id=prepared.fact_id,
    )
    assert _rows(
        runtime,
        "SELECT lifecycle FROM memory_nodes WHERE source_type = 'fact'",
    ) == [("active",)]
    assert _published_events(runtime) == ["fact"]


def _action_evidence_fragment(
    runtime: LocalMemoryFileEditorRuntime, *, user_id: str, action_id: str
) -> tuple[str, str]:
    """Publish one Action memory and return its (revision_id, fragment_id)."""

    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            revision = register_inline_domain_memory(
                connection=connection,
                user_id=user_id,
                source="action",
                source_record_id=action_id,
                content=f"# Action {action_id}\n\nFinished the work.\n",
            )
            fragment_id = str(
                connection.execute(
                    """
                    SELECT fragment_id FROM memory_fragments
                    WHERE user_id = ? AND revision_id = ?
                      AND block_kind <> 'document_root'
                    ORDER BY block_index LIMIT 1
                    """,
                    (user_id, revision.revision_id),
                ).fetchone()[0]
            )
    return revision.revision_id, fragment_id


def _experience_draft_with_evidence(
    *, user_id: str, action_id: str, target_fragment_id: str
) -> MemoryDraftCheckpoint:
    experience_id = derived_experience_ids(JOB_ID)[0]
    entry = (
        f"{_experience_entry(experience_id)}"
        f'- Evidence: Action {action_id} [[ref:ref_1 note:"source action"]]\n'
    )
    documents = rebuild_agent_experience_index(
        (
            MemoryDocument(AGENT_EXPERIENCE_INDEX_PATH, ""),
            MemoryDocument(experience_entry_path(experience_id), entry),
        )
    )
    return MemoryDraftCheckpoint(
        draft_session_id="draft-1",
        user_id=user_id,
        owner_node_id="node-experience",
        base_revision_id=None,
        draft_revision="sha256:" + "a" * 64,
        documents=documents,
        links=(
            DraftLink(
                local_ref_id="ref_1",
                target_fragment_id=target_fragment_id,
                source_path=experience_entry_path(experience_id),
                source_anchor_text=render_action_evidence_anchor(action_id),
                source_anchor_occurrence=1,
                reference_note="source action",
                created_at=NOW,
                state="pending",
            ),
        ),
        applied_commands=(),
    )


def test_experience_evidence_must_name_an_action_of_its_own_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    revision_id, fragment_id = _action_evidence_fragment(
        runtime, user_id=USER_ID, action_id="action-1"
    )
    draft = _experience_draft_with_evidence(
        user_id=USER_ID, action_id="action-1", target_fragment_id=fragment_id
    )
    payload = _payload()
    payload["action_terminals"] = [
        {
            "source_id": "action-1:4",
            "action_id": "action-1",
            "action_completed_at": NOW,
            "source_action_revision_id": revision_id,
            "turn_start_step_number": 1,
            "turn_end_step_number": 4,
            "action_prompt_name": "action",
            "action_prompt_version": "1.0",
        }
    ]

    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        validate_agent_experience_evidence_links(
            connection=connection,
            draft=draft,
            binding=memory_run_binding_from_payload(payload),
        )
        with pytest.raises(MemoryCatalogIntegrityError, match="outside its Memory run"):
            validate_agent_experience_evidence_links(
                connection=connection,
                draft=draft,
                binding=memory_run_binding_from_payload(_payload()),
            )


def test_experience_evidence_cannot_reach_another_users_action(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    with sqlite3.connect(runtime.db_path) as connection:
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-2', 'ja', ?, ?)
                """,
                (NOW, NOW),
            )
    revision_id, other_fragment_id = _action_evidence_fragment(
        runtime, user_id="user-2", action_id="action-1"
    )
    draft = _experience_draft_with_evidence(
        user_id=USER_ID, action_id="action-1", target_fragment_id=other_fragment_id
    )
    payload = _payload()
    payload["action_terminals"] = [
        {
            "source_id": "action-1:4",
            "action_id": "action-1",
            "action_completed_at": NOW,
            "source_action_revision_id": revision_id,
            "turn_start_step_number": 1,
            "turn_end_step_number": 4,
            "action_prompt_name": "action",
            "action_prompt_version": "1.0",
        }
    ]

    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with pytest.raises(MemoryCatalogIntegrityError, match="displayed Action"):
            validate_agent_experience_evidence_links(
                connection=connection,
                draft=draft,
                binding=memory_run_binding_from_payload(payload),
            )


def test_a_fact_intent_written_before_the_memory_run_still_parses() -> None:
    legacy_payload = json.dumps(
        {
            "user_id": USER_ID,
            "fact_id": "fact-1",
            "fact_run_id": "fact-run-1",
            "profile_brief": "## Profile Brief\n- legacy",
            "prompt_name": "fact_structuring",
            "prompt_version": "1.0",
            "source_insight_ids": ["insight-1"],
            "source_activity_log_ids": [],
            "base_storage_path": None,
            "base_sha256": None,
            "created_at": NOW,
        }
    )

    publication = fact_publication_from_intent(
        payload_json=legacy_payload,
        draft=MemoryDraftCheckpoint(
            draft_session_id="draft-1",
            user_id=USER_ID,
            owner_node_id="node-fact",
            base_revision_id=None,
            draft_revision="sha256:" + "b" * 64,
            documents=(),
            links=(),
            applied_commands=(),
        ),
    )

    # A retired run's ledger is closed, so recovery publishes the projection
    # under no Memory run at all.
    assert publication.memory_run is None
    assert (publication.fact_id, publication.profile_brief) == (
        "fact-1",
        "## Profile Brief\n- legacy",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_direction", [True, False])
async def test_plain_todos_persist_without_becoming_the_long_term_profile(
    tmp_path: Path,
    has_direction: bool,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)
    brief_inputs: list[tuple[MemorySource, str]] = []

    async def build_brief(source: MemorySource, memory_text: str) -> str:
        brief_inputs.append((source, memory_text))
        return await _brief(source, memory_text)

    todo = "# TODOs\n- Observed: revise the estimate requested on Friday.\n"
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        if has_direction:
            prepared.router.replace_document(
                expected_revision=prepared.router.draft.draft_revision,
                path="insights/index.md",
                expected_text=next(
                    item.content
                    for item in prepared.router.draft.documents
                    if item.source_path == "insights/index.md"
                ),
                content="# Direction\nBuild a sustainable business.\n",
            )
        prepared.router.append_document(
            path="insights/todos.md",
            content=todo,
            expected_revision=prepared.router.draft.draft_revision,
        )
        await publish_memory_update_run(
            runtime=runtime,
            payload=payload,
            prepared=prepared,
            prompt_name=PROMPT_NAME,
            prompt_version=PROMPT_VERSION,
            build_profile_brief=build_brief,
            applied_memory_request_ids=(),
        )
    if has_direction:
        assert len(brief_inputs) == 1
        assert brief_inputs[0][0] == "long_term_insight"
        assert "Build a sustainable business." in brief_inputs[0][1]
        assert "revise the estimate" not in brief_inputs[0][1]
    else:
        assert brief_inputs == []
        assert _rows(runtime, "SELECT profile_brief FROM memory_revisions") == [
            ("No durable long-term insights recorded.",)
        ]

    with MemoryRunWorkspaceScope() as scope:
        reopened = prepare_memory_update_run(
            runtime=runtime,
            payload=_payload("job-2", "process-2"),
            workspace_scope=scope,
        )
        documents = {
            doc.source_path: doc.content for doc in reopened.router.draft.documents
        }
        assert documents["insights/todos.md"] == todo
        if has_direction:
            assert "Build a sustainable business." in documents["insights/index.md"]
        else:
            assert documents["insights/index.md"] == ""


@pytest.mark.asyncio
async def test_memory_completion_queues_latest_insight_atomically_and_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.tasks import memory_update_completion as completion

    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    payload["short_insight_ids"].append("insight-2")
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute(
            """INSERT INTO agent_insights(insight_id,user_id,status,
               short_term_insight_data,facts,prompt_name,prompt_version,created_at,updated_at)
               VALUES ('insight-2',?,'success','Still pending.','','insight','2.2',?,?)""",
            (USER_ID, NOW, NOW),
        )
    _claim(runtime, payload)
    original_enqueue = completion.enqueue_suggestion_for_insight

    def interrupted_enqueue(**kwargs):
        original_enqueue(**kwargs)
        raise OSError(errno.ENOSPC, "commit interrupted")

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        prepared.router.replace_document(
            expected_revision=prepared.router.draft.draft_revision,
            path="insights/index.md",
            expected_text=next(
                item.content
                for item in prepared.router.draft.documents
                if item.source_path == "insights/index.md"
            ),
            content="# Direction\nGrow sustainably.\n",
        )
        assert _rows(runtime, "SELECT suggestion_id FROM agent_suggestions") == []
        with monkeypatch.context() as interruption:
            interruption.setattr(
                completion, "enqueue_suggestion_for_insight", interrupted_enqueue
            )
            with pytest.raises(OSError, match="commit interrupted"):
                await _publish(runtime, payload, prepared)
    # The category is durable, but neither completion nor its Suggestion escaped
    # the failed completion transaction. Restart can finish from this point.
    assert _published_events(runtime) == ["long_term_insight"]
    assert _rows(runtime, "SELECT suggestion_id FROM agent_suggestions") == []
    assert (
        _rows(
            runtime,
            "SELECT event_id FROM process_events WHERE event_name='memory_update_completed'",
        )
        == []
    )
    for _ in range(2):
        completion.complete_memory_update_run(
            runtime=runtime, payload=payload, applied_note_record_ids=()
        )
    queued = _rows(
        runtime,
        "SELECT payload_json FROM job_payloads JOIN jobs USING(job_id) WHERE job_type='generate_suggestion'",
    )
    assert len(queued) == 1
    assert json.loads(str(queued[0][0]))["insight_id"] == "insight-2"
    assert (
        len(
            _rows(
                runtime,
                "SELECT event_id FROM process_events WHERE event_name='memory_update_completed'",
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_recording_disabled_before_memory_completion_suppresses_suggestion(
    tmp_path: Path,
) -> None:
    from uuid import UUID

    from pantaray_agents.local_runtime.context import store
    from pantaray_agents.schema.context_source import SourceStopped

    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as conn:
        with immediate_transaction(conn):
            store.compare_source(
                conn,
                USER_ID,
                None,
                SourceStopped(
                    kind="stopped",
                    epoch=UUID(int=1),
                    policy_revision="policy",
                    reason="disabled",
                ),
            )
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        await _publish(runtime, payload, prepared)
    assert (
        len(
            _rows(
                runtime,
                "SELECT event_id FROM process_events WHERE event_name='memory_update_completed'",
            )
        )
        == 1
    )
    assert _rows(runtime, "SELECT suggestion_id FROM agent_suggestions") == []
