"""Dispatch for the consent-gated tools that are not brokered.

``capture_screen`` and ``integration_run_tool`` touch no workspace path, but they
need the brokered tools' "settle consent, then run" preflight, so they share that
identity and pause machinery instead of growing a second approval path.
"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.tools import (
    CAPTURE_SCREEN_TOOL_ID,
    INTEGRATION_RUN_TOOL_TOOL_ID,
    ToolDefinition,
)

from .capture_screen import run_capture_screen_preflight, run_capture_screen_tool
from .integration_run import run_integration_run_preflight, run_integration_run_tool
from .shared import (
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
    ValidatedToolArgs,
)

CONSENT_GATED_TOOL_IDS = frozenset(
    {CAPTURE_SCREEN_TOOL_ID, INTEGRATION_RUN_TOOL_TOOL_ID}
)


async def run_consent_gated_preflight(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ValidatedToolArgs,
    tool_request_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    if tool_def.tool_id == CAPTURE_SCREEN_TOOL_ID:
        return await run_capture_screen_preflight(
            step_id=step_id,
            tool_def=tool_def,
            state=state,
            # The input schema requires a non-blank string.
            app_name=str(args["app_name"]),
            tool_request_id=tool_request_id,
            requested_at=requested_at,
        )
    return await run_integration_run_preflight(
        step_id=step_id,
        tool_def=tool_def,
        state=state,
        args=args,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
    )


async def run_consent_gated_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ValidatedToolArgs,
    tool_request_id: str,
    tool_invocation_id: str | None,
    requested_at: str,
) -> UnprojectedToolExecutionResult:
    if tool_invocation_id is None:
        # The claim that makes one approval run one call binds to this row, so a
        # call without it would be unclaimable.
        raise RuntimeError(f"{tool_def.tool_id} requires a tool invocation audit")
    if tool_def.tool_id == CAPTURE_SCREEN_TOOL_ID:
        return await run_capture_screen_tool(
            step_id=step_id,
            tool_def=tool_def,
            state=state,
            app_name=str(args["app_name"]),
            tool_request_id=tool_request_id,
            tool_invocation_id=tool_invocation_id,
            requested_at=requested_at,
        )
    return await run_integration_run_tool(
        step_id=step_id,
        tool_def=tool_def,
        state=state,
        args=args,
        tool_request_id=tool_request_id,
        tool_invocation_id=tool_invocation_id,
        requested_at=requested_at,
    )
