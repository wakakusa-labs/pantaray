"""One Action subagent's job: a private conversation on the shared loop.

The child runs on ``run_conversation`` behind its parent's frozen context, and
stores every item it appends as a row of its own process events
(``action_subagent_history``), so a pause, a restart or a crash resumes the
same conversation. What the parent sees is only the terminal result.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.turn_input import (
    SUBAGENT_ROLE,
    role_system_instruction,
)
from pantaray_agents.agents.action_agent.tools import (
    SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    WRITE_SESSION_MEMORY_TOOL,
    WRITE_SESSION_MEMORY_TOOL_ID,
)
from pantaray_agents.agents.action_agent.tools.session_memory_tool import (
    write_session_memory,
)
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    ActionTurnReply,
    LlmToolUseMixin,
)
from pantaray_agents.agents.core.tool_llm_runner import ToolLlmRunner
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation import provider_turns
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationRequest,
    ConversationRun,
    Finish,
    IdleTurn,
    run_conversation,
)
from pantaray_agents.local_runtime.llm_proxy import build_local_llm_proxy_client
from pantaray_agents.local_runtime.runtime.action_subagent_approval import (
    load_pending_action_subagent_approval,
)
from pantaray_agents.local_runtime.runtime.action_subagent_broker_authority import (
    ActionSubagentBrokerAuthority,
    load_action_subagent_broker_authority,
)
from pantaray_agents.local_runtime.runtime.action_subagent_cancel import (
    action_subagent_cancellation_requested,
)
from pantaray_agents.local_runtime.runtime.action_subagent_pause import (
    ActionSubagentApprovalPause,
    pause_action_subagent_for_approval,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    ACTION_SUBAGENT_REPORT_MAX_BYTES,
    ActionSubagentReportError,
    ActionSubagentTerminalResult,
    ActionSubagentTerminalSuccess,
    build_action_subagent_canceled_result,
    build_action_subagent_failure_result,
    build_action_subagent_success_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    get_local_runtime_db_execution_context,
)
from pantaray_agents.local_runtime.runtime.job_executor import (
    persist_local_job_transition_with_retry,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    tool_error_response,
)
from pantaray_agents.utils.prompt_loader import load_config
from pantaray_llm.contracts.tool_use import LlmToolDefinition
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

from .action_subagent_broker import (
    build_action_subagent_broker_tools,
    execute_action_subagent_broker_tool,
)
from .action_subagent_history import (
    AgentsMdClaims,
    SubagentHistoryWriter,
    load_subagent_history,
)

ACTION_SUBAGENT_PROFILE_UNAVAILABLE = "ACTION_SUBAGENT_PROFILE_UNAVAILABLE"
ACTION_SUBAGENT_EXECUTION_FAILED = "ACTION_SUBAGENT_EXECUTION_FAILED"

_CONFIGURED_PROFILE_IDS = frozenset(
    settings.profile_id for settings in SUBAGENT_MODEL_SETTINGS
)
_ACTION_SUBAGENT_MAX_REPORT_REPAIRS = 2
# The bounds the child ran under on the generic ReAct loop.
_MAX_TURNS = 100
_MAX_TOOL_CALLS = 100
_REPORT_REQUIRED = (
    "Respond with tool calls. When the task is done, or cannot be done, call "
    f"`{SUBMIT_SUBAGENT_REPORT_TOOL_ID}` alone with your report."
)


class ActionSubagentJobFailed(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class _ActionSubagentCancellationObserved(RuntimeError):
    pass


class _ActionSubagentToolLlmRunner(LlmToolUseMixin, ToolLlmRunner):
    pass


async def execute_action_subagent_job(
    *,
    payload: ActionSubagentJobPayload,
    runner: _ActionSubagentToolLlmRunner,
    db_path: Path,
    busy_timeout_ms: int,
    broker_authority: ActionSubagentBrokerAuthority,
) -> ActionSubagentTerminalSuccess:
    sink = CountingSink()
    tunables = load_local_runtime_tunables().action_agent
    check_cancel = functools.partial(
        _raise_if_cancellation_requested,
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        payload=payload,
    )
    load_history = functools.partial(
        load_subagent_history,
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=payload["process_id"],
        assigned_task=_assigned_task_message(payload),
        window_tokens=tunables.context_window_tokens,
    )
    history = load_history()
    agents_md = AgentsMdClaims(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, payload=payload
    )
    agents_md.restore(history)
    writer = SubagentHistoryWriter(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        payload=payload,
        agents_md=agents_md,
    )
    rejected_reports = 0

    resumed = load_pending_action_subagent_approval(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=payload["process_id"],
    )
    if resumed is not None:
        # The saved gated request settles exactly once: the broker either runs
        # the approved call or reports the denial, which answers the call left
        # waiting.
        settled = await execute_action_subagent_broker_tool(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            authority=broker_authority,
            tool_id=resumed.tool_id,
            args=resumed.arguments,
            tool_request_id=resumed.tool_request_id,
            call_id=resumed.call_id,
            agents_md=agents_md,
        )
        writer.answer_waiting_call(resumed.call_id, resumed.tool_id, settled)
        history = load_history()
        agents_md.restore(history)
    entries = await writer.answer_unanswered(history)
    identity, _ = provider_turns.read_provider_turn_target(
        inference_profile=payload["inference_profile_id"]
    )
    store = provider_turns.ProviderTurnStore(
        identity=identity,
        turns={
            turn_id: record
            for turn_id, record in history.turns.items()
            if record.identity == identity
        },
    )

    async def send(request: ConversationRequest) -> ActionTurnReply:
        reply = await runner._generate_llm_action_turn(
            sink=sink,
            prompt=request.prompt,
            tools=request.tools,
            max_parallel_tool_calls=request.max_parallel_tool_calls,
            system_instruction=request.system_instruction,
            conversation=request.conversation,
            before_attempt=check_cancel,
        )
        check_cancel()
        return reply

    def decide(turn: IdleTurn) -> Finish[ActionSubagentTerminalSuccess] | Continue:
        nonlocal rejected_reports
        if turn.ending_call is None:
            return Continue(_REPORT_REQUIRED)
        report = turn.ending_call.arguments.get("report")
        try:
            if not isinstance(report, str):
                raise ActionSubagentReportError(
                    "submit_subagent_report requires a string report"
                )
            return Finish(build_action_subagent_success_result(report))
        except ActionSubagentReportError as exc:
            rejected_reports += 1
            if rejected_reports > _ACTION_SUBAGENT_MAX_REPORT_REPAIRS:
                raise ActionSubagentJobFailed(ACTION_SUBAGENT_EXECUTION_FAILED) from exc
            return Continue(str(exc))

    tools = build_action_subagent_broker_tools(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        payload=payload,
        authority=broker_authority,
        agents_md=agents_md,
    )
    return await run_conversation(
        ConversationRun(
            # The parent's frozen head, byte for byte on every turn and for every
            # child of that parent: the cache prefix the task and the history
            # grow behind.
            prompt=payload["action_context"],
            system_instruction=_subagent_system_instruction(),
            tools=tuple(
                _checked(tool, check_cancel) for tool in (*tools, _SESSION_MEMORY_TOOL)
            ),
            ending_tools=(_report_tool_definition(),),
            history=entries,
            provider_turns=store,
            inference_profile=payload["inference_profile_id"],
            max_turns=_MAX_TURNS,
            max_tool_calls=_MAX_TOOL_CALLS,
            max_parallel_tool_calls=tunables.max_parallel_tool_calls,
            window=history.window,
            usage=lambda: sink.delta,
            send=send,
            before_send=writer.before_send,
            on_turn=writer.on_turn,
            on_result=writer.on_result,
            on_notice=writer.on_notice,
            decide=decide,
        )
    )


def run_action_subagent_job(payload: ActionSubagentJobPayload) -> None:
    db_context = get_local_runtime_db_execution_context()
    if db_context is None:
        raise RuntimeError(
            "Action subagent requires local runtime DB execution context"
        )

    try:
        _raise_if_cancellation_requested(
            db_path=db_context.db_path,
            busy_timeout_ms=db_context.busy_timeout_ms,
            payload=payload,
        )
        _require_configured_profile(payload["inference_profile_id"])
        broker_authority = load_action_subagent_broker_authority(
            db_path=db_context.db_path,
            busy_timeout_ms=db_context.busy_timeout_ms,
            payload=payload,
        )
        runner = _ActionSubagentToolLlmRunner(
            client=build_local_llm_proxy_client(),
            llm_config={},
            default_system_instruction=_subagent_system_instruction(),
            error_code_prefix="ACTION_SUBAGENT",
            llm_inference_profile_id=payload["inference_profile_id"],
        )
        result = asyncio.run(
            execute_action_subagent_job(
                payload=payload,
                runner=runner,
                db_path=db_context.db_path,
                busy_timeout_ms=db_context.busy_timeout_ms,
                broker_authority=broker_authority,
            )
        )
    except ActionSubagentApprovalPause as pause:
        _persist_pause_until_success(
            db_path=db_context.db_path,
            busy_timeout_ms=db_context.busy_timeout_ms,
            payload=payload,
            pause=pause,
        )
        return
    except _ActionSubagentCancellationObserved:
        _persist_terminal_until_success(
            db_path=db_context.db_path,
            busy_timeout_ms=db_context.busy_timeout_ms,
            payload=payload,
            result=build_action_subagent_canceled_result(),
        )
        return
    except Exception as exc:
        error_code = _execution_error_code(exc)
        winner = _persist_terminal_until_success(
            db_path=db_context.db_path,
            busy_timeout_ms=db_context.busy_timeout_ms,
            payload=payload,
            result=build_action_subagent_failure_result(error_code),
        )
        if winner["outcome"] == "canceled":
            return
        raise ActionSubagentJobFailed(error_code) from exc

    _persist_terminal_until_success(
        db_path=db_context.db_path,
        busy_timeout_ms=db_context.busy_timeout_ms,
        payload=payload,
        result=result,
    )


def _require_configured_profile(profile_id: str) -> None:
    if profile_id not in _CONFIGURED_PROFILE_IDS:
        raise ActionSubagentJobFailed(ACTION_SUBAGENT_PROFILE_UNAVAILABLE)


def _raise_if_cancellation_requested(
    *, db_path: Path, busy_timeout_ms: int, payload: ActionSubagentJobPayload
) -> None:
    if action_subagent_cancellation_requested(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        payload=payload,
    ):
        raise _ActionSubagentCancellationObserved


def _execution_error_code(exc: Exception) -> str:
    if isinstance(exc, ActionSubagentJobFailed):
        return exc.error_code
    if isinstance(exc, LlmProxyExecutionError):
        return exc.error_code
    return ACTION_SUBAGENT_EXECUTION_FAILED


def _persist_pause_until_success(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    pause: ActionSubagentApprovalPause,
) -> None:
    persist_local_job_transition_with_retry(
        operation=lambda: pause_action_subagent_for_approval(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            pause=pause,
        ),
        retry_event="LOCAL_ACTION_SUBAGENT_APPROVAL_PAUSE_RETRYING",
    )


def _persist_terminal_until_success(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    result: ActionSubagentTerminalResult,
) -> ActionSubagentTerminalResult:
    return persist_local_job_transition_with_retry(
        operation=lambda: finalize_action_subagent_terminal(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            result=result,
        ),
        retry_event="LOCAL_ACTION_SUBAGENT_TERMINAL_RETRYING",
    )


def _report_tool_definition() -> LlmToolDefinition:
    return LlmToolDefinition(
        name=SUBMIT_SUBAGENT_REPORT_TOOL_ID,
        description="Submit the final private report to the parent agent.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "report": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ACTION_SUBAGENT_REPORT_MAX_BYTES,
                }
            },
            "required": ["report"],
        },
    )


async def _write_session_memory(call: ReactToolCall, _step: int) -> ReactToolResult:
    # The child's own notes, which only its conversation keeps: the parent's
    # tool writes a step of the Action, which this never does.
    try:
        output = write_session_memory(str(call.tool_call_envelope.args["content"]))
    except ValueError as exc:
        return tool_error_response(
            tool_name=WRITE_SESSION_MEMORY_TOOL_ID,
            error_code="SESSION_MEMORY_TOO_LARGE",
            message=str(exc),
        )
    return ReactToolResult(
        tool_name=WRITE_SESSION_MEMORY_TOOL_ID, status="success", output=output
    )


_SESSION_MEMORY_TOOL = ReactToolDefinition(
    name=WRITE_SESSION_MEMORY_TOOL_ID,
    description=WRITE_SESSION_MEMORY_TOOL.prompt_contract.description
    or WRITE_SESSION_MEMORY_TOOL.description,
    request_schema=WRITE_SESSION_MEMORY_TOOL.build_validation_input_schema(),
    response_schema=react_tool_response_schema(
        success_schema=dict(WRITE_SESSION_MEMORY_TOOL.output_schema)
    ),
    execute=_write_session_memory,
    concurrency=WRITE_SESSION_MEMORY_TOOL.concurrency,
)


def _checked(
    tool: ReactToolDefinition, check_cancel: Callable[[], None]
) -> ReactToolDefinition:
    """The tool, run only while no cancel has been requested."""

    async def execute(call: ReactToolCall, step_number: int) -> ReactToolResult:
        check_cancel()
        return await tool.execute(call, step_number)

    return replace(tool, execute=execute)


def _subagent_system_instruction() -> str:
    """The Supervisor's system instruction with the subagent's role section."""

    config = load_config(ActionAgent.EXECUTING_PROMPT_NAME)
    if config.system_instruction is None:
        raise RuntimeError("The Action prompt has no system instruction")
    return role_system_instruction(
        config.system_instruction,
        role_rule=config.require_role_rule(SUBAGENT_ROLE),
    )


def _assigned_task_message(payload: ActionSubagentJobPayload) -> str:
    context_refs = json.dumps(payload["context_refs"], ensure_ascii=False)
    return f"# Assigned Task\n{payload['task']}\n\n# Context References\n{context_refs}"
