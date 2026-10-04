from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_gate import SourceGate
from pantaray_agents.local_runtime.context.source_transport import source_http_request
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    EmbeddingGeneration,
    EmbeddingSpecification,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_search import (
    search_semantic_fragments,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_control import DeferredLocalJob
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    bind_job_route_identity,
    require_current_route_identity,
)
from pantaray_agents.local_runtime.runtime.job_types import LOCAL_SUGGESTION_JOB_TYPE
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
)
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    MissingReconsideredInsight,
    ReconsideredInsight,
    capture_paused_for_user,
)
from pantaray_agents.local_runtime.runtime.suggestion_queue import (
    build_local_suggestion_enqueue_request,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    SuggestionResearchSnapshot,
)
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import SuggestionAgentResponse
from pantaray_agents.schema.context_source import (
    SourceBinding,
    SourceReady,
    SourceStopped,
)
from pantaray_agents.schema.memory_embeddings import MEMORY_EMBEDDING_DIMENSIONS
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.tasks.internal_jobs.suggestion import _run_suggestion_job
from pantaray_agents.tasks.types import SuggestionJobPayload

_EMPTY_RESEARCH_SNAPSHOT = SuggestionResearchSnapshot(
    roots=(),
    stable_memory=SuggestionStableMemoryContext(
        prompt="No stable memory roots are available.",
        has_facts=False,
        has_insights=False,
    ),
    commands_allowed=False,
    read_access_scope="workspace",
)


def _payload() -> SuggestionJobPayload:
    return {
        "job_id": "job-1",
        "process_id": "process-1",
        "suggestion_id": "suggestion-1",
        "user_id": "user-1",
        "enqueued_at": "2026-03-27T00:00:00Z",
        "insight_id": "insight-1",
    }


@pytest.fixture(autouse=True)
def _stub_workspace_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.reserve_suggestion_start",
        lambda **_kwargs: "start",
    )
    workspace_settings = WorkspaceSettings(
        read_access_scope="workspace",
        organizations=(),
        projects=(),
        folders=(),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.list_workspace_settings",
        lambda **_kwargs: workspace_settings,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.build_suggestion_research_snapshot",
        lambda **_kwargs: _EMPTY_RESEARCH_SNAPSHOT,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.capture_paused_for_user",
        lambda **_kwargs: False,
    )
    # The desktop app is connected unless a test says otherwise.
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.owner_has_deliverable_session",
        lambda _owner_id: True,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_reconsidered_insight",
        lambda **_kwargs: ReconsideredInsight(
            short_term_insight="# Insight\nFixing the parser.",
            reconsideration_reason="The user switched goals.",
            source_cursor=None,
        ),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_user_settings_repository",
        AsyncMock(
            return_value=SimpleNamespace(
                get_ui_language=AsyncMock(return_value=RepositoryResult(data="ja"))
            )
        ),
    )


def _persist_source(db_path: Path, *, capture_paused: bool) -> None:
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        with immediate_transaction(conn):
            store.compare_source(
                conn,
                "user-1",
                store.get_source(conn, "user-1"),
                SourceReady(
                    kind="ready",
                    binding=SourceBinding(
                        user_id="user-1",
                        epoch=UUID(int=1),
                        policy_revision="policy-1",
                        store_id="store-1",
                        protocol_version=1,
                    ),
                    capture_paused=capture_paused,
                ),
            )


def _persist_stopped_source(db_path: Path) -> None:
    """The recorder could not be paused, so the app stopped it: still recording off."""
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        with immediate_transaction(conn):
            store.compare_source(
                conn,
                "user-1",
                store.get_source(conn, "user-1"),
                SourceStopped(
                    kind="stopped",
                    epoch=UUID(int=1),
                    policy_revision="policy-1",
                    reason="disabled",
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("recorder_stopped", [False, True])
async def test_a_job_dropped_because_recording_is_off_ends_its_processing_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder_stopped: bool,
) -> None:
    """The row the enqueueing transaction created must not stay `processing`.

    `processing` is what the suggestion route reports as a stream still running,
    so a dropped job would leave the UI waiting for a Suggestion forever.
    """
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-1', 'ja', '2026-03-27T00:00:00Z', '2026-03-27T00:00:00Z')
            """
        )
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        activity_repository=SimpleNamespace(),  # type: ignore[arg-type]
    )
    created = await repository.create_processing_suggestion_row(
        user_id="user-1",
        suggestion_id="suggestion-1",
        created_at="2026-03-27T00:00:00Z",
    )
    assert created.error is None
    get_agent = AsyncMock()
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        get_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.capture_paused_for_user",
        capture_paused_for_user,
    )
    if recorder_stopped:
        _persist_stopped_source(db_path)
    else:
        _persist_source(db_path, capture_paused=True)

    await _run_suggestion_job(_payload())

    get_agent.assert_not_awaited()
    dropped = await repository.get_suggestion(
        user_id="user-1", suggestion_id="suggestion-1"
    )
    assert dropped.data is not None
    assert dropped.data["status"] == "canceled"


@pytest.mark.asyncio
async def test_run_suggestion_job_defers_a_locked_recording_state_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked database must defer the job, not fail it for good."""
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        discard_suggestion_if_processing=AsyncMock(),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=RepositoryResult(data=None)
        ),
    )

    def _locked(**_kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    def _defer(**_kwargs: object) -> None:
        raise DeferredLocalJob(job_id="job-1", scheduled_at="2026-03-27T00:00:05Z")

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.capture_paused_for_user",
        _locked,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.defer_local_job_if_retryable",
        _defer,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: ("runtime.db", 1_000),
    )

    with pytest.raises(DeferredLocalJob):
        await _run_suggestion_job(_payload())

    repository.discard_suggestion_if_processing.assert_not_awaited()
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_drops_a_suggestion_recording_was_turned_off_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        conn.execute("INSERT INTO users VALUES ('user-1', 'ja', NULL, 'now', 'now')")
        conn.commit()
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        save_suggestion=AsyncMock(return_value=SimpleNamespace(error=None)),
        discard_suggestion_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
    )
    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: {
            "prompt_name": "suggestion",
            "prompt_version": "1.0",
            "prompt_text": "prompt",
            "response_text": "answer",
            "request_images_count": 0,
            "used_images_count": 0,
        },
        process=AsyncMock(return_value=SimpleNamespace(status="success")),
    )
    get_agent = AsyncMock(return_value=agent)
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        get_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    # The gate itself is what this test exercises, so it reads the real source.
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.capture_paused_for_user",
        capture_paused_for_user,
    )

    # Recording was turned off after the short Insight queued this job.
    _persist_source(db_path, capture_paused=True)
    await _run_suggestion_job(_payload())

    # Dropped, not failed: no Suggestion is produced out of the tail recorded
    # before the toggle, and the job leaves no error behind.
    get_agent.assert_not_awaited()
    repository.save_suggestion.assert_not_awaited()
    repository.discard_suggestion_if_processing.assert_awaited_once()
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()

    _persist_source(db_path, capture_paused=False)
    await _run_suggestion_job(_payload())

    repository.save_suggestion.assert_awaited_once()

    # Recording is turned off while the agent is running. The preflight gate above
    # already passed, so only a second read, immediately before persistence, keeps
    # the Suggestion generated after the toggle out of the store.
    async def _turn_recording_off(_request: object) -> SimpleNamespace:
        _persist_source(db_path, capture_paused=True)
        return SimpleNamespace(status="success")

    agent.process = _turn_recording_off
    await _run_suggestion_job(_payload())

    repository.save_suggestion.assert_awaited_once()
    assert repository.discard_suggestion_if_processing.await_count == 2
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_returns_terminal_row_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "success",
                }
            )
        ),
        finalize_suggestion_start_error_if_processing=AsyncMock(),
    )
    get_agent = AsyncMock()
    snapshot_builder = Mock(side_effect=AssertionError("must not build snapshot"))
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        get_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.build_suggestion_research_snapshot",
        snapshot_builder,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: ("runtime.db", 1_000),
    )

    await _run_suggestion_job(_payload())

    snapshot_builder.assert_not_called()
    get_agent.assert_not_awaited()
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_finalizes_processing_row_on_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        save_suggestion=AsyncMock(return_value=SimpleNamespace(error=None)),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
    )
    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: {
            "prompt_name": "suggestion",
            "prompt_version": "1.0",
            "prompt_text": "",
            "response_text": "",
            "request_images_count": 0,
            "used_images_count": 0,
        },
        process=AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        AsyncMock(return_value=agent),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: ("runtime.db", 1_000),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await _run_suggestion_job(_payload())

    repository.finalize_suggestion_start_error_if_processing.assert_awaited_once()
    kwargs = repository.finalize_suggestion_start_error_if_processing.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["suggestion_id"] == "suggestion-1"
    assert kwargs["error_code"] == "SUGGESTION_WORKER_FAILED"


@pytest.mark.asyncio
async def test_run_suggestion_job_persists_success_terminal_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        save_suggestion=AsyncMock(return_value=SimpleNamespace(error=None)),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
    )
    response = SimpleNamespace(status="success")
    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: {
            "prompt_name": "suggestion",
            "prompt_version": "1.0",
            "prompt_text": "prompt",
            "response_text": "answer",
            "request_images_count": 1,
            "used_images_count": 1,
        },
        process=AsyncMock(return_value=response),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        AsyncMock(return_value=agent),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )

    await _run_suggestion_job(_payload())

    repository.save_suggestion.assert_awaited_once()
    kwargs = repository.save_suggestion.await_args.kwargs
    assert kwargs["prompt_name"] == "suggestion"
    assert kwargs["request_images_count"] == 1
    assert kwargs["used_images_count"] == 1
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_persists_error_terminal_row_without_start_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        save_suggestion=AsyncMock(return_value=SimpleNamespace(error=None)),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
    )
    response = SimpleNamespace(status="error")
    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: {
            "prompt_name": "suggestion",
            "prompt_version": "1.0",
            "prompt_text": "",
            "response_text": "",
            "request_images_count": 0,
            "used_images_count": 0,
        },
        process=AsyncMock(return_value=response),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        AsyncMock(return_value=agent),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )

    await _run_suggestion_job(_payload())

    repository.save_suggestion.assert_awaited_once()
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_defers_transient_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                error="database locked",
                retryable=True,
            )
        ),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=RepositoryResult(data=None)
        ),
    )
    get_agent = AsyncMock()
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        get_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: ("runtime.db", 1_000),
    )

    def _defer(**_kwargs: object) -> None:
        raise DeferredLocalJob(
            job_id="job-1",
            scheduled_at="2026-03-27T00:00:05Z",
        )

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.defer_local_job_if_retryable",
        _defer,
    )

    with pytest.raises(DeferredLocalJob):
        await _run_suggestion_job(_payload())

    get_agent.assert_not_awaited()
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_defers_transient_semantic_search_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=RepositoryResult(data=None)
        ),
    )
    locked = sqlite3.OperationalError("database is locked")

    def _raise_locked(*_args: object, **_kwargs: object) -> int:
        raise locked

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.memory_catalog.semantic_search._verify_projected_chunk",
        _raise_locked,
    )

    async def _search_memory(*_args: object, **_kwargs: object) -> None:
        with sqlite3.connect(":memory:") as connection:
            search_semantic_fragments(
                connection,
                user_id="user-1",
                visible_revision_ids=("revision-1",),
                query_embedding=(
                    1.0,
                    *([0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 1)),
                ),
                embedding_generation=EmbeddingGeneration(
                    1,
                    EmbeddingSpecification(
                        "amazon.titan-embed-text-v2:0",
                        MEMORY_EMBEDDING_DIMENSIONS,
                        True,
                    ),
                ),
                candidate_limit=8,
            )

    agent = SimpleNamespace(process=AsyncMock(side_effect=_search_memory))
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        AsyncMock(return_value=agent),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: ("runtime.db", 1_000),
    )

    def _defer(**_kwargs: object) -> None:
        raise DeferredLocalJob(
            job_id="job-1",
            scheduled_at="2026-03-27T00:00:05Z",
        )

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.defer_local_job_if_retryable",
        _defer,
    )

    with pytest.raises(DeferredLocalJob):
        await _run_suggestion_job(_payload())

    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_retries_terminal_save_database_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-1', 'ja', '2026-03-27T00:00:00Z', '2026-03-27T00:00:00Z')
            """
        )
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        activity_repository=SimpleNamespace(),  # type: ignore[arg-type]
    )
    created = await repository.create_processing_suggestion_row(
        user_id="user-1",
        suggestion_id="suggestion-1",
        created_at="2026-03-27T00:00:00Z",
    )
    assert created.error is None
    payload = _payload()
    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_suggestion_enqueue_request(payload),
    )
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=LOCAL_SUGGESTION_JOB_TYPE,
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    response = SuggestionAgentResponse(
        suggestion_id="suggestion-1",
        user_id="user-1",
        created_at="2026-03-27T00:00:00Z",
        answer="saved after retry",
        thinking="thinking",
        suggestion_summary="summary",
        status=StatusType.SUCCESS,
        has_suggestion=True,
        interaction_contract="action_offer",
    )
    save_attempts = 0

    async def save_suggestion(*args: object, **kwargs: object):
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return await repository.save_suggestion(*args, **kwargs)  # type: ignore[arg-type]

    finalize = AsyncMock(wraps=repository.finalize_suggestion_start_error_if_processing)
    repository_proxy = SimpleNamespace(
        get_suggestion=repository.get_suggestion,
        save_suggestion=save_suggestion,
        finalize_suggestion_start_error_if_processing=finalize,
    )
    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: {
            "prompt_name": "suggestion",
            "prompt_version": "1.0",
            "prompt_text": "prompt",
            "response_text": "saved after retry",
            "request_images_count": 0,
            "used_images_count": 0,
        },
        process=AsyncMock(return_value=response),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository_proxy),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        AsyncMock(return_value=agent),
    )

    with pytest.raises(DeferredLocalJob):
        await _run_suggestion_job(payload)

    with sqlite3.connect(db_path) as connection:
        job_status = connection.execute(
            "SELECT status FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        suggestion_status = connection.execute(
            "SELECT status FROM agent_suggestions WHERE suggestion_id = 'suggestion-1'"
        ).fetchone()
        connection.execute(
            "UPDATE jobs SET scheduled_at = '2000-01-01T00:00:00Z' WHERE job_id = 'job-1'"
        )
    assert job_status == ("queued",)
    assert suggestion_status == ("processing",)
    finalize.assert_not_awaited()

    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=LOCAL_SUGGESTION_JOB_TYPE,
        owner_user_id="user-1",
        claimed_by="worker-2",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    await _run_suggestion_job(payload)

    saved = await repository.get_suggestion(
        user_id="user-1",
        suggestion_id="suggestion-1",
    )
    assert saved.data is not None
    assert saved.data["status"] == "success"
    assert saved.data["answer"] == "saved after retry"
    assert save_attempts == 2
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_suggestion_job_finalizes_row_when_the_source_insight_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
    )
    get_agent = AsyncMock()

    def _missing(**_kwargs: object) -> ReconsideredInsight:
        raise MissingReconsideredInsight("short Insight not found: insight-1")

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_reconsidered_insight",
        _missing,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        get_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_local_runtime_db_config",
        lambda: ("runtime.db", 1_000),
    )

    with pytest.raises(MissingReconsideredInsight):
        await _run_suggestion_job(_payload())

    get_agent.assert_not_awaited()
    kwargs = repository.finalize_suggestion_start_error_if_processing.await_args.kwargs
    assert kwargs["error_code"] == "SUGGESTION_PREFLIGHT_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
async def test_waiting_or_superseded_review_does_not_call_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    superseded: bool,
) -> None:
    from datetime import UTC, datetime, timedelta

    payload = _payload()
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id,ui_language,created_at,updated_at) VALUES ('user-1','ja',?,?)",
            (payload["enqueued_at"], payload["enqueued_at"]),
        )
    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_suggestion_enqueue_request(payload),
    )
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type=LOCAL_SUGGESTION_JOB_TYPE,
            owner_user_id="user-1",
            claimed_by="worker",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        activity_repository=SimpleNamespace(),  # type: ignore[arg-type]
    )
    await repository.create_processing_suggestion_row(
        user_id="user-1",
        suggestion_id="suggestion-1",
        created_at=payload["enqueued_at"],
    )
    agent = SimpleNamespace(process=AsyncMock())
    for name, replacement in (
        ("read_local_runtime_db_config", lambda: (db_path, 1_000)),
        ("deps.get_suggestion_repository", AsyncMock(return_value=repository)),
        ("deps.get_suggestion_agent", AsyncMock(return_value=agent)),
    ):
        monkeypatch.setattr(
            f"pantaray_agents.tasks.internal_jobs.suggestion.{name}", replacement
        )
    due = datetime.now(UTC) + timedelta(minutes=15)
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.reserve_suggestion_start",
        lambda **_kwargs: "superseded" if superseded else due,
    )
    if superseded:
        await _run_suggestion_job(payload)
    else:
        with pytest.raises(DeferredLocalJob):
            await _run_suggestion_job(payload)
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT status,scheduled_at FROM jobs WHERE job_id='job-1'"
            ).fetchone() == (
                "queued",
                format_utc_iso(due),
            )
    agent.process.assert_not_awaited()
    saved = await repository.get_suggestion(
        user_id="user-1", suggestion_id="suggestion-1"
    )
    assert saved.data is not None
    assert saved.data["status"] == ("canceled" if superseded else "processing")


@pytest.mark.asyncio
async def test_a_suggestion_answered_on_a_replaced_route_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account changed while the agent ran, so nothing is stored.

    The Suggestion row must stay `processing` and the job must go back to
    `queued`: an answer inferred for the previous owner is not shown to the new
    one, and no terminal row stops the job from producing one again.
    """
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-1', 'ja', '2026-03-27T00:00:00Z', '2026-03-27T00:00:00Z')
            """
        )

    def _sign_in(session_version: str) -> None:
        import_desktop_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            desktop_access_token="header.payload.signature",
            expires_at="2099-03-27T01:00:00Z",
            session_version=session_version,
        )

    register_logged_out_owner("local-owner")
    mark_configured()
    _sign_in("1")
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        activity_repository=SimpleNamespace(),  # type: ignore[arg-type]
    )
    created = await repository.create_processing_suggestion_row(
        user_id="user-1",
        suggestion_id="suggestion-1",
        created_at="2026-03-27T00:00:00Z",
    )
    assert created.error is None
    payload = _payload()
    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_suggestion_enqueue_request(payload),
    )
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type=LOCAL_SUGGESTION_JOB_TYPE,
            owner_user_id="user-1",
            claimed_by="worker-1",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )

    async def _process(_request: object) -> SuggestionAgentResponse:
        # The agent answered, and only then did the identity change: a second
        # sign-in of the same account is a new cloud identity.
        _sign_in("2")
        return SuggestionAgentResponse(
            suggestion_id="suggestion-1",
            user_id="user-1",
            created_at="2026-03-27T00:00:00Z",
            answer="answer for the old account",
            thinking="thinking",
            suggestion_summary="summary",
            status=StatusType.SUCCESS,
            has_suggestion=True,
            interaction_contract="action_offer",
        )

    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: (_ for _ in ()).throw(
            AssertionError("a replaced route must not reach persistence")
        ),
        process=_process,
    )
    for name, replacement in (
        ("read_local_runtime_db_config", lambda: (db_path, 1_000)),
        ("deps.get_suggestion_repository", AsyncMock(return_value=repository)),
        ("deps.get_suggestion_agent", AsyncMock(return_value=agent)),
    ):
        monkeypatch.setattr(
            f"pantaray_agents.tasks.internal_jobs.suggestion.{name}", replacement
        )

    try:
        with bind_job_route_identity(
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            process_pending_status="enqueued",
            db_path=db_path,
            busy_timeout_ms=1_000,
            requeue_on_change=True,
        ):
            with pytest.raises(DeferredLocalJob):
                await _run_suggestion_job(payload)
    finally:
        reset_logged_out_owner()

    with sqlite3.connect(db_path) as connection:
        job_row = connection.execute(
            "SELECT status, claimed_by FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        process_row = connection.execute(
            "SELECT status FROM processes WHERE process_id = 'process-1'"
        ).fetchone()
    assert job_row == ("queued", None)
    assert process_row == ("enqueued",)
    stored = await repository.get_suggestion(
        user_id="user-1", suggestion_id="suggestion-1"
    )
    assert stored.data is not None
    assert stored.data["status"] == "processing"


def _readable_insight_job(
    monkeypatch: pytest.MonkeyPatch, *, process
) -> tuple[SimpleNamespace, AsyncMock, SourceGate]:
    """A job whose Insight committed a cursor while recording is readable."""
    gate = SourceGate()
    gate.activate(
        SourceBinding(
            user_id="user-1",
            epoch=UUID(int=1),
            policy_revision="policy-1",
            store_id="store-1",
            protocol_version=1,
        )
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.context_source_control",
        SimpleNamespace(gate=gate),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.read_reconsidered_insight",
        lambda **_kwargs: ReconsideredInsight(
            short_term_insight="# Insight\nFixing the parser.",
            reconsideration_reason="The user switched goals.",
            source_cursor="insight-cursor",
        ),
    )
    repository = SimpleNamespace(
        get_suggestion=AsyncMock(
            return_value=RepositoryResult(
                data={
                    "user_id": "user-1",
                    "suggestion_id": "suggestion-1",
                    "status": "processing",
                }
            )
        ),
        save_suggestion=AsyncMock(return_value=SimpleNamespace(error=None)),
        discard_suggestion_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
        finalize_suggestion_start_error_if_processing=AsyncMock(
            return_value=SimpleNamespace(error=None)
        ),
    )
    agent = SimpleNamespace(
        build_persistence_payload=lambda _response: {
            "prompt_name": "suggestion",
            "prompt_version": "1.0",
            "prompt_text": "prompt",
            "response_text": "answer",
            "request_images_count": 0,
            "used_images_count": 0,
        },
        process=process,
    )
    get_agent = AsyncMock(return_value=agent)
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_agent",
        get_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.suggestion.deps.get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    return repository, get_agent, gate


@pytest.mark.asyncio
async def test_the_run_reads_activity_from_the_cursor_its_insight_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, get_agent, gate = _readable_insight_job(
        monkeypatch, process=AsyncMock(return_value=SimpleNamespace(status="success"))
    )

    await _run_suggestion_job(_payload())

    start = get_agent.await_args.kwargs["activity_start"]
    assert start.cursor == "insight-cursor"
    assert start.source == gate.current("user-1")
    repository.save_suggestion.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["during_the_run", "before_publishing"])
async def test_a_revoked_activity_permit_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, revoked: str
) -> None:
    repository, get_agent, gate = _readable_insight_job(monkeypatch, process=None)
    finished = []

    async def _process(_request: object) -> SimpleNamespace:
        gate.revoke("user-1")
        if revoked == "during_the_run":
            # The next model call or step write is where the revocation lands.
            await asyncio.sleep(1)
        finished.append(True)
        return SimpleNamespace(status="success")

    get_agent.return_value.process = _process

    await _run_suggestion_job(_payload())

    assert finished == ([] if revoked == "during_the_run" else [True])
    repository.save_suggestion.assert_not_awaited()
    repository.discard_suggestion_if_processing.assert_awaited_once()
    repository.finalize_suggestion_start_error_if_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_model_request_started_after_revocation_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, get_agent, gate = _readable_insight_job(monkeypatch, process=None)
    sent = []

    async def _process(_request: object) -> SimpleNamespace:
        gate.revoke("user-1")
        # Before the cancel reaches this task, the next model send must not start.
        async with source_http_request("user-1"):
            sent.append(True)
        return SimpleNamespace(status="success")

    get_agent.return_value.process = _process

    await _run_suggestion_job(_payload())

    assert sent == []
    repository.save_suggestion.assert_not_awaited()
    repository.discard_suggestion_if_processing.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "switched",
    [
        "owner_after_the_answer",
        "owner_during_the_run",
        "permit_revoked",
        "permit_revoked_during_the_run",
    ],
)
async def test_a_run_that_lost_its_access_is_discarded_not_requeued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    switched: str,
) -> None:
    """A run that lost its owner or its activity permit must not wait in the queue.

    A plain route change requeues the run (see the replaced-route test). After
    an account switch the previous owner's job is never claimed again, and a
    revoked permit is not restored by a requeue, so the row and the run steps
    are ended here instead.
    """
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'ja', '2026-03-27T00:00:00Z', '2026-03-27T00:00:00Z')",
            [("user-1",), ("user-2",)],
        )

    def _sign_in(user_id: str, session_version: str) -> None:
        import_desktop_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id=user_id,
            desktop_access_token="header.payload.signature",
            expires_at="2099-03-27T01:00:00Z",
            session_version=session_version,
        )

    register_logged_out_owner("local-owner")
    mark_configured()
    _sign_in("user-1", "1")
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        activity_repository=SimpleNamespace(),  # type: ignore[arg-type]
    )
    await repository.create_processing_suggestion_row(
        user_id="user-1", suggestion_id="suggestion-1"
    )
    payload = _payload()
    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_suggestion_enqueue_request(payload),
    )
    claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=LOCAL_SUGGESTION_JOB_TYPE,
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    gate = SourceGate()
    gate.activate(
        SourceBinding(
            user_id="user-1",
            epoch=UUID(int=1),
            policy_revision="policy-1",
            store_id="store-1",
            protocol_version=1,
        )
    )
    job = "pantaray_agents.tasks.internal_jobs.suggestion"
    if switched in ("permit_revoked", "permit_revoked_during_the_run"):
        # The run reads activity under a permit; the same account signs in again.
        monkeypatch.setattr(f"{job}.context_source_control", SimpleNamespace(gate=gate))
        monkeypatch.setattr(
            f"{job}.read_reconsidered_insight",
            lambda **_kwargs: ReconsideredInsight(
                short_term_insight="# Insight",
                reconsideration_reason="The user switched goals.",
                source_cursor="insight-cursor",
            ),
        )
    recording_reads: list[bool] = []

    def _recording_paused(**_kwargs: object) -> bool:
        # The read right before publishing is where this test changes the world.
        recording_reads.append(False)
        if len(recording_reads) == 2 and switched == "permit_revoked":
            gate.revoke("user-1")
            _sign_in("user-1", "2")
        if len(recording_reads) == 2 and switched == "owner_after_the_answer":
            _sign_in("user-2", "2")
        return False

    async def _process(_request: object) -> SuggestionAgentResponse:
        await repository.save_suggestion_run_step(
            suggestion_id="suggestion-1",
            step_number=1,
            step_kind="llm",
            status="success",
            llm_response_text="answer for the previous owner",
        )
        if switched == "owner_during_the_run":
            _sign_in("user-2", "2")
            # The next model call is where a run without an activity permit notices.
            await require_current_route_identity()
        if switched == "permit_revoked_during_the_run":
            # Same owner: only the permit and the route change mid-run.
            gate.revoke("user-1")
            _sign_in("user-1", "2")
            await require_current_route_identity()
        return SuggestionAgentResponse(
            suggestion_id="suggestion-1",
            user_id="user-1",
            created_at="2026-03-27T00:00:00Z",
            answer="answer for the previous owner",
            thinking="thinking",
            status=StatusType.SUCCESS,
            has_suggestion=True,
            interaction_contract="message_only",
        )

    for name, replacement in (
        ("capture_paused_for_user", _recording_paused),
        ("read_local_runtime_db_config", lambda: (db_path, 1_000)),
        ("deps.get_suggestion_repository", AsyncMock(return_value=repository)),
        (
            "deps.get_suggestion_agent",
            AsyncMock(return_value=SimpleNamespace(process=_process)),
        ),
    ):
        monkeypatch.setattr(f"{job}.{name}", replacement)

    try:
        with bind_job_route_identity(
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            process_pending_status="enqueued",
            db_path=db_path,
            busy_timeout_ms=1_000,
            requeue_on_change=True,
        ):
            if switched.endswith("_during_the_run"):
                with pytest.raises(DeferredLocalJob):
                    await _run_suggestion_job(payload)
            else:
                await _run_suggestion_job(payload)
    finally:
        reset_logged_out_owner()

    with sqlite3.connect(db_path) as connection:
        suggestion = connection.execute(
            "SELECT status, answer FROM agent_suggestions WHERE suggestion_id = ?",
            ("suggestion-1",),
        ).fetchone()
        (steps,) = connection.execute(
            "SELECT COUNT(*) FROM agent_suggestion_run_steps"
        ).fetchone()
        (job_status,) = connection.execute(
            "SELECT status FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
    assert (suggestion, steps) == (("canceled", None), 0)
    if not switched.endswith("_during_the_run"):
        # Decided before the route check, so the job is not put back in the queue.
        assert job_status == "running"
