"""Carry on a finished subagent's work in a new child of the active parent.

A finished child's rows never change. When the parent messages a child that
has stopped or finished, a new child takes its task, model, context references
and resource claims through the ordinary spawn path, under the active parent,
and starts from the finished child's conversation with the message added.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from .action_subagent_ownership import owned_child_sql
from .action_subagent_spawn import (
    ActionSubagentContinuation,
    ActionSubagentSpawnRequest,
    ActionSubagentSpawnResult,
    ExternalSpawnResourceClaim,
    WorkspaceSpawnResourceClaim,
    spawn_action_subagent,
)
from .job_payload_models import parse_action_subagent_job_payload_json
from .job_types import LOCAL_ACTION_SUBAGENT_JOB_TYPE


class ActionSubagentContinueInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionSubagentContinueRequest:
    user_id: str
    action_id: str
    parent_process_id: str
    parent_job_id: str
    root_execution_session_id: str
    manifest_id: str
    origin: ActionToolCallOrigin
    action_context: str
    current_cwd: Path
    child_process_id: str
    message_id: str
    content: str
    continued_at: str


def continue_action_subagent(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: ActionSubagentContinueRequest,
) -> ActionSubagentSpawnResult:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            f"""
            SELECT payload.payload_json
            FROM processes AS child
            JOIN jobs AS job ON job.process_id=child.process_id
              AND job.job_type=? AND job.logical_key=job.job_id
            JOIN job_payloads AS payload ON payload.job_id=job.job_id
            WHERE child.process_id=? AND child.user_id=? AND child.action_id=?
              AND child.kind='action_subagent'
              AND child.status IN ('completed','failed','canceled')
              AND {owned_child_sql("?")}
            """,
            (
                LOCAL_ACTION_SUBAGENT_JOB_TYPE,
                request.child_process_id,
                request.user_id,
                request.action_id,
                request.parent_process_id,
            ),
        ).fetchone()
        if row is None:
            raise ActionSubagentContinueInputError(
                "child_process_id is not a finished subagent of this Action"
            )
        claims = connection.execute(
            "SELECT resource_kind,root_identity,normalized_key "
            "FROM action_subagent_resource_claims WHERE child_process_id=? "
            "ORDER BY rowid",
            (request.child_process_id,),
        ).fetchall()
    finished = parse_action_subagent_job_payload_json(str(row[0]))
    model_selector = next(
        (
            setting.selector
            for setting in SUBAGENT_MODEL_SETTINGS
            if setting.profile_id == finished["inference_profile_id"]
        ),
        None,
    )
    if model_selector is None:
        raise ActionSubagentContinueInputError(
            "the finished subagent's model is no longer configured"
        )
    return spawn_action_subagent(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=ActionSubagentSpawnRequest(
            user_id=request.user_id,
            action_id=request.action_id,
            parent_process_id=request.parent_process_id,
            parent_job_id=request.parent_job_id,
            root_execution_session_id=request.root_execution_session_id,
            manifest_id=request.manifest_id,
            origin=request.origin,
            model_selector=model_selector,
            action_context=request.action_context,
            task=finished["task"],
            context_refs=tuple(finished["context_refs"]),
            # A workspace claim is stored as its resolved absolute path, which the
            # new claim resolves to the same resource in the current manifest.
            resource_claims=tuple(
                WorkspaceSpawnResourceClaim(str(key), request.current_cwd)
                if kind == "workspace_path"
                else ExternalSpawnResourceClaim(str(root), str(key))
                for kind, root, key in claims
            ),
            spawned_at=request.continued_at,
            continues=ActionSubagentContinuation(
                child_process_id=request.child_process_id,
                message_id=request.message_id,
                content=request.content,
            ),
        ),
    )


__all__ = [
    "ActionSubagentContinueInputError",
    "ActionSubagentContinueRequest",
    "continue_action_subagent",
]
