from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

from pantaray_agents.agents.action_agent.runtime.agents_md import (
    attach_repository_agents_md,
)
from pantaray_agents.agents.action_agent.tools.apply_patch_tool import APPLY_PATCH_TOOL
from pantaray_agents.agents.action_agent.tools.base import ToolDefinition
from pantaray_agents.agents.action_agent.tools.bash_tool import (
    ACTION_SUBAGENT_BASH_TOOL,
)
from pantaray_agents.agents.action_agent.tools.discovery_tools import (
    GLOB_TOOL,
    GREP_TOOL,
    LIST_TOOL,
)
from pantaray_agents.agents.action_agent.tools.read_tool import READ_TOOL
from pantaray_agents.agents.action_agent.tools.run_python_tool import RUN_PYTHON_TOOL
from pantaray_agents.local_runtime.runtime.action_subagent_broker_authority import (
    ActionSubagentBrokerAuthority,
)
from pantaray_agents.local_runtime.runtime.action_subagent_pause import (
    ActionSubagentApprovalPause,
    next_action_subagent_tool_request_id,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerExecutionError,
    BrokerToolOutcome,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    READ_TOOL_ID,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.repository.approval_sessions import (
    load_approval_session_by_request,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs.action_subagent_history import (
    AgentsMdClaims,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.tools.contract import (
    BrokerPolicyError,
    JsonSchema,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    tool_error_response,
)

_CHILD_BROKER_TOOLS = (
    READ_TOOL,
    LIST_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    APPLY_PATCH_TOOL,
    ACTION_SUBAGENT_BASH_TOOL,
    RUN_PYTHON_TOOL,
)
APPROVAL_DENIED_MESSAGE = (
    "The requested tool call was denied by the user. "
    "Treat this tool call as skipped and continue."
)
_ACTION_FILE_JSON_RESULT_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "storage",
        "path",
        "media_type",
        "byte_size",
        "character_count",
        "line_count",
    ],
    "properties": {
        "storage": {"type": "string", "enum": ["action_file"]},
        "path": {"type": "string", "minLength": 1},
        "media_type": {"type": "string", "enum": ["application/json"]},
        "byte_size": {"type": "integer", "minimum": 0},
        "character_count": {"type": "integer", "minimum": 0},
        "line_count": {"type": "integer", "minimum": 1},
        "preview": {"type": "string"},
        "retry_hint": {"type": "string"},
        "status": {"type": "string"},
    },
}
_APPROVAL_DENIED_RESULT_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "tool_id", "executed", "message"],
    "properties": {
        "kind": {"type": "string", "enum": ["approval_denied"]},
        "tool_id": {"type": "string", "minLength": 1},
        "executed": {"type": "boolean"},
        "message": {"type": "string", "minLength": 1},
    },
}


def build_action_subagent_broker_tools(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    authority: ActionSubagentBrokerAuthority,
    agents_md: AgentsMdClaims,
) -> tuple[ReactToolDefinition, ...]:
    return tuple(
        _build_tool(
            definition,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            authority=authority,
            agents_md=agents_md,
        )
        for definition in _CHILD_BROKER_TOOLS
    )


async def execute_action_subagent_broker_tool(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    authority: ActionSubagentBrokerAuthority,
    tool_id: str,
    args: dict[str, JSONValue],
    tool_request_id: str,
    call_id: str,
    agents_md: AgentsMdClaims,
) -> ReactToolResult:
    """Run one child broker call, turning approval control into a typed pause."""

    try:
        outcome = await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=tool_id,
            user_id=payload["user_id"],
            actor_process_id=payload["process_id"],
            manifest_id=authority.manifest_id,
            execution_session_id=authority.execution_session_id,
            args=args,
            tool_request_id=tool_request_id,
            requested_at=now_utc_iso(),
        )
    except BrokerApprovalRequiredError as exc:
        raise _approval_pause(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            tool_id=tool_id,
            args=args,
            tool_request_id=tool_request_id,
            approval_session_id=exc.approval_session_id,
            call_id=call_id,
        ) from exc
    except BrokerApprovalDeniedError:
        return ReactToolResult(
            tool_name=tool_id,
            status="success",
            output={
                "kind": "approval_denied",
                "tool_id": tool_id,
                "executed": False,
                "message": APPROVAL_DENIED_MESSAGE,
            },
        )
    except (BrokerPolicyError, BrokerExecutionError) as exc:
        return tool_error_response(
            tool_name=tool_id,
            error_code=getattr(exc, "code", exc.__class__.__name__),
            message=str(exc),
        )
    if isinstance(outcome, BrokerToolOutcome) and outcome.attachments:
        # Attachment bytes travel only on the broker outcome, and the child's
        # durable transcript carries output alone. Fail instead of handing the
        # child a success message it cannot back with the actual content.
        return tool_error_response(
            tool_name=tool_id,
            error_code="SUBAGENT_ATTACHMENT_READ_UNSUPPORTED",
            message=(
                "Image contents cannot be delivered to a subagent. "
                "Report the file path so the parent agent can read it, and "
                "continue with text paths such as grep, list, or text read."
            ),
        )
    if outcome.status == "error":
        return tool_error_response(
            tool_name=tool_id,
            error_code=(
                outcome.failure.error_type if outcome.failure else "TOOL_FAILED"
            ),
            message=_bounded_failure_message(outcome),
        )
    if isinstance(outcome, BrokerToolOutcome) and outcome.failure is None:
        _claim_agents_md(
            agents_md,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            authority=authority,
            tool_id=tool_id,
            args=args,
        )
    return ReactToolResult(
        tool_name=tool_id,
        status="success",
        output=outcome.output,
    )


def _claim_agents_md(
    agents_md: AgentsMdClaims,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    authority: ActionSubagentBrokerAuthority,
    tool_id: str,
    args: dict[str, JSONValue],
) -> None:
    """Claim the repository AGENTS.md files this call is the first to reach."""

    try:
        read_context = load_broker_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=READ_TOOL_ID,
            path_access_kind="read",
            user_id=payload["user_id"],
            actor_process_id=payload["process_id"],
            manifest_id=authority.manifest_id,
            execution_session_id=authority.execution_session_id,
        )
    except BrokerPolicyError:
        # A child that may not read files gets no AGENTS.md either.
        return
    agents_md.claim(
        lambda attached: attach_repository_agents_md(
            attached, tool_id=tool_id, args=args, read_context=read_context
        )
    )


def _bounded_failure_message(outcome: BrokerToolOutcome) -> str:
    """Bound the failure text the child transcript replays on every later turn.

    ``BrokerToolOutcome.failure`` is derived from the unprojected tool output, so
    a failed command carries its whole raw stderr while ``outcome.output`` was
    already projected. The parent keeps that projected output and uses the
    failure only as control-flow identity; the child inlines the text, so it is
    held to the same durable inline bound.
    """

    message = outcome.failure.message if outcome.failure else "Tool failed."
    head = message[:ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT]
    return head if len(head) == len(message) else f"{head}\n[truncated]"


def _approval_pause(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    tool_id: str,
    args: dict[str, JSONValue],
    tool_request_id: str,
    approval_session_id: str,
    call_id: str,
) -> ActionSubagentApprovalPause:
    session = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=payload["user_id"],
        tool_request_id=tool_request_id,
    )
    if session is None:
        raise BrokerPolicyError(
            "approval-required child tool result is missing its approval session"
        )
    return ActionSubagentApprovalPause(
        tool_id=tool_id,
        arguments=args,
        tool_request_id=tool_request_id,
        approval_session_id=approval_session_id,
        call_id=call_id,
        intent_class=session.intent_class,
        command_summary=dict(session.command_summary_json),
    )


def _build_tool(
    definition: ToolDefinition,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    authority: ActionSubagentBrokerAuthority,
    agents_md: AgentsMdClaims,
) -> ReactToolDefinition:
    async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
        if call.call_id is None:
            raise RuntimeError("A child tool runs only on the conversation loop")
        # The durable identity is the next history row, which calls running at
        # once would share. Only read-only calls run at once, and they need no
        # replay after a restart, so each gets an identity of its own; a
        # changing call keeps the durable one that lets a restart replay it.
        tool_request_id = (
            str(uuid.uuid4())
            if definition.concurrency.placement == "parallel"
            else next_action_subagent_tool_request_id(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                process_id=payload["process_id"],
                call_id=call.call_id,
            )
        )
        return await execute_action_subagent_broker_tool(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            authority=authority,
            tool_id=definition.tool_id,
            args=cast(dict[str, JSONValue], call.tool_args),
            tool_request_id=tool_request_id,
            call_id=call.call_id,
            agents_md=agents_md,
        )

    return ReactToolDefinition(
        name=definition.tool_id,
        description=definition.prompt_contract.description or definition.description,
        request_schema=definition.build_validation_input_schema(),
        response_schema=react_tool_response_schema(
            success_schema={
                "oneOf": [
                    dict(definition.output_schema),
                    dict(_ACTION_FILE_JSON_RESULT_SCHEMA),
                    dict(_APPROVAL_DENIED_RESULT_SCHEMA),
                ]
            }
        ),
        execute=execute,
        concurrency=definition.concurrency,
    )
