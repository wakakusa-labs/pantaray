"""Publication side of one unified Memory run.

Each memory category the agent actually changed becomes its own artifact
revision, activated in its own transaction and recorded on the run's process, so
a crash between two categories cannot republish the one that already landed.
A category the agent left untouched publishes nothing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable

from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.memory_catalog.agent_experience import (
    AgentExperiencePublication,
    publish_agent_experience_artifact,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_delta import (
    rebuild_agent_experience_index,
    validate_agent_experience_tree_change,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_domain_publication import (
    FactArtifactPublication,
    LongTermInsightArtifactPublication,
    publish_fact_artifact,
    publish_long_term_insight_artifact,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_workspace import (
    drop_links_to_deleted_targets,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.document_rendering import (
    TODO_DOCUMENT_PATH,
    render_artifact_documents,
)
from pantaray_agents.local_runtime.memory_catalog.draft import replace_draft_documents
from pantaray_agents.local_runtime.memory_catalog.lifecycle import archive_memory
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    MemoryRunBinding,
    derived_experience_ids,
    memory_run_binding_from_payload,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDraftCheckpoint,
    MemorySource,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    append_memory_update_completed_in_connection,
    load_published_memory_categories,
    memory_update_is_complete,
)
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    enqueue_suggestion_for_insight,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.tasks.memory_update_context import PreparedMemoryUpdateRun
from pantaray_agents.tasks.types import MemoryUpdateJobPayload

logger = logging.getLogger(__name__)

type ProfileBriefBuilder = Callable[[MemorySource, str], Awaitable[str]]


async def publish_memory_update_run(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
    prepared: PreparedMemoryUpdateRun,
    prompt_name: str,
    prompt_version: str,
    build_profile_brief: ProfileBriefBuilder,
    applied_memory_request_ids: tuple[str, ...],
) -> None:
    binding = memory_run_binding_from_payload(payload)
    for route in prepared.router.routes:
        base = prepared.base_draft(route.source)
        draft = route.session.draft
        if draft.draft_revision == base.draft_revision:
            _discard_unpublished_node(runtime=runtime, draft=draft)
            continue
        draft = _without_deleted_targets(runtime=runtime, draft=draft)
        if route.source == "agent_experience":
            _publish_agent_experience(
                runtime=runtime,
                binding=binding,
                base=base,
                draft=draft,
            )
        elif route.source == "fact":
            publish_fact_artifact(
                db_path=runtime.db_path,
                busy_timeout_ms=runtime.busy_timeout_ms,
                artifact_root=runtime.artifact_root,
                publication=FactArtifactPublication(
                    user_id=binding.user_id,
                    fact_id=prepared.fact_id,
                    memory_run=binding,
                    draft=draft,
                    profile_brief=await build_profile_brief(
                        "fact", render_artifact_documents(draft.documents)
                    ),
                    prompt_name=prompt_name,
                    prompt_version=prompt_version,
                    source_insight_ids=tuple(payload["short_insight_ids"]),
                    created_at=payload["enqueued_at"],
                ),
            )
        else:
            long_term_text = render_artifact_documents(
                tuple(
                    document
                    for document in draft.documents
                    if document.source_path != TODO_DOCUMENT_PATH
                    and document.content.strip()
                )
            )
            publish_long_term_insight_artifact(
                db_path=runtime.db_path,
                busy_timeout_ms=runtime.busy_timeout_ms,
                artifact_root=runtime.artifact_root,
                publication=LongTermInsightArtifactPublication(
                    user_id=binding.user_id,
                    memory_run=binding,
                    source_run_id=binding.job_id,
                    draft=draft,
                    profile_brief=(
                        await build_profile_brief("long_term_insight", long_term_text)
                        if long_term_text
                        else "No durable long-term insights recorded."
                    ),
                    prompt_name=prompt_name,
                    prompt_version=prompt_version,
                ),
            )
    complete_memory_update_run(
        runtime=runtime,
        payload=payload,
        # Only a note this run rendered can be marked applied.
        applied_note_record_ids=tuple(
            request.note_record_id
            for request in prepared.memory_requests
            if request.request_id in applied_memory_request_ids
        ),
    )


def _publish_agent_experience(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    binding: MemoryRunBinding,
    base: MemoryDraftCheckpoint,
    draft: MemoryDraftCheckpoint,
) -> None:
    # The index is host-owned, so it is regenerated from the entries the agent
    # wrote before the tree is judged and published.
    indexed = replace_draft_documents(
        draft=draft, documents=rebuild_agent_experience_index(draft.documents)
    )
    validate_agent_experience_tree_change(
        base_documents=base.documents,
        published_documents=indexed.documents,
        allowed_new_experience_ids=derived_experience_ids(binding.job_id),
    )
    publish_agent_experience_artifact(
        db_path=runtime.db_path,
        busy_timeout_ms=runtime.busy_timeout_ms,
        artifact_root=runtime.artifact_root,
        publication=AgentExperiencePublication(binding=binding, draft=indexed),
    )


def _without_deleted_targets(
    *, runtime: LocalMemoryFileEditorRuntime, draft: MemoryDraftCheckpoint
) -> MemoryDraftCheckpoint:
    """A conversation deleted mid-run takes the refs into its copies with it."""

    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
    ) as connection:
        published, dropped = drop_links_to_deleted_targets(
            connection=connection, draft=draft
        )
    if dropped:
        logger.info("Memory update dropped %d refs to deleted memory", dropped)
    return published


def _discard_unpublished_node(
    *, runtime: LocalMemoryFileEditorRuntime, draft: MemoryDraftCheckpoint
) -> None:
    """Leave no preparing node behind for a category this run did not change."""

    if draft.base_revision_id is not None:
        return
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            connection.execute(
                """
                DELETE FROM memory_nodes
                WHERE user_id = ? AND node_id = ? AND lifecycle = 'preparing'
                  AND current_revision_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_revision_intents AS intents
                      WHERE intents.user_id = memory_nodes.user_id
                        AND intents.node_id = memory_nodes.node_id
                  )
                """,
                (draft.user_id, draft.owner_node_id),
            )


def complete_memory_update_run(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
    applied_note_record_ids: tuple[str, ...],
) -> None:
    """Close the run with the categories it published across every attempt.

    A note the run applied is now part of memory, so memory_search stops
    returning it; conversation deletion still removes it with its Action.
    """

    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            if memory_update_is_complete(
                connection=connection, process_id=payload["process_id"]
            ):
                return
            completed_at = now_utc_iso()
            append_memory_update_completed_in_connection(
                connection=connection,
                process_id=payload["process_id"],
                published_sources=tuple(
                    sorted(
                        load_published_memory_categories(
                            connection=connection, process_id=payload["process_id"]
                        )
                    )
                ),
                created_at=completed_at,
            )
            _archive_applied_notes(
                connection=connection,
                user_id=payload["user_id"],
                note_record_ids=applied_note_record_ids,
            )
            if payload["short_insight_ids"] and not store.is_capture_paused(
                connection, payload["user_id"]
            ):
                enqueue_suggestion_for_insight(
                    connection=connection,
                    user_id=payload["user_id"],
                    insight_id=payload["short_insight_ids"][-1],
                    now=completed_at,
                )


def _archive_applied_notes(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    note_record_ids: tuple[str, ...],
) -> None:
    for (node_id,) in connection.execute(
        f"""
        SELECT node_id FROM memory_nodes
        WHERE user_id = ? AND source_type = 'memory_note' AND lifecycle = 'active'
          AND source_record_id IN ({", ".join("?" for _ in note_record_ids)})
        """,
        (user_id, *note_record_ids),
    ).fetchall():
        archive_memory(connection=connection, user_id=user_id, node_id=node_id)


__all__ = [
    "ProfileBriefBuilder",
    "complete_memory_update_run",
    "publish_memory_update_run",
]
