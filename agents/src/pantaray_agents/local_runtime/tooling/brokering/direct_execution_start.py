from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from ..audit_payloads import build_run_python_request_audit_args
from ..models import CommandToolInvocationStartInput, ToolInvocationStartInput
from ..repository import (
    ToolInvocationSessionConflictError,
    record_tool_invocation_start,
)
from .broker_common import BrokerContext
from .broker_protocol import (
    ValidatedCommandRequest,
    ValidatedGlobRequest,
    ValidatedGrepRequest,
    ValidatedListRequest,
    ValidatedPatchRequest,
    ValidatedReadRequest,
    ValidatedRenderPdfPageRequest,
)

type DirectReadOnlyRequest = (
    ValidatedReadRequest
    | ValidatedRenderPdfPageRequest
    | ValidatedListRequest
    | ValidatedGlobRequest
    | ValidatedGrepRequest
)


def start_direct_execution(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    context: BrokerContext,
    validated: DirectReadOnlyRequest | ValidatedPatchRequest | ValidatedCommandRequest,
    args: dict[str, JSONValue],
) -> str | None:
    tool_invocation_id = validated.tool_invocation_id
    if tool_invocation_id is None or not tool_invocation_id.strip():
        return None
    if not should_start_direct_invocation(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_invocation_id=tool_invocation_id,
    ):
        raise BrokerPolicyError("tool invocation has already been claimed")
    try:
        record_tool_invocation_start(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            invocation=build_direct_invocation_start(
                context=context,
                validated=validated,
                args=args,
            ),
        )
    except ToolInvocationSessionConflictError as exc:
        raise BrokerPolicyError(
            "execution session no longer accepts tool invocation creation"
        ) from exc
    return tool_invocation_id


def should_start_direct_invocation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_invocation_id: str,
) -> bool:
    return not tool_invocation_exists(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_invocation_id=tool_invocation_id,
    )


def tool_invocation_exists(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_invocation_id: str | None,
) -> bool:
    if tool_invocation_id is None or not tool_invocation_id.strip():
        return False
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        row = connection.execute(
            "SELECT invocation_id FROM tool_invocations WHERE invocation_id = ?",
            (tool_invocation_id,),
        ).fetchone()
    return row is not None


def build_direct_invocation_start(
    *,
    context: BrokerContext,
    validated: DirectReadOnlyRequest | ValidatedPatchRequest | ValidatedCommandRequest,
    args: dict[str, JSONValue],
) -> ToolInvocationStartInput:
    if validated.tool_invocation_id is None:
        raise BrokerPolicyError("direct broker execution requires an invocation id")
    if isinstance(
        validated,
        (
            ValidatedReadRequest,
            ValidatedRenderPdfPageRequest,
            ValidatedListRequest,
            ValidatedGlobRequest,
            ValidatedGrepRequest,
        ),
    ):
        return _build_read_only_invocation_start(
            context=context,
            validated=validated,
            args=args,
        )
    if isinstance(validated, ValidatedCommandRequest):
        return CommandToolInvocationStartInput(
            actor_process_id=context.actor_process_id,
            write_roots=tuple(Path(root) for root in validated.real_write_roots),
            invocation_id=validated.tool_invocation_id,
            tool_request_id=validated.tool_request_id,
            user_id=context.execution_session.user_id,
            action_id=validated.action_id,
            step_id=validated.tool_request_id,
            tool_id=context.tool_definition.tool_id,
            manifest_id=context.manifest_id,
            execution_session_id=validated.execution_session_id,
            cwd=validated.cwd,
            timeout_ms=validated.timeout_ms,
            intent_class=context.tool_definition.intent_class,
            network_policy=validated.network_policy,
            command_summary_json=validated.command_summary_json,
            capability_snapshot_json=context.execution_session.capability_snapshot_json,
            request_json=_command_request_json_for_audit(
                validated=validated,
                args=args,
            ),
            status="running",
            started_at=validated.requested_at,
        )
    return ToolInvocationStartInput(
        invocation_id=validated.tool_invocation_id,
        tool_request_id=validated.tool_request_id,
        user_id=context.execution_session.user_id,
        action_id=validated.action_id,
        step_id=validated.tool_request_id,
        tool_id=context.tool_definition.tool_id,
        manifest_id=context.manifest_id,
        execution_session_id=validated.execution_session_id,
        cwd=context.execution_session.cwd_path,
        timeout_ms=None,
        intent_class=context.tool_definition.intent_class,
        network_policy=context.execution_session.network_policy,
        command_summary_json=validated.command_summary_json,
        capability_snapshot_json=context.execution_session.capability_snapshot_json,
        request_json=args,
        status="running",
        started_at=validated.requested_at,
    )


def _build_read_only_invocation_start(
    *,
    context: BrokerContext,
    validated: DirectReadOnlyRequest,
    args: dict[str, JSONValue],
) -> ToolInvocationStartInput:
    if validated.tool_invocation_id is None:
        raise BrokerPolicyError("direct broker execution requires an invocation id")
    return ToolInvocationStartInput(
        invocation_id=validated.tool_invocation_id,
        tool_request_id=validated.tool_request_id,
        user_id=context.execution_session.user_id,
        action_id=validated.action_id,
        step_id=validated.tool_request_id,
        tool_id=context.tool_definition.tool_id,
        manifest_id=context.manifest_id,
        execution_session_id=validated.execution_session_id,
        cwd=context.execution_session.cwd_path,
        timeout_ms=None,
        intent_class=context.tool_definition.intent_class,
        network_policy=context.execution_session.network_policy,
        command_summary_json=None,
        capability_snapshot_json=context.execution_session.capability_snapshot_json,
        request_json=args,
        status="running",
        started_at=validated.requested_at,
    )


def _command_request_json_for_audit(
    *,
    validated: ValidatedCommandRequest,
    args: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    if validated.generated_python_code is None:
        return args
    return build_run_python_request_audit_args(
        args=args,
        generated_python_code=validated.generated_python_code,
    )


__all__ = ["start_direct_execution"]
