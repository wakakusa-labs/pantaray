from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import ValidationError

from pantaray_agents.schema.agent.base import JSONValue

from .audit_payloads import build_run_python_request_audit_args
from .bootstrap import resolve_tool_audit_metadata
from .brokering.broker_common import (
    APPLY_PATCH_TOOL_ID,
    BASH_TOOL_ID,
    GLOB_TOOL_ID,
    GREP_TOOL_ID,
    LIST_TOOL_ID,
    READ_TOOL_ID,
    RENDER_PDF_PAGE_TOOL_ID,
    RUN_PYTHON_TOOL_ID,
    BrokerPolicyError,
)
from .brokering.broker_protocol import ApplyPatchToolArgs
from .brokering.broker_structured_patch import extract_structured_patch_paths
from .brokering.command_approval_summaries import (
    build_apply_patch_summary,
    build_bash_summary,
    build_run_python_summary,
)
from .models import (
    ToolInvocationStartInput,
)
from .repository import (
    load_execution_session,
    load_tool_definition,
    record_tool_invocation_start,
)
from .tool_result_finalization import (
    TIMEOUT_CLEANUP_EVENT_TYPE,
    TIMEOUT_TOOL_OUTPUT_ERROR_TYPE,
)

BROKERED_TOOL_IDS = frozenset(
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


def _resolve_audit_cwd(*, tool_id: str, args: dict[str, JSONValue]) -> str | None:
    if tool_id not in {BASH_TOOL_ID, RUN_PYTHON_TOOL_ID}:
        return None
    cwd_value = args.get("cwd")
    if cwd_value is None:
        return "."
    if isinstance(cwd_value, str) and cwd_value.strip():
        return cwd_value
    raise RuntimeError("validated bash.cwd must be a non-empty string")


def _resolve_audit_timeout_ms(
    *,
    tool_id: str,
    args: dict[str, JSONValue],
    default_timeout_ms: int | None,
) -> int | None:
    del args
    if tool_id != "bash":
        return default_timeout_ms
    return default_timeout_ms


def _build_request_json_for_audit(
    *,
    tool_id: str,
    args: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    if tool_id == RUN_PYTHON_TOOL_ID:
        return {"args": build_run_python_request_audit_args(args=args)}
    return {"args": dict(args)}


def _build_command_summary_json_for_audit(
    *,
    tool_id: str,
    args: dict[str, JSONValue],
    default_timeout_ms: int | None,
) -> dict[str, JSONValue] | None:
    try:
        return _build_valid_command_summary_json_for_audit(
            tool_id=tool_id,
            args=args,
            default_timeout_ms=default_timeout_ms,
        )
    except (ValidationError, BrokerPolicyError, TypeError, ValueError):
        return None


def _build_valid_command_summary_json_for_audit(
    *,
    tool_id: str,
    args: dict[str, JSONValue],
    default_timeout_ms: int | None,
) -> dict[str, JSONValue] | None:
    if tool_id == APPLY_PATCH_TOOL_ID:
        request = ApplyPatchToolArgs.model_validate(args)
        return build_apply_patch_summary(
            patch_paths=extract_structured_patch_paths(request.changes)
        )
    if tool_id == BASH_TOOL_ID:
        command = args.get("command")
        if not isinstance(command, str):
            return None
        cwd = args.get("cwd")
        return build_bash_summary(
            command=command,
            cwd_relative_path=cwd if isinstance(cwd, str) and cwd.strip() else ".",
            timeout_ms=default_timeout_ms or 0,
            use_login_environment=args.get("use_login_environment") is True,
            run_outside_sandbox=args.get("run_outside_sandbox") is True,
            reason=_justification(args),
        )
    if tool_id == RUN_PYTHON_TOOL_ID:
        code = args.get("code")
        run_args = args.get("args")
        cwd = args.get("cwd")
        if not isinstance(code, str):
            return None
        return build_run_python_summary(
            cwd_relative_path=cwd if isinstance(cwd, str) and cwd.strip() else ".",
            code=code,
            args_count=len(run_args) if isinstance(run_args, list) else 0,
            timeout_ms=default_timeout_ms or 0,
            reason=_justification(args),
        )
    return None


def _justification(args: dict[str, JSONValue]) -> str | None:
    justification = args.get("justification")
    return justification if isinstance(justification, str) else None


def start_local_tool_invocation_audit(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    step_id: str,
    tool_id: str,
    manifest_id: str,
    execution_session_id: str,
    args: dict[str, JSONValue],
    tool_request_id: str | None,
    started_at: str,
) -> str:
    execution_session = load_execution_session(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        execution_session_id=execution_session_id,
    )
    stored_definition = load_tool_definition(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_id=tool_id,
    )
    audit_metadata = resolve_tool_audit_metadata(
        stored_definition,
        session_network_policy=execution_session.network_policy,
    )
    invocation_id = str(uuid.uuid4())
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=tool_request_id,
            user_id=user_id,
            action_id=action_id,
            step_id=step_id,
            tool_id=tool_id,
            manifest_id=manifest_id,
            execution_session_id=execution_session_id,
            cwd=_resolve_audit_cwd(tool_id=tool_id, args=args),
            timeout_ms=_resolve_audit_timeout_ms(
                tool_id=tool_id,
                args=args,
                default_timeout_ms=audit_metadata.default_timeout_ms,
            ),
            intent_class=audit_metadata.intent_class,
            network_policy=audit_metadata.network_policy,
            command_summary_json=_build_command_summary_json_for_audit(
                tool_id=tool_id,
                args=args,
                default_timeout_ms=audit_metadata.default_timeout_ms,
            ),
            capability_snapshot_json={
                "required_capabilities": list(audit_metadata.required_capabilities)
            },
            request_json=_build_request_json_for_audit(tool_id=tool_id, args=args),
            status="queued" if tool_id in BROKERED_TOOL_IDS else "running",
            started_at=started_at,
        ),
    )
    return invocation_id


__all__ = [
    "BROKERED_TOOL_IDS",
    "TIMEOUT_CLEANUP_EVENT_TYPE",
    "TIMEOUT_TOOL_OUTPUT_ERROR_TYPE",
    "start_local_tool_invocation_audit",
]
