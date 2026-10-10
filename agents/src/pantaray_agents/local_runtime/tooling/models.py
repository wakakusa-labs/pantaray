from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import ReadAccessScope
from pantaray_agents.schema.tool_result import (
    ToolOutputOwnerKind as ToolOutputOwnerKind,
)
from pantaray_agents.schema.tool_result import (
    ToolOutputStorageKind as ToolOutputStorageKind,
)

WorkspaceKind = Literal["user_repo", "user_folder", "app_project", "scratch"]
WorkspaceTrustLevel = Literal["user_selected", "app_managed", "ephemeral"]
WorkspaceVcsKind = Literal["git", "none"]
ExecutionMode = Literal["brokered_file_ops", "workspace_command"]
# Whether a sandboxed command may reach the network. Declared here rather
# than beside the broker's own request models because the sandbox protocol
# carries it too, and the sandbox must not depend on the broker that drives
# it.
BrokerNetworkPolicy = Literal["deny", "allow"]
ExecutionSessionStartStatus = Literal["running"]
ExecutionSessionTerminalStatus = Literal["completed", "failed", "canceled", "expired"]
ExecutionSessionStatus = ExecutionSessionStartStatus | ExecutionSessionTerminalStatus
EXECUTION_SESSION_TERMINAL_STATUSES: tuple[ExecutionSessionTerminalStatus, ...] = (
    "completed",
    "failed",
    "canceled",
    "expired",
)
ApprovalMode = Literal["prompt_each_time", "always_allow"]
ApprovalSessionStatus = Literal["pending", "approved_once", "denied"]
ToolRiskLevel = Literal["low", "medium", "high"]
ToolInvocationStartStatus = Literal["queued", "running"]
ToolInvocationTerminalStatus = Literal["completed", "failed", "canceled", "timed_out"]
ToolInvocationStatus = ToolInvocationStartStatus | ToolInvocationTerminalStatus
TOOL_INVOCATION_TERMINAL_STATUSES: tuple[ToolInvocationTerminalStatus, ...] = (
    "completed",
    "failed",
    "canceled",
    "timed_out",
)
ToolRuntimeResourceKind = Literal["process_group", "temp_file", "temp_dir", "lock"]
ToolRuntimeResourceStatus = Literal["active", "cleaned", "cleanup_failed", "abandoned"]
ToolIntentClass = Literal[
    "read_only",
    "surgical_edit",
    "bulk_edit",
    "process_exec_local",
    "dependency_install",
    "dependency_update",
    "network_access",
    "automation_control",
    "screen_capture",
]

type JSONStringMap = dict[str, JSONValue]

PRODUCER_BACKED_PATH_RESOURCE_KINDS: tuple[ToolRuntimeResourceKind, ...] = (
    "temp_file",
    "temp_dir",
)


class InternalStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


@dataclass(frozen=True, slots=True)
class ToolDefinitionSeed:
    tool_id: str
    tool_name: str
    tool_description: str
    category: str
    risk_level: ToolRiskLevel
    intent_class: ToolIntentClass
    required_capabilities: tuple[str, ...]
    input_schema_json: JSONStringMap
    output_schema_json: JSONStringMap | None
    rate_limit_json: JSONStringMap | None
    default_timeout_ms: int | None
    llm_guide_json: JSONStringMap
    is_enabled: bool
    version: str


class ExecutionSessionCreateInput(InternalStrictModel):
    execution_session_id: str
    user_id: str
    action_id: str | None
    parent_execution_session_id: str | None
    exec_mode: ExecutionMode
    cwd_path: str
    action_temp_dir: str | None
    app_runtime_python: str | None
    network_policy: str
    read_access_scope: ReadAccessScope
    capability_snapshot_json: JSONStringMap
    tool_allowlist_json: JSONValue | None
    status: ExecutionSessionStartStatus
    started_at: str
    expires_at: str | None


class ToolInvocationStartInput(InternalStrictModel):
    invocation_id: str
    tool_request_id: str | None
    user_id: str
    action_id: str
    step_id: str
    tool_id: str
    manifest_id: str
    execution_session_id: str
    cwd: str | None
    timeout_ms: int | None
    intent_class: ToolIntentClass
    network_policy: str | None
    command_summary_json: JSONStringMap | None
    capability_snapshot_json: JSONStringMap | None
    request_json: JSONStringMap
    status: ToolInvocationStartStatus
    started_at: str


class CommandToolInvocationStartInput(ToolInvocationStartInput):
    actor_process_id: str
    write_roots: tuple[Path, ...]


class ToolInvocationFileReferenceInput(InternalStrictModel):
    file_reference_id: str
    path: str


class ToolInvocationReadMemoryInput(InternalStrictModel):
    path: str
    source_path: str
    content: str


class ToolInvocationCompletionInput(InternalStrictModel):
    invocation_id: str
    status: ToolInvocationTerminalStatus
    completed_at: str
    output_json: JSONValue | None
    output_storage_kind: ToolOutputStorageKind
    search_text: str | None
    stdout_text: str | None
    stderr_text: str | None
    redaction_applied: bool
    file_references: tuple[ToolInvocationFileReferenceInput, ...] = ()
    read_memory: ToolInvocationReadMemoryInput | None = None


class ToolRuntimeResourceCreateInput(InternalStrictModel):
    resource_id: str
    execution_session_id: str
    tool_invocation_id: str | None
    action_id: str | None
    resource_kind: ToolRuntimeResourceKind
    status: ToolRuntimeResourceStatus
    created_at: str
    pid: int | None
    pgid: int | None
    process_start_signature: str | None
    resource_path: str | None
    lock_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredToolRuntimeResource:
    resource_id: str
    execution_session_id: str
    tool_invocation_id: str | None
    action_id: str | None
    resource_kind: ToolRuntimeResourceKind
    status: ToolRuntimeResourceStatus
    pid: int | None
    pgid: int | None
    process_start_signature: str | None
    resource_path: str | None
    created_at: str
    updated_at: str
    cleaned_at: str | None
    cleanup_error: str | None
    cleanup_attempts: int
    lock_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredToolRuntimeResourceEvent:
    event_id: str
    resource_id: str | None
    tool_invocation_id: str | None
    action_id: str | None
    event_type: str
    message: str
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredInflightToolInvocation:
    invocation_id: str
    tool_id: str
    action_id: str
    status: ToolInvocationStatus


@dataclass(frozen=True, slots=True)
class ActionExecutionContext:
    manifest_id: str
    workspace_path: Path
    tool_results_path: Path
    action_temp_dir: Path
    app_runtime_python: Path
    execution_session_id: str
    network_policy: str
    read_access_scope: ReadAccessScope


@dataclass(frozen=True, slots=True)
class ToolAuditMetadata:
    intent_class: ToolIntentClass
    network_policy: str
    required_capabilities: tuple[str, ...]
    default_timeout_ms: int | None


@dataclass(frozen=True, slots=True)
class StoredToolDefinition:
    tool_id: str
    intent_class: ToolIntentClass
    required_capabilities: tuple[str, ...]
    default_timeout_ms: int | None
    llm_guide_json: JSONStringMap
    output_schema_json: JSONStringMap | None


@dataclass(frozen=True, slots=True)
class ToolInvocationCompletionContext:
    tool_id: str
    user_id: str
    action_id: str
    manifest_id: str


@dataclass(frozen=True, slots=True)
class StoredExecutionSession:
    execution_session_id: str
    action_id: str | None
    user_id: str
    status: ExecutionSessionStatus
    exec_mode: ExecutionMode
    cwd_path: str
    action_temp_dir: str | None
    app_runtime_python: str | None
    network_policy: str
    read_access_scope: ReadAccessScope
    capability_snapshot_json: JSONStringMap
    tool_allowlist_json: JSONValue | None


@dataclass(frozen=True, slots=True)
class StoredApprovalPreference:
    # None for the built-in default, which the user has not saved.
    preference_id: str | None
    scope_type: Literal["global"]
    scope_ref: str | None
    approval_mode: ApprovalMode
    applies_to: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredApprovalSession:
    approval_session_id: str
    user_id: str
    action_id: str
    manifest_id: str
    claimed_at: str | None
    tool_invocation_id: str | None
    tool_id: str
    intent_class: ToolIntentClass
    approval_source: Literal["prompt", "settings"]
    tool_request_id: str
    status: ApprovalSessionStatus
    approved_capabilities_json: JSONStringMap
    command_summary_json: JSONStringMap
    decided_at: str | None


ApprovalSessionMutationStatus = Literal[
    "updated", "not_found", "request_mismatch", "conflict"
]


@dataclass(frozen=True, slots=True)
class ApprovalSessionMutationResult:
    status: ApprovalSessionMutationStatus
    approval_session: StoredApprovalSession | None


class ApprovalPreferenceUpsertInput(InternalStrictModel):
    preference_id: str
    user_id: str
    scope_type: Literal["global"]
    scope_ref: str | None
    approval_mode: ApprovalMode
    applies_to: tuple[str, ...]
    created_at: str
    updated_at: str


class CapabilityGrantCreateInput(InternalStrictModel):
    grant_id: str
    user_id: str
    preference_id: str | None
    capability: str
    scope_type: Literal["global"]
    scope_ref: str | None
    grant_source: Literal["settings", "prompt"]
    granted_at: str


class ApprovalSessionUpsertInput(InternalStrictModel):
    approval_session_id: str
    tool_request_id: str
    user_id: str
    action_id: str
    manifest_id: str
    expected_execution_session_id: str
    tool_invocation_id: str | None
    tool_id: str
    intent_class: ToolIntentClass
    approval_source: Literal["prompt", "settings"]
    status: ApprovalSessionStatus
    approved_capabilities_json: JSONStringMap
    command_summary_json: JSONStringMap
    requested_at: str
    decided_at: str | None
    claimed_at: str | None
