"""Run preparation for the unified Memory agent.

One run owns three catalog nodes. Categories this job already activated in an
earlier attempt are left out of the draft and out of the editable policy, so a
rerun cannot republish them.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Final

from pantaray_agents.agents.artifact_react import ReactToolDefinition
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_ENTRIES_ROOT,
    initial_agent_experience_documents,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_workspace import (
    prepare_artifact_draft,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.document_rendering import (
    render_artifact_manifest,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    derived_experience_ids,
    merged_action_terminals,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryDraftCheckpoint,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.runtime.fact_identity import (
    load_current_fact_identity,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    load_published_memory_categories,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.agent_experience import (
    ActionTurnWindow,
    AgentExperienceActionHistoryTools,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
    MemoryDraftRoute,
    MemoryDraftRouter,
    MemoryDraftToolSession,
    build_artifact_readable_roots,
    build_local_memory_file_tools,
)
from pantaray_agents.local_runtime.tooling.memory_retrieval import (
    MEMORY_EDITOR_RETRIEVAL_POLICY,
    MemoryContextSession,
    MemoryRetrievalSession,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_context import (
    load_workspace_structure_prompt,
)
from pantaray_agents.schema.agent.memory_update import MemoryUpdateContext
from pantaray_agents.tasks.types import (
    MemoryUpdateActionTerminal,
    MemoryUpdateJobPayload,
)
from pantaray_agents.utils.local_time import (
    describe_utc_timestamp,
    local_period,
    local_time_note,
    local_zone_name,
)

FACTS_INDEX_PATH: Final[str] = "facts/index.md"
INSIGHTS_INDEX_PATH: Final[str] = "insights/index.md"


@dataclass(frozen=True, slots=True)
class MemoryRequest:
    """One note the Action's remember tool recorded in a turn this run binds."""

    request_id: str
    action_id: str
    short_step_id: str
    # The memory_note node's source_record_id, which the remember tool keys by
    # the step that recorded it.
    note_record_id: str
    note: str


@dataclass(frozen=True, slots=True)
class PreparedMemoryUpdateRun:
    context: MemoryUpdateContext
    router: MemoryDraftRouter
    fact_id: str
    base_drafts: tuple[tuple[MemorySource, MemoryDraftCheckpoint], ...]
    tool_definitions: tuple[ReactToolDefinition, ...]
    memory_requests: tuple[MemoryRequest, ...]

    @property
    def editable_sources(self) -> tuple[MemorySource, ...]:
        return tuple(route.source for route in self.router.routes)

    @property
    def has_evidence(self) -> bool:
        return bool(
            self.context.short_term_insights
            or self.context.activity_summaries
            or self.context.action_turns
        )

    def base_draft(self, source: MemorySource) -> MemoryDraftCheckpoint:
        """The category's draft before the agent touched it."""

        for base_source, draft in self.base_drafts:
            if base_source == source:
                return draft
        raise KeyError(source)


def derived_first_fact_id(job_id: str) -> str:
    """The Fact node this run creates when the user has none yet.

    A rerun of the same job must reopen the node its predecessor started
    instead of leaving an orphan behind, so the first Fact identity is derived
    from the job rather than minted.
    """

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pantaray://memory-runs/{job_id}/fact"))


def prepare_memory_update_run(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
    workspace_scope: MemoryRunWorkspaceScope,
) -> PreparedMemoryUpdateRun:
    user_id = payload["user_id"]
    job_id = payload["job_id"]
    with open_memory_catalog_connection(
        db_path=runtime.db_path,
        busy_timeout_ms=runtime.busy_timeout_ms,
    ) as connection:
        published = load_published_memory_categories(
            connection=connection, process_id=payload["process_id"]
        )
        fact_id = _resolve_fact_id(connection=connection, payload=payload)
        # An Action deleted from history since the run was queued is skipped.
        terminals = tuple(
            terminal
            for terminal in merged_action_terminals(tuple(payload["action_terminals"]))
            if connection.execute(
                "SELECT 1 FROM agent_actions WHERE user_id = ? AND action_id = ?",
                (user_id, terminal["action_id"]),
            ).fetchone()
        )
        short_term_insights = _render_short_insights(
            connection=connection,
            user_id=user_id,
            insight_ids=tuple(payload["short_insight_ids"]),
        )
        activity_summaries = _render_activity_summaries(
            connection=connection,
            user_id=user_id,
            summary_ids=tuple(payload["summary_ids"]),
        )
        memory_requests = _load_memory_requests(
            connection=connection, user_id=user_id, terminals=terminals
        )
        workspace_scope.create(
            artifact_root=runtime.artifact_root,
            user_id=user_id,
            run_id=job_id,
        )
        routes: list[MemoryDraftRoute] = []
        base_drafts: list[tuple[MemorySource, MemoryDraftCheckpoint]] = []
        # One context epoch spans the run: memory_search populates it once and
        # link_memory resolves handles against it from any category.
        memory_context = MemoryContextSession(user_id=user_id, run_id=job_id)
        with immediate_transaction(connection):
            for source, root, record_id, initial in _category_plan(
                user_id=user_id, fact_id=fact_id
            ):
                if source in published:
                    continue
                draft = prepare_artifact_draft(
                    connection=connection,
                    artifact_root=runtime.artifact_root,
                    user_id=user_id,
                    source=source,
                    source_record_id=record_id,
                    initial_documents=initial,
                )
                routes.append(
                    MemoryDraftRoute(
                        source=source,
                        root=root,
                        session=MemoryDraftToolSession(
                            editable_policy=EditablePathPolicy(
                                _editable_globs(source, root)
                            ),
                            draft=draft,
                            memory_context=memory_context,
                        ),
                    )
                )
                base_drafts.append((source, draft))
    router = MemoryDraftRouter(routes=tuple(routes))
    file_tools = build_local_memory_file_tools(
        editable_policy=router.editable_policy,
        memory_session=router,
        readable_roots=build_artifact_readable_roots(
            db_path=runtime.db_path,
            busy_timeout_ms=runtime.busy_timeout_ms,
            user_id=user_id,
        ),
    )
    retrieval_tools = MemoryRetrievalSession(
        db_path=runtime.db_path,
        busy_timeout_ms=runtime.busy_timeout_ms,
        context=memory_context,
        policy=MEMORY_EDITOR_RETRIEVAL_POLICY,
    ).definitions()
    history_tools = (
        AgentExperienceActionHistoryTools(
            db_path=runtime.db_path,
            busy_timeout_ms=runtime.busy_timeout_ms,
            user_id=user_id,
            turns=tuple(
                ActionTurnWindow(
                    action_id=terminal["action_id"],
                    turn_start_step_number=terminal["turn_start_step_number"],
                    turn_end_step_number=terminal["turn_end_step_number"],
                )
                for terminal in terminals
            ),
        ).definitions()
        if terminals
        else ()
    )
    context = MemoryUpdateContext(
        user_id=user_id,
        run_id=job_id,
        short_term_insights=short_term_insights,
        activity_summaries=activity_summaries,
        action_turns=_render_action_turns(terminals),
        memory_requests=_render_memory_requests(memory_requests),
        memory_request_ids=tuple(request.request_id for request in memory_requests),
        local_time_note=local_time_note(local_zone_name()),
        memory_file_manifest=render_artifact_manifest(router.draft.documents),
        workspace_context_prompt=load_workspace_structure_prompt(
            db_path=runtime.db_path,
            busy_timeout_ms=runtime.busy_timeout_ms,
            user_id=user_id,
        ),
        new_experience_ids=(
            derived_experience_ids(job_id)
            if any(route.source == "agent_experience" for route in router.routes)
            else ()
        ),
        draft_revision=router.draft.draft_revision,
    )
    return PreparedMemoryUpdateRun(
        context=context,
        router=router,
        fact_id=fact_id,
        base_drafts=tuple(base_drafts),
        tool_definitions=(*file_tools, *retrieval_tools, *history_tools),
        memory_requests=memory_requests,
    )


def resolve_memory_run_fact_id(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
) -> str:
    """The Fact node this run owns, before any draft is prepared."""

    with open_memory_catalog_connection(
        db_path=runtime.db_path,
        busy_timeout_ms=runtime.busy_timeout_ms,
    ) as connection:
        return _resolve_fact_id(connection=connection, payload=payload)


def _resolve_fact_id(
    *, connection: sqlite3.Connection, payload: MemoryUpdateJobPayload
) -> str:
    identity = load_current_fact_identity(
        connection=connection, user_id=payload["user_id"]
    )
    if identity is None:
        return derived_first_fact_id(payload["job_id"])
    return identity[0]


def _category_plan(
    *, user_id: str, fact_id: str
) -> tuple[tuple[MemorySource, str, str, tuple[MemoryDocument, ...]], ...]:
    return (
        ("fact", "facts", fact_id, (MemoryDocument(FACTS_INDEX_PATH, ""),)),
        (
            "long_term_insight",
            "insights",
            user_id,
            (MemoryDocument(INSIGHTS_INDEX_PATH, ""),),
        ),
        (
            "agent_experience",
            "agent_experience",
            user_id,
            tuple(
                MemoryDocument(path, content)
                for path, content in initial_agent_experience_documents()
            ),
        ),
    )


def _editable_globs(source: MemorySource, root: str) -> tuple[str, ...]:
    # The Agent Experience index is host-generated; only entry files are editable.
    if source == "agent_experience":
        return (f"{AGENT_EXPERIENCE_ENTRIES_ROOT}/*.md",)
    return (f"{root}/**",)


def _render_action_turns(
    terminals: tuple[MemoryUpdateActionTerminal, ...],
) -> str:
    return "\n".join(
        f"- action_id: {terminal['action_id']} "
        f"(completed at {describe_utc_timestamp(terminal['action_completed_at'])}, "
        f"steps {terminal['turn_start_step_number']}-"
        f"{terminal['turn_end_step_number']}, "
        f"prompt {terminal['action_prompt_name']}@"
        f"{terminal['action_prompt_version']})"
        for terminal in terminals
    )


def _load_memory_requests(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    terminals: tuple[MemoryUpdateActionTerminal, ...],
) -> tuple[MemoryRequest, ...]:
    requests: list[MemoryRequest] = []
    for terminal in terminals:
        rows = connection.execute(
            _REMEMBER_STEPS_SQL,
            (
                user_id,
                terminal["action_id"],
                terminal["turn_start_step_number"],
                terminal["turn_end_step_number"],
            ),
        ).fetchall()
        for row in rows:
            requests.append(
                MemoryRequest(
                    request_id=f"R{len(requests) + 1}",
                    action_id=terminal["action_id"],
                    short_step_id=str(row["short_step_id"]),
                    note_record_id=f"{terminal['action_id']}:{row['step_id']}",
                    note=str(row["note"]),
                )
            )
    return tuple(requests)


def _render_memory_requests(requests: tuple[MemoryRequest, ...]) -> str:
    # The note is the Action's own wording; JSON keeps it one literal string.
    return "\n".join(
        f"- request_id: {request.request_id} (action_id: {request.action_id}, "
        f"step {request.short_step_id}) note: "
        f"{json.dumps(request.note, ensure_ascii=False)}"
        for request in requests
    )


# A step retried under one short_step_id resolves to its latest attempt, as the
# Action history tools resolve it, so only a remember call that finally
# succeeded counts.
_REMEMBER_STEPS_SQL = """
WITH ranked_steps AS (
    SELECT
        step_id, short_step_id, step_number, local_step_number, step_name,
        status, created_at, json_extract(tool_args, '$.args.note') AS note,
        ROW_NUMBER() OVER (
            PARTITION BY short_step_id
            ORDER BY
                completed_at IS NULL ASC,
                completed_at DESC,
                created_at DESC,
                step_id DESC
        ) AS resolution_rank
    FROM agent_action_steps
    WHERE user_id = ? AND action_id = ? AND step_number BETWEEN ? AND ?
      AND short_step_id IS NOT NULL
)
SELECT step_id, short_step_id, note
FROM ranked_steps
WHERE resolution_rank = 1 AND step_name = 'tool::remember' AND status = 'success'
ORDER BY step_number, local_step_number, created_at, short_step_id
"""


def _render_short_insights(
    *, connection: sqlite3.Connection, user_id: str, insight_ids: tuple[str, ...]
) -> str:
    if not insight_ids:
        return ""
    placeholders = ",".join("?" for _ in insight_ids)
    rows = connection.execute(
        f"""
        SELECT insights.insight_id, insights.short_term_insight_data,
               insights.created_at, activity.description,
               activity.period_start, activity.period_end
        FROM agent_insights AS insights
        LEFT JOIN activity_logs AS activity
          ON activity.user_id = insights.user_id
         AND activity.log_id = insights.insight_id AND activity.status = 'success'
        WHERE insights.user_id = ? AND insights.status = 'success'
          AND insights.insight_id IN ({placeholders})
        ORDER BY insights.created_at ASC, insights.insight_id ASC
        """,
        (user_id, *insight_ids),
    ).fetchall()
    return "\n\n".join(
        f"### short-term Insight {row['insight_id']} "
        f"({describe_utc_timestamp(row['created_at'])})\n"
        f"{str(row['short_term_insight_data'] or '').strip()}\n\n"
        + (
            "#### Objective Activity "
            f"({local_period(row['period_start'], row['period_end'])})\n"
            f"{row['description']}"
            if row["description"] is not None
            else "Objective Activity: unavailable for this Insight."
        )
        for row in rows
    )


def _render_activity_summaries(
    *, connection: sqlite3.Connection, user_id: str, summary_ids: tuple[str, ...]
) -> str:
    if not summary_ids:
        return ""
    placeholders = ",".join("?" for _ in summary_ids)
    rows = connection.execute(
        f"""
        SELECT summary_id, summary, period_start, period_end
        FROM activity_summaries
        WHERE user_id = ? AND status = 'success' AND summary_type = '24h'
          AND summary_id IN ({placeholders})
        ORDER BY period_end ASC, summary_id ASC
        """,
        (user_id, *summary_ids),
    ).fetchall()
    return "\n\n".join(
        f"### 24h Activity Summary {row['summary_id']} "
        f"({local_period(row['period_start'], row['period_end'])})\n"
        f"{str(row['summary'] or '').strip()}"
        for row in rows
    )


__all__ = [
    "MemoryRequest",
    "PreparedMemoryUpdateRun",
    "derived_first_fact_id",
    "prepare_memory_update_run",
    "resolve_memory_run_fact_id",
]
