from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import ReadAccessScope
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.manifest_paths import ManifestRoot, load_manifest_roots

from ...storage.migrations import MigrationError
from ..models import (
    ApprovalSessionUpsertInput,
    StoredExecutionSession,
    StoredToolDefinition,
    ToolOutputStorageKind,
)
from ..outside_workspace_grant import approved_summary_covers_request
from ..repository import (
    ApprovalSessionExecutionConflictError,
    apply_approval_decision_with_grants,
    load_action_approval_mode,
    load_action_manifest_id,
    load_active_capability_grants,
    load_approval_session_by_request,
    load_effective_approval_preference,
    load_execution_session,
    load_latest_approval_session,
    load_tool_definition,
    upsert_approval_session,
)
from .broker_protocol import BrokerPathAccessKind

READ_TOOL_ID = "read"
RENDER_PDF_PAGE_TOOL_ID = "render_pdf_page"
LIST_TOOL_ID = "list"
GLOB_TOOL_ID = "glob"
GREP_TOOL_ID = "grep"
APPLY_PATCH_TOOL_ID = "apply_patch"
BASH_TOOL_ID = "bash"
RUN_PYTHON_TOOL_ID = "run_python"
RUNNING_SESSION_STATUS = "running"
ALLOWED_CAPABILITIES_KEY = "allowed_capabilities"
APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND = "workspace_edit_and_command"
APPROVAL_SCOPE_SCREEN_CAPTURE = "screen_capture"
SCREEN_CAPTURE_INTENT_CLASS = "screen_capture"
APPROVAL_MODE_PROMPT_EACH_TIME: Literal["prompt_each_time"] = "prompt_each_time"
APPROVAL_MODE_ALWAYS_ALLOW: Literal["always_allow"] = "always_allow"
APPROVAL_STATUS_PENDING: Literal["pending"] = "pending"
APPROVAL_STATUS_APPROVED_ONCE: Literal["approved_once"] = "approved_once"
APPROVAL_STATUS_DENIED: Literal["denied"] = "denied"
BROKER_TOOL_TIMEOUT_ERROR_TYPE = "ToolTimeoutError"


class BrokerExecutionError(RuntimeError):
    """Broker execution failure."""

    def __init__(
        self,
        message: str,
        *,
        tool_invocation_id: str | None = None,
        finalized_output: JSONValue | None = None,
        output_storage_kind: ToolOutputStorageKind | None = None,
    ) -> None:
        if (finalized_output is None) != (output_storage_kind is None):
            raise ValueError(
                "finalized broker output and storage kind must be provided together"
            )
        super().__init__(message)
        self.tool_invocation_id = tool_invocation_id
        self.finalized_output = finalized_output
        self.output_storage_kind = output_storage_kind


class FinalizedBrokerPolicyError(BrokerPolicyError):
    """A policy rejection whose terminal output was durably finalized."""

    def __init__(
        self,
        *,
        cause: BrokerPolicyError,
        tool_invocation_id: str,
        finalized_output: JSONValue,
        output_storage_kind: ToolOutputStorageKind,
    ) -> None:
        super().__init__(
            str(cause),
            code=cause.code,
            fix_hint=cause.fix_hint,
            examples=cause.examples,
        )
        self.cause = cause
        self.tool_invocation_id = tool_invocation_id
        self.finalized_output = finalized_output
        self.output_storage_kind = output_storage_kind


class BrokerApprovalRequiredError(RuntimeError):
    """Tool execution requires explicit user approval."""

    def __init__(self, message: str, *, approval_session_id: str) -> None:
        super().__init__(message)
        self.approval_session_id = approval_session_id


class BrokerApprovalDeniedError(RuntimeError):
    """Tool execution was denied by the user approval decision."""

    def __init__(self, *, approval_session_id: str) -> None:
        super().__init__("tool approval was denied")
        self.approval_session_id = approval_session_id


class ApprovalDecisionError(RuntimeError):
    """Approval decision application failure."""


@dataclass(frozen=True, slots=True)
class BrokerContext:
    manifest_id: str
    actor_process_id: str
    scratch_root_path: Path
    execution_session: StoredExecutionSession
    tool_definition: StoredToolDefinition
    path_access_kind: BrokerPathAccessKind
    manifest_roots: tuple[ManifestRoot, ...]
    read_access_scope: ReadAccessScope
    db_path: Path
    busy_timeout_ms: int


def resolve_workspace_relative_path(
    *,
    workspace_root: Path,
    relative_path: str,
    field_name: str,
) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise BrokerPolicyError(f"{field_name} must be workspace-relative")
    if not relative_path.strip():
        raise BrokerPolicyError(f"{field_name} must not be empty")
    resolved = (workspace_root / candidate).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise BrokerPolicyError(
            f"{field_name} escapes the bound workspace root"
        ) from exc
    return resolved


def load_broker_context(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_id: str,
    path_access_kind: BrokerPathAccessKind,
    user_id: str,
    actor_process_id: str,
    manifest_id: str,
    execution_session_id: str,
) -> BrokerContext:
    if not actor_process_id.strip():
        raise BrokerPolicyError("actor_process_id is required for brokered tools")
    tool_definition = load_tool_definition(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_id=tool_id,
    )
    execution_session = load_execution_session(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        execution_session_id=execution_session_id,
    )
    if execution_session.user_id != user_id:
        raise BrokerPolicyError("execution session user mismatch")
    if execution_session.status != RUNNING_SESSION_STATUS:
        raise BrokerPolicyError("execution session is not running")
    if execution_session.action_id is None:
        raise BrokerPolicyError("execution session is not bound to an action")
    expected_manifest_id = load_action_manifest_id(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        action_id=execution_session.action_id,
    )
    if manifest_id != expected_manifest_id:
        raise BrokerPolicyError("manifest does not match execution session action")
    manifest_row = _load_ready_manifest_paths(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        manifest_id=manifest_id,
    )
    manifest_roots = load_manifest_roots(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        manifest_id=manifest_id,
    )
    allowlist = execution_session.tool_allowlist_json
    if not isinstance(allowlist, list) or not all(
        isinstance(tool_name, str) for tool_name in allowlist
    ):
        raise MigrationError("tool_allowlist_json must be a string array")
    if tool_id not in allowlist:
        raise BrokerPolicyError(f"tool {tool_id} is not allowlisted")

    return BrokerContext(
        manifest_id=manifest_id,
        actor_process_id=actor_process_id,
        scratch_root_path=manifest_row,
        execution_session=execution_session,
        tool_definition=tool_definition,
        path_access_kind=path_access_kind,
        manifest_roots=manifest_roots,
        read_access_scope=execution_session.read_access_scope,
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )


def _load_ready_manifest_paths(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    manifest_id: str,
) -> Path:
    import sqlite3

    from ..repository.common import _configure_connection

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        row = connection.execute(
            """
            SELECT scratch_root_path
            FROM workspace_manifests
            WHERE user_id = ? AND manifest_id = ? AND status = 'ready'
            """,
            (user_id, manifest_id),
        ).fetchone()
    if row is None:
        raise BrokerPolicyError("ready workspace manifest is not available")
    return Path(str(row["scratch_root_path"])).resolve(strict=False)


def ensure_session_capabilities(*, context: BrokerContext) -> None:
    allowed_capabilities = _load_allowed_capabilities(context)
    missing_capabilities = [
        capability
        for capability in context.tool_definition.required_capabilities
        if capability not in allowed_capabilities
    ]
    if missing_capabilities:
        raise BrokerPolicyError(
            "missing required capabilities: " + ", ".join(missing_capabilities)
        )


def approval_scope_for_intent_class(intent_class: str) -> str:
    """The approval scope a tool's consent is read from and written under.

    Screen capture has its own scope on purpose: a durable grant for workspace
    edits and commands says nothing about recording the screen, so reusing that
    scope would let one consent auto-approve the other.
    """

    if intent_class == SCREEN_CAPTURE_INTENT_CLASS:
        return APPROVAL_SCOPE_SCREEN_CAPTURE
    return APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND


def ensure_tool_authorization(
    *,
    context: BrokerContext,
    tool_invocation_id: str | None,
    tool_request_id: str,
    command_summary: dict[str, JSONValue],
    requested_at: str,
    require_user_prompt: bool = False,
) -> tuple[str | None, Literal["settings", "prompt"] | None]:
    """Authorize one tool request.

    `require_user_prompt` asks the user even under always_allow: that mode is
    consent for the registered workspace only, not for a write outside it.
    """

    allowed_capabilities = _load_allowed_capabilities(context)
    missing_capabilities = [
        capability
        for capability in context.tool_definition.required_capabilities
        if capability not in allowed_capabilities
    ]
    if not missing_capabilities and not require_user_prompt:
        return None, None
    if context.execution_session.action_id is None:
        raise MigrationError(
            "approval-required tool execution requires an owning agent action"
        )

    # Resolved per tool request, so a mode change made from the Agent Overlay
    # while the Action runs takes effect from the next preflight onwards.
    # That control grants file/command consent, never screen-capture consent.
    approval_scope = approval_scope_for_intent_class(
        context.tool_definition.intent_class
    )
    action_approval_mode = (
        load_action_approval_mode(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=context.execution_session.user_id,
            action_id=context.execution_session.action_id,
        )
        if approval_scope == APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND
        else None
    )
    if action_approval_mode is None:
        user_default = load_effective_approval_preference(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=context.execution_session.user_id,
            applies_to=approval_scope,
        )
        approval_mode = user_default.approval_mode
        # A saved always_allow auto-approves only against the durable capability
        # grants written with it. The unsaved built-in default has no row to grant
        # from, and an Action-level override carries its own consent record and is
        # scoped to this one conversation.
        requires_global_grants = user_default.preference_id is not None
    else:
        approval_mode = action_approval_mode
        requires_global_grants = False
    latest_session = load_latest_approval_session(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=context.execution_session.user_id,
        manifest_id=context.manifest_id,
        tool_request_id=tool_request_id,
        tool_id=context.tool_definition.tool_id,
    )
    if latest_session is not None:
        if not approved_summary_covers_request(
            approved=latest_session.command_summary_json,
            requested=command_summary,
            manifest_roots=context.manifest_roots,
        ):
            raise BrokerPolicyError(
                "approval session command summary does not match the tool request"
            )
        if latest_session.status == APPROVAL_STATUS_APPROVED_ONCE:
            if latest_session.claimed_at is not None:
                raise BrokerPolicyError("tool approval has already been claimed")
            if latest_session.approval_source == "settings":
                return latest_session.approval_session_id, "settings"
            return latest_session.approval_session_id, "prompt"
        if latest_session.status == APPROVAL_STATUS_DENIED:
            raise BrokerApprovalDeniedError(
                approval_session_id=latest_session.approval_session_id
            )
        raise BrokerApprovalRequiredError(
            "tool execution requires user approval",
            approval_session_id=latest_session.approval_session_id,
        )

    if approval_mode not in {
        APPROVAL_MODE_ALWAYS_ALLOW,
        APPROVAL_MODE_PROMPT_EACH_TIME,
    }:
        raise MigrationError("unsupported approval mode")
    if approval_mode == APPROVAL_MODE_ALWAYS_ALLOW and not require_user_prompt:
        if requires_global_grants:
            active_grants = load_active_capability_grants(
                db_path=context.db_path,
                busy_timeout_ms=context.busy_timeout_ms,
                user_id=context.execution_session.user_id,
                applies_to=approval_scope,
            )
            missing_grants = [
                capability
                for capability in missing_capabilities
                if capability not in active_grants
            ]
            if missing_grants:
                raise BrokerPolicyError(
                    "durable capability grants are required for always_allow"
                )
        return (
            create_settings_approval_session(
                context=context,
                tool_request_id=tool_request_id,
                command_summary=command_summary,
                requested_at=requested_at,
                approved_capabilities=missing_capabilities,
            ),
            "settings",
        )

    action_id = context.execution_session.action_id
    try:
        approval_session = upsert_approval_session(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            approval_session=ApprovalSessionUpsertInput(
                approval_session_id=str(uuid.uuid4()),
                tool_request_id=tool_request_id,
                user_id=context.execution_session.user_id,
                action_id=action_id,
                manifest_id=context.manifest_id,
                expected_execution_session_id=(
                    context.execution_session.execution_session_id
                ),
                tool_invocation_id=None,
                tool_id=context.tool_definition.tool_id,
                intent_class=context.tool_definition.intent_class,
                approval_source="prompt",
                status=APPROVAL_STATUS_PENDING,
                approved_capabilities_json={
                    "required_capabilities": cast(JSONValue, missing_capabilities)
                },
                command_summary_json=command_summary,
                requested_at=requested_at,
                decided_at=None,
                claimed_at=None,
            ),
        )
    except ApprovalSessionExecutionConflictError as exc:
        raise BrokerPolicyError(str(exc)) from exc
    raise BrokerApprovalRequiredError(
        "tool execution requires user approval",
        approval_session_id=approval_session.approval_session_id,
    )


def apply_approval_decision(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_request_id: str,
    user_id: str,
    action_id: str,
    approval_session_id: str | None,
    decision: Literal["approved_once", "denied"],
    decided_at: str,
) -> None:
    session = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        tool_request_id=tool_request_id,
    )
    if session is None:
        raise ApprovalDecisionError("approval session not found")
    if session.action_id != action_id:
        raise ApprovalDecisionError("approval session action mismatch")
    if (
        approval_session_id is not None
        and session.approval_session_id != approval_session_id
    ):
        raise ApprovalDecisionError("approval session locator mismatch")
    if decision not in {
        APPROVAL_STATUS_APPROVED_ONCE,
        APPROVAL_STATUS_DENIED,
    }:
        raise ApprovalDecisionError("unsupported approval decision")

    result = apply_approval_decision_with_grants(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        tool_request_id=tool_request_id,
        action_id=action_id,
        approval_session_id=approval_session_id,
        expected_current_status=APPROVAL_STATUS_PENDING,
        next_status=decision,
        decided_at=decided_at,
        preference=None,
        grants=(),
    )
    if result.status == "not_found":
        raise ApprovalDecisionError("approval session not found")
    if result.status == "request_mismatch":
        raise ApprovalDecisionError("approval session request mismatch")
    if result.status == "conflict":
        raise ApprovalDecisionError("approval session is not pending")


def _load_allowed_capabilities(context: BrokerContext) -> tuple[str, ...]:
    allowed_capabilities = context.execution_session.capability_snapshot_json.get(
        ALLOWED_CAPABILITIES_KEY
    )
    if not isinstance(allowed_capabilities, list) or not all(
        isinstance(capability, str) for capability in allowed_capabilities
    ):
        raise MigrationError("allowed_capabilities must be a string array")
    return tuple(cast(list[str], allowed_capabilities))


def create_settings_approval_session(
    *,
    context: BrokerContext,
    tool_request_id: str,
    command_summary: dict[str, JSONValue],
    requested_at: str,
    approved_capabilities: list[str],
) -> str:
    action_id = context.execution_session.action_id
    if action_id is None:
        raise MigrationError(
            "settings approval session requires an owning agent action"
        )
    try:
        approval_session = upsert_approval_session(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            approval_session=ApprovalSessionUpsertInput(
                approval_session_id=str(uuid.uuid4()),
                tool_request_id=tool_request_id,
                user_id=context.execution_session.user_id,
                action_id=action_id,
                manifest_id=context.manifest_id,
                expected_execution_session_id=(
                    context.execution_session.execution_session_id
                ),
                tool_invocation_id=None,
                tool_id=context.tool_definition.tool_id,
                intent_class=context.tool_definition.intent_class,
                approval_source="settings",
                status=APPROVAL_STATUS_APPROVED_ONCE,
                approved_capabilities_json={
                    "required_capabilities": cast(JSONValue, approved_capabilities)
                },
                command_summary_json=command_summary,
                requested_at=requested_at,
                decided_at=requested_at,
                claimed_at=None,
            ),
        )
    except ApprovalSessionExecutionConflictError as exc:
        raise BrokerPolicyError(str(exc)) from exc
    return approval_session.approval_session_id


__all__ = [
    "ALLOWED_CAPABILITIES_KEY",
    "APPLY_PATCH_TOOL_ID",
    "APPROVAL_MODE_ALWAYS_ALLOW",
    "APPROVAL_MODE_PROMPT_EACH_TIME",
    "APPROVAL_SCOPE_SCREEN_CAPTURE",
    "APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND",
    "APPROVAL_STATUS_APPROVED_ONCE",
    "APPROVAL_STATUS_DENIED",
    "APPROVAL_STATUS_PENDING",
    "ApprovalDecisionError",
    "BASH_TOOL_ID",
    "BrokerApprovalDeniedError",
    "BrokerApprovalRequiredError",
    "BrokerContext",
    "BrokerExecutionError",
    "READ_TOOL_ID",
    "RENDER_PDF_PAGE_TOOL_ID",
    "RUN_PYTHON_TOOL_ID",
    "apply_approval_decision",
    "approval_scope_for_intent_class",
    "create_settings_approval_session",
    "ensure_session_capabilities",
    "ensure_tool_authorization",
    "load_broker_context",
    "resolve_workspace_relative_path",
]
