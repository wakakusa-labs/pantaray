from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from functools import cache
from pathlib import Path

from pantaray_agents.schema.read_access import ReadAccessScope

from ..app_runtime_verification import load_and_verify_app_runtime_python_from_env
from ..descriptor_access import DescriptorPathError, open_directory_descriptor
from ..runtime.runtime_env import read_local_runtime_artifact_root
from ..storage.migrations import MigrationError
from ..storage.transactions import immediate_transaction
from .action_session_temp_paths import (
    LOCAL_RUNTIME_WORKSPACE_DIRNAME,
    MANAGED_DIRECTORY_MODE,
    SCRATCH_SESSION_TEMP_DIRNAME,
    SCRATCH_WORKSPACE_DIRNAME,
    ActionSessionTempPathError,
    ActionStoragePaths,
    open_durable_action_storage_child,
    resolve_action_session_temp_leaf,
    resolve_action_storage_paths,
)
from .brokering.broker_common import (
    APPLY_PATCH_TOOL_ID,
    BASH_TOOL_ID,
    GLOB_TOOL_ID,
    GREP_TOOL_ID,
    LIST_TOOL_ID,
    READ_TOOL_ID,
    RENDER_PDF_PAGE_TOOL_ID,
    RUN_PYTHON_TOOL_ID,
)
from .models import (
    ActionExecutionContext,
    ExecutionSessionCreateInput,
    StoredToolDefinition,
    ToolAuditMetadata,
    ToolDefinitionSeed,
    ToolRuntimeResourceCreateInput,
)
from .repository import (
    ExecutionSessionTerminalStateError,
    complete_execution_session,
    load_read_access_scope,
    seed_tool_definitions,
)
from .repository.executions import create_execution_session_in_connection
from .repository.manifests import (
    _prepare_agent_experience_root,
    ensure_action_workspace_manifest_in_connection,
)
from .resources.resource_db_support import configure_connection
from .resources.resource_store import create_tool_runtime_resource_in_connection
from .tool_result_storage import ACTION_TOOL_RESULTS_DIRNAME


class ActionExecutionContextError(RuntimeError):
    """Raised when an action execution target cannot be bound safely."""


class VerifiedActionExecutionContextLeafError(ActionExecutionContextError):
    def __init__(self, *, execution_session_id: str) -> None:
        super().__init__(
            "retained Action execution context has no canonical existing temp leaf"
        )
        self.execution_session_id = execution_session_id


SCRATCH_WORKSPACE_KIND = "scratch"
SCRATCH_WORKSPACE_TRUST_LEVEL = "app_managed"
SCRATCH_WORKSPACE_VCS_KIND = "none"
SCRATCH_EXECUTION_MODE = "brokered_file_ops"
SCRATCH_EXECUTION_STATUS = "running"
SCRATCH_NETWORK_POLICY = "cloud-proxy-only"
TOOL_CATEGORY_INTERNAL = "internal"
TOOL_CATEGORY_WEB = "web"
TOOL_CATEGORY_WORKSPACE = "workspace"
TOOL_RISK_LOW = "low"
TOOL_RISK_MEDIUM = "medium"
TOOL_RISK_HIGH = "high"
CAPABILITY_SCOPED_READ = "scoped_read"
CAPABILITY_SCOPED_WRITE = "scoped_write"
CAPABILITY_PROCESS_EXEC_LOCAL = "process_exec_local"
TOOL_INTENT_READ_ONLY = "read_only"
TOOL_INTENT_SURGICAL_EDIT = "surgical_edit"
TOOL_INTENT_PROCESS_EXEC_LOCAL = "process_exec_local"
DEFAULT_READ_TIMEOUT_MS = 5_000
DEFAULT_BASH_TIMEOUT_MS = 60_000
WORKSPACE_TOOL_IDS = frozenset(
    {
        READ_TOOL_ID,
        RENDER_PDF_PAGE_TOOL_ID,
        LIST_TOOL_ID,
        GLOB_TOOL_ID,
        GREP_TOOL_ID,
        APPLY_PATCH_TOOL_ID,
        BASH_TOOL_ID,
        RUN_PYTHON_TOOL_ID,
    }
)
HIGH_RISK_TOOL_IDS = frozenset({APPLY_PATCH_TOOL_ID, BASH_TOOL_ID, RUN_PYTHON_TOOL_ID})
SCRATCH_ALLOWED_CAPABILITIES = (CAPABILITY_SCOPED_READ,)


def resolve_tool_audit_metadata(
    definition: ToolDefinitionSeed | StoredToolDefinition,
    *,
    session_network_policy: str,
) -> ToolAuditMetadata:
    return ToolAuditMetadata(
        intent_class=definition.intent_class,
        network_policy=session_network_policy,
        required_capabilities=definition.required_capabilities,
        default_timeout_ms=definition.default_timeout_ms,
    )


def _all_runtime_tool_definitions():
    from pantaray_agents.agents.action_agent.tools import TOOL_REGISTRY

    return TOOL_REGISTRY


def _infer_tool_category(tool_id: str) -> str:
    if tool_id.startswith("web_"):
        return TOOL_CATEGORY_WEB
    if tool_id in WORKSPACE_TOOL_IDS:
        return TOOL_CATEGORY_WORKSPACE
    return TOOL_CATEGORY_INTERNAL


def _infer_tool_risk(tool_id: str) -> str:
    if tool_id in HIGH_RISK_TOOL_IDS:
        return TOOL_RISK_HIGH
    if tool_id.startswith("web_"):
        return TOOL_RISK_MEDIUM
    return TOOL_RISK_LOW


def _build_tool_seed(tool_definition) -> ToolDefinitionSeed:
    from pantaray_agents.agents.action_agent.tools.base import schema_to_plain_json

    input_schema_json = schema_to_plain_json(tool_definition.input_schema)
    output_schema_json = schema_to_plain_json(tool_definition.output_schema)
    if not isinstance(input_schema_json, dict) or not isinstance(
        output_schema_json, dict
    ):
        raise TypeError("tool input and output schemas must be JSON objects")
    return ToolDefinitionSeed(
        tool_id=tool_definition.tool_id,
        tool_name=tool_definition.name,
        tool_description=tool_definition.description,
        category=_infer_tool_category(tool_definition.tool_id),
        risk_level=_infer_tool_risk(tool_definition.tool_id),
        intent_class=tool_definition.execution_policy.intent_class,
        required_capabilities=tool_definition.execution_policy.required_capabilities,
        input_schema_json=input_schema_json,
        output_schema_json=output_schema_json,
        rate_limit_json=None,
        default_timeout_ms=tool_definition.execution_policy.default_timeout_ms,
        llm_guide_json={
            "what": tool_definition.guide.what,
            "when": tool_definition.guide.when,
            "pitfalls": tool_definition.guide.pitfalls,
        },
        is_enabled=True,
        version=tool_definition.input_schema_fingerprint,
    )


def build_local_tool_definition_seeds() -> tuple[ToolDefinitionSeed, ...]:
    seeds = [
        _build_tool_seed(tool) for tool in _all_runtime_tool_definitions().values()
    ]
    return tuple(sorted(seeds, key=lambda seed: seed.tool_id))


def bootstrap_local_tooling_catalog(*, db_path: Path, busy_timeout_ms: int) -> None:
    seed_tool_definitions(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        definitions=build_local_tool_definition_seeds(),
    )


@cache
def _resolve_verified_app_runtime_python() -> Path:
    return load_and_verify_app_runtime_python_from_env()


def _ensure_action_storage_layout(
    *,
    db_path: Path,
    user_id: str,
    action_id: str,
) -> ActionStoragePaths:
    try:
        paths = resolve_action_storage_paths(
            db_path=db_path,
            user_id=user_id,
            action_id=action_id,
        )
    except ActionSessionTempPathError as exc:
        raise ActionExecutionContextError(str(exc)) from exc
    base_path = paths.storage_base
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    open_fds: list[int] = []
    try:
        base_fd = os.open(base_path, directory_flags)
        open_fds.append(base_fd)
        workspaces_fd = open_durable_action_storage_child(
            parent_fd=base_fd,
            name=LOCAL_RUNTIME_WORKSPACE_DIRNAME,
        )
        open_fds.append(workspaces_fd)
        scratch_root_fd = open_durable_action_storage_child(
            parent_fd=workspaces_fd,
            name=SCRATCH_WORKSPACE_DIRNAME,
        )
        open_fds.append(scratch_root_fd)
        user_fd = open_durable_action_storage_child(
            parent_fd=scratch_root_fd,
            name=user_id,
        )
        open_fds.append(user_fd)
        action_fd = open_durable_action_storage_child(
            parent_fd=user_fd,
            name=action_id,
        )
        open_fds.append(action_fd)
        workspace_fd = open_durable_action_storage_child(
            parent_fd=action_fd,
            name=SCRATCH_WORKSPACE_DIRNAME,
        )
        open_fds.append(workspace_fd)
        tool_results_fd = open_durable_action_storage_child(
            parent_fd=action_fd,
            name=ACTION_TOOL_RESULTS_DIRNAME,
        )
        open_fds.append(tool_results_fd)
        temp_fd = open_durable_action_storage_child(
            parent_fd=workspace_fd,
            name=SCRATCH_SESSION_TEMP_DIRNAME,
        )
        open_fds.append(temp_fd)
    except OSError as exc:
        raise ActionExecutionContextError(
            "failed to create the durable Action storage layout"
        ) from exc
    finally:
        for directory_fd in reversed(open_fds):
            os.close(directory_fd)

    return paths


def ensure_action_scratch_execution_context(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    started_at: str,
    allowed_tool_ids: tuple[str, ...],
) -> ActionExecutionContext:
    execution_session_id = str(uuid.uuid4())
    paths = _ensure_action_storage_layout(
        db_path=db_path,
        user_id=user_id,
        action_id=action_id,
    )
    workspace_path = paths.workspace
    tool_results_path = paths.tool_results
    temp_dir_path = resolve_action_session_temp_leaf(
        paths=paths,
        execution_session_id=execution_session_id,
    )
    app_runtime_python = _resolve_verified_app_runtime_python()
    read_access_scope = load_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
    )
    manifest_id = _persist_action_execution_context(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        execution_session_id=execution_session_id,
        user_id=user_id,
        action_id=action_id,
        workspace_path=workspace_path,
        tool_results_path=tool_results_path,
        temp_dir_path=temp_dir_path,
        app_runtime_python=app_runtime_python,
        read_access_scope=read_access_scope,
        allowed_tool_ids=allowed_tool_ids,
        started_at=started_at,
    )
    try:
        _materialize_action_session_temp_leaf(
            session_temp_root=paths.session_temp_root,
            execution_session_id=execution_session_id,
        )
    except (OSError, DescriptorPathError) as exc:
        _mark_session_failed_after_leaf_error(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            execution_session_id=execution_session_id,
            failed_at=started_at,
            leaf_error=exc,
        )
    return ActionExecutionContext(
        manifest_id=manifest_id,
        workspace_path=workspace_path,
        tool_results_path=tool_results_path,
        action_temp_dir=temp_dir_path,
        app_runtime_python=app_runtime_python,
        execution_session_id=execution_session_id,
        network_policy=SCRATCH_NETWORK_POLICY,
        read_access_scope=read_access_scope,
    )


def _persist_action_execution_context(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    execution_session_id: str,
    user_id: str,
    action_id: str,
    workspace_path: Path,
    tool_results_path: Path,
    temp_dir_path: Path,
    app_runtime_python: Path,
    read_access_scope: ReadAccessScope,
    allowed_tool_ids: tuple[str, ...],
    started_at: str,
) -> str:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        agent_experience_root = _prepare_agent_experience_root(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            artifact_root=read_local_runtime_artifact_root(),
        )
        with immediate_transaction(connection):
            create_execution_session_in_connection(
                connection=connection,
                session=ExecutionSessionCreateInput(
                    execution_session_id=execution_session_id,
                    user_id=user_id,
                    action_id=action_id,
                    parent_execution_session_id=None,
                    exec_mode=SCRATCH_EXECUTION_MODE,
                    cwd_path=str(workspace_path),
                    action_temp_dir=str(temp_dir_path),
                    app_runtime_python=str(app_runtime_python),
                    network_policy=SCRATCH_NETWORK_POLICY,
                    read_access_scope=read_access_scope,
                    capability_snapshot_json={
                        "allowed_capabilities": list(SCRATCH_ALLOWED_CAPABILITIES)
                    },
                    tool_allowlist_json=sorted(allowed_tool_ids),
                    status=SCRATCH_EXECUTION_STATUS,
                    started_at=started_at,
                    expires_at=None,
                ),
            )
            create_tool_runtime_resource_in_connection(
                connection=connection,
                resource=ToolRuntimeResourceCreateInput(
                    resource_id=str(uuid.uuid4()),
                    execution_session_id=execution_session_id,
                    tool_invocation_id=None,
                    action_id=action_id,
                    resource_kind="temp_dir",
                    status="active",
                    created_at=started_at,
                    pid=None,
                    pgid=None,
                    process_start_signature=None,
                    resource_path=str(temp_dir_path),
                ),
            )
            return ensure_action_workspace_manifest_in_connection(
                connection=connection,
                user_id=user_id,
                action_id=action_id,
                execution_session_id=execution_session_id,
                scratch_root_id=f"root:{action_id}:scratch",
                manifest_id=f"manifest:{action_id}",
                scratch_real_path=str(workspace_path),
                tool_results_real_path=str(tool_results_path),
                agent_experience_root=agent_experience_root,
                created_at=started_at,
            )


def _materialize_action_session_temp_leaf(
    *, session_temp_root: Path, execution_session_id: str
) -> None:
    parent_fd = open_directory_descriptor(root_path=session_temp_root)
    leaf_fd: int | None = None
    try:
        os.mkdir(execution_session_id, mode=MANAGED_DIRECTORY_MODE, dir_fd=parent_fd)
        os.fsync(parent_fd)
        leaf_fd = os.open(
            execution_session_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        if stat.S_IMODE(os.fstat(leaf_fd).st_mode) != MANAGED_DIRECTORY_MODE:
            os.fchmod(leaf_fd, MANAGED_DIRECTORY_MODE)
            os.fsync(leaf_fd)
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        os.close(parent_fd)


def _mark_session_failed_after_leaf_error(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    execution_session_id: str,
    failed_at: str,
    leaf_error: OSError | DescriptorPathError,
) -> None:
    try:
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            execution_session_id=execution_session_id,
            status="failed",
            completed_at=failed_at,
        )
    except (
        sqlite3.Error,
        MigrationError,
        ExecutionSessionTerminalStateError,
    ) as compensation_error:
        raise ActionExecutionContextError(
            "failed to create the Action session temp leaf "
            f"({type(leaf_error).__name__}: {leaf_error}); failed to mark its "
            f"session failed ({type(compensation_error).__name__}: {compensation_error})"
        ) from leaf_error
    raise ActionExecutionContextError(
        "failed to create the Action session temp leaf"
    ) from leaf_error


def validate_reusable_action_scratch_execution_context(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    manifest_id: str,
    execution_session_id: str,
    execution_network_policy: str,
    action_temp_dir: str,
    app_runtime_python: str,
    read_access_scope: ReadAccessScope,
) -> None:
    try:
        paths = resolve_action_storage_paths(
            db_path=db_path,
            user_id=user_id,
            action_id=action_id,
        )
        canonical_leaf = resolve_action_session_temp_leaf(
            paths=paths,
            execution_session_id=execution_session_id,
        )
    except ActionSessionTempPathError as exc:
        raise ActionExecutionContextError(
            "retained Action execution context has invalid path identity"
        ) from exc
    if action_temp_dir != str(canonical_leaf):
        raise ActionExecutionContextError(
            "retained Action execution context has a noncanonical session temp path"
        )
    database_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT session.execution_session_id
            FROM execution_sessions AS session
            JOIN agent_actions AS action
              ON action.action_id = session.action_id
             AND action.user_id = session.user_id
            JOIN workspace_manifests AS manifest
              ON manifest.execution_session_id = session.execution_session_id
             AND manifest.user_id = session.user_id
             AND manifest.action_id = session.action_id
            JOIN tool_runtime_resources AS root
              ON root.execution_session_id = session.execution_session_id
             AND root.resource_kind = 'temp_dir'
             AND root.tool_invocation_id IS NULL
             AND root.action_id = session.action_id
            WHERE session.execution_session_id = :execution_session_id
              AND session.user_id = :user_id AND session.action_id = :action_id
              AND session.parent_execution_session_id IS NULL
              AND session.status = 'running'
              AND session.exec_mode = :exec_mode
              AND session.cwd_path = :workspace_path
              AND session.action_temp_dir = :action_temp_dir
              AND session.app_runtime_python = :app_runtime_python
              AND session.network_policy = :network_policy
              AND session.read_access_scope = :read_access_scope
              AND action.status = 'processing'
              AND manifest.manifest_id = :manifest_id
              AND manifest.status = 'ready'
              AND manifest.scratch_root_path = :workspace_path
              AND root.status = 'active'
              AND root.resource_path = :action_temp_dir
              AND root.pid IS NULL AND root.pgid IS NULL
              AND root.process_start_signature IS NULL AND root.lock_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tool_runtime_resources AS other_root
                  WHERE other_root.execution_session_id = session.execution_session_id
                    AND other_root.resource_kind = 'temp_dir'
                    AND other_root.tool_invocation_id IS NULL
                    AND other_root.action_id IS NOT NULL
                    AND other_root.resource_id <> root.resource_id
              )
            """,
            {
                "action_id": action_id,
                "action_temp_dir": action_temp_dir,
                "app_runtime_python": app_runtime_python,
                "exec_mode": SCRATCH_EXECUTION_MODE,
                "execution_session_id": execution_session_id,
                "manifest_id": manifest_id,
                "network_policy": execution_network_policy,
                "read_access_scope": read_access_scope,
                "user_id": user_id,
                "workspace_path": str(paths.workspace),
            },
        ).fetchone()
    if row is None:
        raise ActionExecutionContextError(
            "retained Action execution context no longer has exact durable authority"
        )
    try:
        leaf_fd = open_directory_descriptor(root_path=canonical_leaf)
    except (DescriptorPathError, OSError) as exc:
        raise VerifiedActionExecutionContextLeafError(
            execution_session_id=execution_session_id
        ) from exc
    os.close(leaf_fd)
