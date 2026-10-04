from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from ..storage.transactions import immediate_transaction
from ..tooling.action_subagent_resource_claims import (
    ActionSubagentResourceClaim,
    ExternalResourceClaim,
    WorkspacePathResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from .action_subagent_queue import enqueue_action_subagent_job_in_connection
from .job_payload_builder import build_action_subagent_job_payload
from .job_payload_models import serialize_action_subagent_job_payload
from .job_types import LOCAL_ACTION_JOB_TYPE, LOCAL_ACTION_SUBAGENT_JOB_TYPE


class ActionSubagentSpawnRequestError(ValueError):
    """The internal spawn request is incomplete."""


class ActionSubagentSpawnAuthorityError(RuntimeError):
    """The requested parent runtime no longer owns spawn authority."""


class ActionSubagentSpawnConflictError(RuntimeError):
    """A durable spawn identity is already bound to different content."""


@dataclass(frozen=True, slots=True)
class WorkspaceSpawnResourceClaim:
    raw_path: str
    current_cwd: Path


@dataclass(frozen=True, slots=True)
class ExternalSpawnResourceClaim:
    root_identity: str
    normalized_key: str


type ActionSubagentSpawnResourceClaim = (
    WorkspaceSpawnResourceClaim | ExternalSpawnResourceClaim
)


@dataclass(frozen=True, slots=True)
class ActionSubagentSpawnRequest:
    user_id: str
    action_id: str
    parent_process_id: str
    parent_job_id: str
    root_execution_session_id: str
    manifest_id: str
    origin: ActionToolCallOrigin
    model_selector: str
    action_context: str
    task: str
    context_refs: tuple[str, ...]
    resource_claims: tuple[ActionSubagentSpawnResourceClaim, ...]
    spawned_at: str

    @property
    def logical_request_id(self) -> str:
        return json.dumps(
            [self.origin.llm_step_id, self.origin.call_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class ActionSubagentSpawnResult:
    child_process_id: str
    job_id: str
    inserted_new: bool


def spawn_action_subagent(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: ActionSubagentSpawnRequest,
) -> ActionSubagentSpawnResult:
    """Atomically bind one adopted parent call to a child job and its claims."""

    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    _validate_request_identity(request)
    inference_profile_id = _inference_profile_id(request.model_selector)
    child_process_id = _durable_id("child", request)
    job_id = _durable_id("job", request)
    claims = _build_resource_claims(request, child_process_id=child_process_id)
    payload = _build_validated_action_subagent_job_payload(
        ActionSubagentJobPayload(
            job_id=job_id,
            process_id=child_process_id,
            user_id=request.user_id,
            action_id=request.action_id,
            parent_process_id=request.parent_process_id,
            inference_profile_id=inference_profile_id,
            action_context=request.action_context,
            task=request.task,
            context_refs=list(request.context_refs),
            resource_claim_ids=[claim.claim_id for claim in claims],
        )
    )

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            current_cwd = _require_parent_spawn_authority(connection, request)
            _require_workspace_claim_cwds(request, current_cwd=current_cwd)
            if _is_exact_replay(
                connection,
                request=request,
                child_process_id=child_process_id,
                job_id=job_id,
                payload=payload,
                claim_ids=tuple(claim.claim_id for claim in claims),
            ):
                return ActionSubagentSpawnResult(
                    child_process_id=child_process_id,
                    job_id=job_id,
                    inserted_new=False,
                )
            enqueue_action_subagent_job_in_connection(
                connection=connection,
                payload=payload,
                scheduled_at=request.spawned_at,
            )
            acquire_action_subagent_resource_claims_in_connection(
                connection,
                user_id=request.user_id,
                action_id=request.action_id,
                parent_process_id=request.parent_process_id,
                child_process_id=child_process_id,
                acquired_at=request.spawned_at,
                claims=claims,
            )
    return ActionSubagentSpawnResult(
        child_process_id=child_process_id,
        job_id=job_id,
        inserted_new=True,
    )


def _build_validated_action_subagent_job_payload(
    payload: ActionSubagentJobPayload,
) -> ActionSubagentJobPayload:
    try:
        return build_action_subagent_job_payload(payload)
    except MigrationError as exc:
        raise ActionSubagentSpawnRequestError(str(exc)) from exc


def _validate_request_identity(request: ActionSubagentSpawnRequest) -> None:
    values = {
        "user_id": request.user_id,
        "action_id": request.action_id,
        "parent_process_id": request.parent_process_id,
        "parent_job_id": request.parent_job_id,
        "root_execution_session_id": request.root_execution_session_id,
        "manifest_id": request.manifest_id,
        "model_selector": request.model_selector,
        "action_context": request.action_context,
        "task": request.task,
        "spawned_at": request.spawned_at,
    }
    for field, value in values.items():
        if not value.strip():
            raise ActionSubagentSpawnRequestError(f"{field} must not be blank")


def _inference_profile_id(model_selector: str) -> str:
    setting = next(
        (
            candidate
            for candidate in SUBAGENT_MODEL_SETTINGS
            if candidate.selector == model_selector
        ),
        None,
    )
    if setting is None:
        raise ActionSubagentSpawnRequestError(
            "model_selector must name a configured subagent model"
        )
    return setting.profile_id


def _durable_id(kind: str, request: ActionSubagentSpawnRequest) -> str:
    identity = json.dumps(
        [
            "pantaray.action-subagent.spawn.v1",
            kind,
            request.user_id,
            request.action_id,
            request.parent_process_id,
            request.logical_request_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def _build_resource_claims(
    request: ActionSubagentSpawnRequest,
    *,
    child_process_id: str,
) -> tuple[ActionSubagentResourceClaim, ...]:
    claims: list[ActionSubagentResourceClaim] = []
    for ordinal, requested in enumerate(request.resource_claims):
        claim_content = _claim_request_content(request, requested)
        claim_id = _claim_id(
            request,
            child_process_id=child_process_id,
            ordinal=ordinal,
            claim_content=claim_content,
        )
        if isinstance(requested, WorkspaceSpawnResourceClaim):
            claims.append(
                WorkspacePathResourceClaim(
                    claim_id=claim_id,
                    manifest_id=request.manifest_id,
                    raw_path=requested.raw_path,
                    current_cwd=requested.current_cwd,
                )
            )
        else:
            claims.append(
                ExternalResourceClaim(
                    claim_id=claim_id,
                    root_identity=requested.root_identity,
                    normalized_key=requested.normalized_key,
                )
            )
    return tuple(claims)


def _claim_request_content(
    request: ActionSubagentSpawnRequest,
    claim: ActionSubagentSpawnResourceClaim,
) -> str:
    if isinstance(claim, WorkspaceSpawnResourceClaim):
        content: dict[str, str] = {
            "resource_kind": "workspace_path",
            "root_identity": request.manifest_id,
            "raw_path": claim.raw_path,
            "current_cwd": claim.current_cwd.as_posix(),
        }
    else:
        content = {
            "resource_kind": "external_resource",
            "root_identity": claim.root_identity,
            "normalized_key": claim.normalized_key,
        }
    return json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _claim_id(
    request: ActionSubagentSpawnRequest,
    *,
    child_process_id: str,
    ordinal: int,
    claim_content: str,
) -> str:
    identity = json.dumps(
        [
            "pantaray.action-subagent.claim.v1",
            child_process_id,
            request.logical_request_id,
            ordinal,
            claim_content,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def _require_parent_spawn_authority(
    connection: sqlite3.Connection,
    request: ActionSubagentSpawnRequest,
) -> Path:
    row = connection.execute(
        """
        SELECT session.cwd_path
        FROM agent_actions AS action
        JOIN processes AS process
          ON process.action_id = action.action_id
         AND process.user_id = action.user_id
        JOIN jobs AS job
          ON job.process_id = process.process_id
         AND job.user_id = process.user_id
        JOIN execution_sessions AS session
          ON session.user_id = action.user_id
         AND session.action_id = action.action_id
        JOIN workspace_manifests AS manifest
          ON manifest.execution_session_id = session.execution_session_id
         AND manifest.user_id = session.user_id
         AND manifest.action_id = session.action_id
        WHERE action.action_id = :action_id
          AND action.user_id = :user_id
          AND action.status = 'processing'
          AND process.process_id = :parent_process_id
          AND process.kind = 'action'
          AND process.status = 'running'
          AND process.current_job_id = :parent_job_id
          AND job.job_id = :parent_job_id
          AND job.job_type = :action_job_type
          AND job.status = 'running'
          AND job.cancel_requested_at IS NULL
          AND job.logical_key = :action_id
          AND job.attempt >= 1
          AND session.execution_session_id = :root_execution_session_id
          AND session.parent_execution_session_id IS NULL
          AND session.status = 'running'
          AND manifest.manifest_id = :manifest_id
          AND manifest.status = 'ready'
          AND manifest.scratch_root_path = session.cwd_path
        """,
        {
            "action_id": request.action_id,
            "action_job_type": LOCAL_ACTION_JOB_TYPE,
            "manifest_id": request.manifest_id,
            "parent_job_id": request.parent_job_id,
            "parent_process_id": request.parent_process_id,
            "root_execution_session_id": request.root_execution_session_id,
            "user_id": request.user_id,
        },
    ).fetchone()
    if row is None:
        raise ActionSubagentSpawnAuthorityError(
            "Action subagent spawn parent authority is not active"
        )
    lineage = resolve_action_process_lineage_in_connection(
        connection=connection,
        user_id=request.user_id,
        action_id=request.action_id,
        process_id=request.parent_process_id,
    )
    if lineage.job_id != request.parent_job_id:
        raise ActionSubagentSpawnAuthorityError(
            "Action subagent spawn parent lineage is not active"
        )
    _require_durable_supervisor_think(
        connection,
        request=request,
        root_process_id=lineage.root_process_id,
        root_accepted_sequence=lineage.root_accepted_sequence,
    )
    return Path(str(row["cwd_path"]))


def _require_workspace_claim_cwds(
    request: ActionSubagentSpawnRequest,
    *,
    current_cwd: Path,
) -> None:
    if any(
        isinstance(claim, WorkspaceSpawnResourceClaim)
        and claim.current_cwd != current_cwd
        for claim in request.resource_claims
    ):
        raise ActionSubagentSpawnAuthorityError(
            "workspace claims require the active execution-session cwd"
        )


def _require_durable_supervisor_think(
    connection: sqlite3.Connection,
    *,
    request: ActionSubagentSpawnRequest,
    root_process_id: str,
    root_accepted_sequence: int,
) -> None:
    row = connection.execute(
        """
        WITH RECURSIVE ancestors(step_id, parent_step_id, step_type, accepted_sequence, adopted_process_id) AS (
            SELECT step_id, parent_step_id, step_type, accepted_sequence, adopted_process_id
            FROM agent_action_steps
            WHERE step_id = :step_id AND user_id = :user_id
              AND action_id = :action_id
            UNION
            SELECT parent.step_id, parent.parent_step_id, parent.step_type,
                   parent.accepted_sequence, parent.adopted_process_id
            FROM agent_action_steps AS parent
            JOIN ancestors AS child ON child.parent_step_id = parent.step_id
            WHERE parent.user_id = :user_id AND parent.action_id = :action_id
        )
        SELECT step.step_type, step.step_name, step.status, step.goal_handle,
               (SELECT MIN(accepted_sequence) FROM ancestors WHERE step_type = 'user_request'
                AND adopted_process_id = :root_process_id) AS root_sequence
        FROM agent_action_steps AS step
        WHERE step.step_id = :step_id AND step.user_id = :user_id
          AND step.action_id = :action_id
        """,
        {
            "action_id": request.action_id,
            "root_process_id": root_process_id,
            "step_id": request.origin.llm_step_id,
            "user_id": request.user_id,
        },
    ).fetchone()
    if (
        row is None
        or row["step_type"] != "llm_output"
        or row["step_name"] != "supervisor_think"
        or row["status"] != "success"
        or row["goal_handle"] != "S"
        or row["root_sequence"] != root_accepted_sequence
    ):
        raise ActionSubagentSpawnAuthorityError(
            "Action subagent spawn requires the active logical run's durable "
            "Supervisor THINK"
        )


def _is_exact_replay(
    connection: sqlite3.Connection,
    *,
    request: ActionSubagentSpawnRequest,
    child_process_id: str,
    job_id: str,
    payload: ActionSubagentJobPayload,
    claim_ids: tuple[str, ...],
) -> bool:
    process = connection.execute(
        """
        SELECT user_id, kind, action_id, parent_process_id
        FROM processes WHERE process_id = ?
        """,
        (child_process_id,),
    ).fetchone()
    job = connection.execute(
        """
        SELECT job.user_id, job.job_type, job.process_id, job.logical_key,
               payload.payload_json
        FROM jobs AS job
        LEFT JOIN job_payloads AS payload ON payload.job_id = job.job_id
        WHERE job.job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if process is None and job is None:
        return False
    expected_process = (
        request.user_id,
        "action_subagent",
        request.action_id,
        request.parent_process_id,
    )
    expected_job = (
        request.user_id,
        LOCAL_ACTION_SUBAGENT_JOB_TYPE,
        child_process_id,
        job_id,
        serialize_action_subagent_job_payload(payload),
    )
    if (
        process is None
        or job is None
        or tuple(process) != expected_process
        or tuple(job) != expected_job
    ):
        raise ActionSubagentSpawnConflictError(
            "Action subagent spawn identity is bound to different content"
        )
    stored_claim_ids = tuple(
        str(row["claim_id"])
        for row in connection.execute(
            """
            SELECT claim_id
            FROM action_subagent_resource_claims
            WHERE user_id = ? AND action_id = ? AND parent_process_id = ?
              AND child_process_id = ?
            ORDER BY claim_id
            """,
            (
                request.user_id,
                request.action_id,
                request.parent_process_id,
                child_process_id,
            ),
        ).fetchall()
    )
    if stored_claim_ids != tuple(sorted(claim_ids)):
        raise ActionSubagentSpawnConflictError(
            "Action subagent spawn claims differ from the durable request"
        )
    return True
