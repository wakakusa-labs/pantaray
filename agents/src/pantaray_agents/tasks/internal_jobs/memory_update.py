from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import pantaray_agents.dependencies as deps
from pantaray_agents.agents.memory_agent import (
    MemoryConsolidationRequest,
    MemoryUpdateAgent,
    MemoryUpdateAgentResult,
)
from pantaray_agents.agents.memory_file_editor.runner import MemoryEditorConversation
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.runtime.help_outcomes import DueOutcome
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    require_current_route_identity,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    MEMORY_UPDATE_CATEGORIES,
    load_published_memory_categories,
    memory_update_is_complete,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
    build_local_memory_file_editor_runtime,
)
from pantaray_agents.schema.agent.memory_update import MemoryUpdateContext
from pantaray_agents.tasks.internal_jobs.memory_update_job import run_memory_update_job
from pantaray_agents.tasks.memory_consolidation import (
    consolidate_memory_files,
    consolidation_targets,
)
from pantaray_agents.tasks.memory_update_completion import (
    complete_memory_update_run,
    publish_memory_update_run,
)
from pantaray_agents.tasks.memory_update_context import (
    PreparedMemoryUpdateRun,
    prepare_memory_update_run,
    resolve_memory_run_fact_id,
)
from pantaray_agents.tasks.types import MemoryUpdateJobPayload
from pantaray_agents.tools.contract import ReactToolDefinition
from pantaray_agents.utils.local_time import describe_utc_timestamp
from pantaray_agents.utils.structured_logging import log_structured_event

logger = logging.getLogger(__name__)


class MemoryUpdateWorkerAgent(Protocol):
    async def update(
        self,
        context: MemoryUpdateContext,
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        tool_results_path: Path,
        db_path: Path,
    ) -> MemoryUpdateAgentResult: ...

    async def consolidate(
        self,
        request: MemoryConsolidationRequest,
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        tool_results_path: Path,
        db_path: Path,
    ) -> None: ...

    async def look_back_on_outcomes(
        self,
        conversation: MemoryEditorConversation,
        due: tuple[DueOutcome, ...],
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        now: str,
    ) -> None: ...


type MemoryUpdateAgentBuilder = Callable[[], Awaitable[MemoryUpdateWorkerAgent]]


async def build_memory_update_agent() -> MemoryUpdateWorkerAgent:
    """Compose the internal agent without expanding the global dependency module."""

    return MemoryUpdateAgent(
        client=deps.get_llm_client(),
        llm_config={},
    )


async def execute_memory_update_job(
    *,
    payload: MemoryUpdateJobPayload,
    runtime: LocalMemoryFileEditorRuntime,
    build_agent: MemoryUpdateAgentBuilder,
) -> None:
    """Run one serialized, idempotent unified Memory update."""

    async def is_complete() -> bool:
        with open_memory_catalog_connection(
            db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
        ) as connection:
            return memory_update_is_complete(
                connection=connection, process_id=payload["process_id"]
            )

    async def run() -> None:
        if _every_category_is_published(runtime=runtime, payload=payload):
            # A crash after the last activation but before the run was closed.
            complete_memory_update_run(
                runtime=runtime, payload=payload, applied_note_record_ids=()
            )
            return
        with MemoryRunWorkspaceScope() as workspace_scope:
            prepared = prepare_memory_update_run(
                runtime=runtime,
                payload=payload,
                workspace_scope=workspace_scope,
            )
            agent = await build_agent()
            result: MemoryUpdateAgentResult | None = None
            # With every input deleted from history, the run publishes nothing.
            if prepared.has_evidence:
                result = await agent.update(
                    prepared.context,
                    tool_definitions=prepared.tool_definitions,
                    tool_results_path=workspace_scope.require_tool_results_path(),
                    db_path=runtime.db_path,
                )
                # Before publishing, so what it finds reaches this run's memory,
                # Agent Experience included.
                if prepared.due:
                    await _look_back_on_outcomes(
                        payload=payload,
                        agent=agent,
                        conversation=result.conversation,
                        prepared=prepared,
                    )
            # Everything above stayed in the run-only workspace; this is where
            # the edits become the owner's memory, so the route they were made
            # on has to still be current.
            await require_current_route_identity()
            publish_memory_update_run(
                runtime=runtime,
                payload=payload,
                prepared=prepared,
                prompt_name=MemoryUpdateAgent.PROMPT_NAME,
                prompt_version=MemoryUpdateAgent.PROMPT_VERSION,
                applied_memory_request_ids=(
                    () if result is None else result.applied_memory_request_ids
                ),
            )
        # After completion: a crash from here leaves the update complete, and
        # the next update that changes an oversized file consolidates it.
        await consolidate_memory_files(
            runtime=runtime,
            payload=payload,
            fact_id=prepared.fact_id,
            targets=consolidation_targets(prepared, runtime=runtime),
            agent=agent,
        )

    await run_memory_update_job(
        runtime=runtime,
        user_id=payload["user_id"],
        job_id=payload["job_id"],
        process_id=payload["process_id"],
        job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
        process_pending_status="enqueued",
        resolve_sources=lambda: _memory_update_sources(
            runtime=runtime, payload=payload
        ),
        is_complete=is_complete,
        run=run,
    )


async def _look_back_on_outcomes(
    *,
    payload: MemoryUpdateJobPayload,
    agent: MemoryUpdateWorkerAgent,
    conversation: MemoryEditorConversation,
    prepared: PreparedMemoryUpdateRun,
) -> None:
    """Continue the accepted update into what the due items changed.

    A failure is logged and the drafts go back to where the accepted update
    left them, so the update still publishes. A look-back stopped midway can
    leave edits each tool accepted but the tree as a whole fails, such as a new
    Agent Experience entry whose superseded entry is not deleted yet.
    """

    accepted = tuple(route.session.draft for route in prepared.router.routes)
    try:
        await agent.look_back_on_outcomes(
            conversation,
            prepared.due,
            tool_definitions=prepared.tool_definitions,
            now=describe_utc_timestamp(now_utc_iso()),
        )
    # Every failure the conversation and its model sends declare is a
    # RuntimeError (output refused, turns exhausted, context full, upstream
    # error). A deferred job is a BaseException and still ends the run.
    except RuntimeError as exc:
        for route, draft in zip(prepared.router.routes, accepted, strict=True):
            route.session.draft = draft
        log_structured_event(
            logger,
            level="error",
            evt="MEMORY_OUTCOMES_LOOK_BACK_FAILED",
            exception=exc,
            component="tasks.memory_update",
            user_id=payload["user_id"],
            job_id=payload["job_id"],
            error_class=exc.__class__.__name__,
        )


def _memory_update_sources(
    *, runtime: LocalMemoryFileEditorRuntime, payload: MemoryUpdateJobPayload
) -> tuple[tuple[MemorySource, str], ...]:
    user_id = payload["user_id"]
    return (
        ("fact", resolve_memory_run_fact_id(runtime=runtime, payload=payload)),
        ("long_term_insight", user_id),
        ("agent_experience", user_id),
    )


def _every_category_is_published(
    *, runtime: LocalMemoryFileEditorRuntime, payload: MemoryUpdateJobPayload
) -> bool:
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
    ) as connection:
        published = load_published_memory_categories(
            connection=connection, process_id=payload["process_id"]
        )
    return set(MEMORY_UPDATE_CATEGORIES) <= published


def run_memory_update(payload: MemoryUpdateJobPayload) -> None:
    asyncio.run(
        execute_memory_update_job(
            payload=payload,
            runtime=build_local_memory_file_editor_runtime(),
            build_agent=build_memory_update_agent,
        )
    )


__all__ = [
    "MemoryUpdateAgentBuilder",
    "MemoryUpdateWorkerAgent",
    "build_memory_update_agent",
    "execute_memory_update_job",
    "run_memory_update",
]
