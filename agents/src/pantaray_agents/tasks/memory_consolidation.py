"""Consolidation of an oversized Fact or long-term Insight file, or of Agent
Experience whose lessons' `Applies when` text has grown too long.

A memory update changes only what its evidence touches, so a file that keeps
receiving evidence grows into a log of events, and Agent Experience only ever
gains lessons. Once an update has published, each file it changed that is now
over the size limit is rewritten on its own, and Agent Experience it changed
merges its overlapping lessons, still inside the update's job: the job's user
lock and the one active update per user keep it from racing another update, and
its publication is bound to the job's Memory run like the update's own. A
consolidation that fails, or does not shrink its file or its lessons'
`Applies when` text, publishes nothing and leaves the update's revision current.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from pantaray_agents.agents.memory_agent import (
    MEMORY_CONSOLIDATION_PROMPT_NAME,
    MEMORY_CONSOLIDATION_PROMPT_VERSION,
    MemoryConsolidationRequest,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_ENTRIES_ROOT,
    AGENT_EXPERIENCE_INDEX_PATH,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_delta import (
    applies_when_chars,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_workspace import (
    prepare_artifact_draft,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.document_rendering import (
    TODO_DOCUMENT_PATH,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryLinkValidationError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDraftCheckpoint,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    require_current_route_identity,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
    MemoryDraftToolSession,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.reference_tags import (
    misused_reference_ids,
    restore_reference_tags,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.tool_support import (
    args_object,
    required_string,
    response_schema,
    success,
    tool,
)
from pantaray_agents.tasks.experience_consolidation import (
    entry_written_dates,
    experience_entries_text,
    last_consolidated_applies_when_chars,
    merge_experiences_tool,
)
from pantaray_agents.tasks.memory_update_completion import publish_memory_draft
from pantaray_agents.tasks.memory_update_context import PreparedMemoryUpdateRun
from pantaray_agents.tasks.types import MemoryUpdateJobPayload
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_agents.tools.memory.retrieval import MemoryContextSession
from pantaray_agents.utils.structured_logging import log_structured_event

logger = logging.getLogger(__name__)

# Characters as Python counts them (code points). letta-code caps one memory file
# at 20,000; past that, every update that edits the file reads all of it and its
# events pile up faster than any one update removes them.
MEMORY_FILE_CONSOLIDATION_THRESHOLD_CHARS: Final[int] = 20_000
# Characters of every lesson's `Applies when`, which Action is to receive
# whole when it starts: about 80 lessons at the ~125 characters one averaged
# when 73 lessons totalled 9,100.
EXPERIENCE_CONSOLIDATION_THRESHOLD_CHARS: Final[int] = 10_000
# Lessons on separate situations stay apart, so a consolidation can leave the
# total over the threshold; the next one waits for about 16 new lessons
# (~2,000 characters) instead of rerunning on each update's one or two.
EXPERIENCE_CONSOLIDATION_MIN_GROWTH_CHARS: Final[int] = 2_000
EXPERIENCE_CONSOLIDATION_PROMPT_NAME: Final[str] = "memory_experience_consolidation"
REWRITE_FILE_TOOL_NAME: Final[str] = "rewrite_file"
# The TODO list is pruned whole by every update.
_CONSOLIDATED_SOURCES: Final[frozenset[MemorySource]] = frozenset(
    {"fact", "long_term_insight"}
)
_COMPONENT: Final[str] = "tasks.memory_consolidation"


class MemoryConsolidationRejectedError(RuntimeError):
    """The run ended without a rewrite that shrinks the file."""


class MemoryConsolidationAgent(Protocol):
    async def consolidate(
        self,
        request: MemoryConsolidationRequest,
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        tool_result_directory_fd: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryConsolidationTarget:
    source: MemorySource
    path: str


def consolidation_targets(
    prepared: PreparedMemoryUpdateRun, *, runtime: LocalMemoryFileEditorRuntime
) -> tuple[MemoryConsolidationTarget, ...]:
    """Files this run changed that are now over the limit.

    A file the run left alone is not reconsidered: it only grows when an update
    changes it, and one that stays over the limit after a consolidation would
    otherwise be rewritten again by every update.
    """

    targets: list[MemoryConsolidationTarget] = []
    for route in prepared.router.routes:
        if route.source == "agent_experience":
            draft = route.session.draft
            if draft.draft_revision != prepared.base_draft(
                route.source
            ).draft_revision and experience_consolidation_is_due(
                runtime=runtime,
                user_id=draft.user_id,
                chars=applies_when_chars(draft.documents),
            ):
                targets.append(
                    MemoryConsolidationTarget(
                        source=route.source, path=AGENT_EXPERIENCE_INDEX_PATH
                    )
                )
            continue
        if route.source not in _CONSOLIDATED_SOURCES:
            continue
        before = {
            document.source_path: document.content
            for document in prepared.base_draft(route.source).documents
        }
        targets.extend(
            MemoryConsolidationTarget(source=route.source, path=document.source_path)
            for document in route.session.draft.documents
            if document.source_path != TODO_DOCUMENT_PATH
            and len(document.content) > MEMORY_FILE_CONSOLIDATION_THRESHOLD_CHARS
            and before.get(document.source_path) != document.content
        )
    return tuple(targets)


def experience_consolidation_is_due(
    *, runtime: LocalMemoryFileEditorRuntime, user_id: str, chars: int
) -> bool:
    """Whether lessons whose `Applies when` total `chars` are due a merge."""

    if chars <= EXPERIENCE_CONSOLIDATION_THRESHOLD_CHARS:
        return False
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
    ) as connection:
        last = last_consolidated_applies_when_chars(
            connection=connection, user_id=user_id
        )
    return last is None or chars - last >= EXPERIENCE_CONSOLIDATION_MIN_GROWTH_CHARS


async def consolidate_memory_files(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
    fact_id: str,
    targets: tuple[MemoryConsolidationTarget, ...],
    agent: MemoryConsolidationAgent,
) -> None:
    for target in targets:
        try:
            await _consolidate_file(
                runtime=runtime,
                payload=payload,
                fact_id=fact_id,
                target=target,
                agent=agent,
            )
        # The update this follows is already complete, so its job would end as
        # a success over any error raised here: this is the boundary that
        # reports it. The file keeps the update's revision, and the next update
        # that changes the file tries again. A deferred job is a BaseException
        # and still ends the run.
        except Exception as exc:
            log_structured_event(
                logger,
                level="error",
                evt="MEMORY_CONSOLIDATION_FAILED",
                exception=exc,
                component=_COMPONENT,
                user_id=payload["user_id"],
                job_id=payload["job_id"],
                source=target.source,
                path=target.path,
                error_class=exc.__class__.__name__,
            )


async def _consolidate_file(
    *,
    runtime: LocalMemoryFileEditorRuntime,
    payload: MemoryUpdateJobPayload,
    fact_id: str,
    target: MemoryConsolidationTarget,
    agent: MemoryConsolidationAgent,
) -> None:
    user_id = payload["user_id"]
    run_id = payload["job_id"]
    experience = target.source == "agent_experience"
    with MemoryRunWorkspaceScope() as workspace_scope:
        workspace_scope.create(
            artifact_root=runtime.artifact_root, user_id=user_id, run_id=run_id
        )
        with open_memory_catalog_connection(
            db_path=runtime.db_path, busy_timeout_ms=runtime.busy_timeout_ms
        ) as connection:
            with immediate_transaction(connection):
                # The update just published this category, so its node is active
                # and the draft opens on that revision.
                base = prepare_artifact_draft(
                    connection=connection,
                    artifact_root=runtime.artifact_root,
                    user_id=user_id,
                    source=target.source,
                    source_record_id=fact_id if target.source == "fact" else user_id,
                    initial_documents=(),
                )
                written = (
                    entry_written_dates(connection=connection, draft=base)
                    if experience
                    else {}
                )
        session = MemoryDraftToolSession(
            editable_policy=EditablePathPolicy(
                (f"{AGENT_EXPERIENCE_ENTRIES_ROOT}/*.md",)
                if experience
                else (target.path,)
            ),
            draft=base,
            memory_context=MemoryContextSession(user_id=user_id, run_id=run_id),
        )
        today = datetime.now().astimezone().date().isoformat()
        if experience:
            request = MemoryConsolidationRequest(
                run_id=run_id,
                prompt_name=EXPERIENCE_CONSOLIDATION_PROMPT_NAME,
                path=target.path,
                content=experience_entries_text(base, written),
                limit_chars=EXPERIENCE_CONSOLIDATION_THRESHOLD_CHARS,
                today=today,
            )
            tools = (merge_experiences_tool(session=session, run_id=run_id),)
            before_chars = applies_when_chars(base.documents)
        else:
            before = _content(base, target.path)
            before_chars = len(before)
            request = MemoryConsolidationRequest(
                run_id=run_id,
                prompt_name=MEMORY_CONSOLIDATION_PROMPT_NAME,
                path=target.path,
                content=before,
                limit_chars=MEMORY_FILE_CONSOLIDATION_THRESHOLD_CHARS,
                today=today,
            )
            tools = (rewrite_file_tool(session=session, path=target.path),)
        await agent.consolidate(
            request,
            tool_definitions=tools,
            tool_result_directory_fd=workspace_scope.require_tool_results_fd(),
        )
    after_chars = (
        applies_when_chars(session.draft.documents)
        if experience
        else len(_content(session.draft, target.path))
    )
    # `completed` after a refused or missing rewrite leaves the file as it was.
    if after_chars >= before_chars:
        raise MemoryConsolidationRejectedError(
            f"consolidation left {after_chars} of {before_chars} characters"
        )
    await require_current_route_identity()
    publish_memory_draft(
        runtime=runtime,
        payload=payload,
        source=target.source,
        fact_id=fact_id,
        base=base,
        draft=session.draft,
        prompt_name=MEMORY_CONSOLIDATION_PROMPT_NAME,
        prompt_version=MEMORY_CONSOLIDATION_PROMPT_VERSION,
    )
    log_structured_event(
        logger,
        level="info",
        evt="MEMORY_FILE_CONSOLIDATED",
        component=_COMPONENT,
        user_id=user_id,
        job_id=run_id,
        source=target.source,
        path=target.path,
        chars_before=before_chars,
        chars_after=after_chars,
    )


def rewrite_file_tool(
    *, session: MemoryDraftToolSession, path: str
) -> ReactToolDefinition:
    """Replace the one file under consolidation with a whole new text."""

    async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
        text = required_string(args_object(call.tool_args), "text")
        current = _content(session.draft, path)
        content = restore_reference_tags(before=current, after=text)
        message = misused_reference_ids(before=current, after=content)
        if message is None:
            try:
                revision = session.replace_document(
                    path=path,
                    content=content,
                    expected_text=current,
                    expected_revision=session.draft.draft_revision,
                )
                return success(
                    call.tool_name,
                    {
                        "status": "success",
                        "path": path,
                        "characters": len(_content(session.draft, path)),
                        "draft_revision": revision,
                    },
                )
            except MemoryLinkValidationError as exc:
                message = str(exc)
        return tool_error_response(
            tool_name=call.tool_name,
            error_code="MEMORY_LINK_INVALID",
            message=f"The file was not changed: {message}",
            details={
                "path": path,
                "retry_advice": (
                    "Fix the named refs and send the whole file again; every "
                    "other kept ref stays as it is."
                ),
            },
        )

    return tool(
        name=REWRITE_FILE_TOOL_NAME,
        description=(
            f"Replace the whole of `{path}` with `text`. Keep a ref by writing "
            "its tag, with or without its note; its stored note is kept. A ref "
            "whose tag is left out is unlinked. Refs cannot be added."
        ),
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        response_schema=response_schema(
            required=("status", "path", "characters", "draft_revision"),
            properties={
                "path": {"type": "string"},
                "characters": {"type": "integer"},
                "draft_revision": {"type": "string", "pattern": "^sha256:"},
            },
        ),
        execute=execute,
    )


def _content(draft: MemoryDraftCheckpoint, path: str) -> str:
    return next(
        document.content for document in draft.documents if document.source_path == path
    )


__all__ = [
    "EXPERIENCE_CONSOLIDATION_MIN_GROWTH_CHARS",
    "EXPERIENCE_CONSOLIDATION_PROMPT_NAME",
    "EXPERIENCE_CONSOLIDATION_THRESHOLD_CHARS",
    "MEMORY_FILE_CONSOLIDATION_THRESHOLD_CHARS",
    "REWRITE_FILE_TOOL_NAME",
    "MemoryConsolidationAgent",
    "MemoryConsolidationRejectedError",
    "MemoryConsolidationTarget",
    "consolidate_memory_files",
    "consolidation_targets",
    "experience_consolidation_is_due",
    "rewrite_file_tool",
]
