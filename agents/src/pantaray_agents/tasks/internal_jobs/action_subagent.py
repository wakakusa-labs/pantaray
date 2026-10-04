from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.turn_input import (
    SUBAGENT_ROLE,
    role_system_instruction,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    EXCLUSION_NOTICES,
    PROVIDER_DROPPED_NOTICE,
    plan_tool_batch,
)
from pantaray_agents.agents.action_agent.tools import SUBMIT_SUBAGENT_REPORT_TOOL_ID
from pantaray_agents.agents.artifact_react import (
    NativeReactCompletion,
    NativeReactRunInput,
    NativeReactSkippedCall,
    NativeReactTurnInterrupt,
    NativeReactTurnPlan,
    ReactLoopPolicy,
    ReactLoopStep,
    ReactToolResult,
    run_native_react,
)
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    LlmToolCallTurn,
    LlmToolUseMixin,
)
from pantaray_agents.agents.core.tool_llm_runner import ToolLlmRunner
from pantaray_agents.config_tunables import load_local_runtime_tunables
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
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    append_action_subagent_tool_transcript,
    build_action_subagent_conversation,
    load_action_subagent_transcript,
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
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.utils.prompt_loader import load_config
from pantaray_llm.contracts.conversation import LlmConversation
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

from .action_subagent_broker import (
    build_action_subagent_broker_tools,
    execute_action_subagent_broker_tool,
)

ACTION_SUBAGENT_PROFILE_UNAVAILABLE = "ACTION_SUBAGENT_PROFILE_UNAVAILABLE"
ACTION_SUBAGENT_EXECUTION_FAILED = "ACTION_SUBAGENT_EXECUTION_FAILED"

_CONFIGURED_PROFILE_IDS = frozenset(
    settings.profile_id for settings in SUBAGENT_MODEL_SETTINGS
)
_ACTION_SUBAGENT_MAX_REPORT_REPAIRS = 2


@dataclass(frozen=True, slots=True)
class _PlannedCall:
    """One requested call, in the shape the parent's batch policy reads."""

    call: LlmToolCall

    @property
    def tool_id(self) -> str:
        return self.call.name


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
    terminal_tool = _report_tool_definition()
    max_parallel = load_local_runtime_tunables().action_agent.max_parallel_tool_calls
    rejected_reports = 0
    turn_conversation: LlmConversation | None = None

    async def call_llm(
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        _continuation: LlmToolContinuation | None,
        _tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        # Every turn is rebuilt from process_events so that a message the parent
        # sends mid-run, and a restart, both reach the model. That leaves no turn
        # on which a continuation could be replayed, so asking for one would only
        # make the adapter assemble state this loop drops.
        turn = await runner._generate_llm_tool_call(
            sink=sink,
            prompt=prompt,
            tools=tools,
            continuation_mode="disabled",
            conversation=turn_conversation,
            max_parallel_tool_calls=max_parallel,
            before_attempt=lambda: _raise_if_cancellation_requested(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                payload=payload,
            ),
        )
        _raise_if_cancellation_requested(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
        )
        return turn

    async def record_tool_step(step: ReactLoopStep) -> None:
        if step.step_kind != "tool" or step.status == "processing":
            return
        if step.tool_name is None:
            raise RuntimeError("completed Action subagent Tool step requires a name")
        append_action_subagent_tool_transcript(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=payload["user_id"],
            action_id=payload["action_id"],
            process_id=payload["process_id"],
            job_id=payload["job_id"],
            tool_name=step.tool_name,
            status="completed" if step.status == "success" else "error",
            arguments=(
                {}
                if step.tool_name == SUBMIT_SUBAGENT_REPORT_TOOL_ID
                and step.status == "error"
                else step.tool_args
            ),
            output=step.tool_output,
            error_message=step.error_message,
            completed_at=now_utc_iso(),
        )

    def build_turn_input(
        _tool_results: tuple[ReactToolResult, ...], last_error: str | None
    ) -> str:
        # The loop calls this immediately before ``call_llm`` and nothing runs
        # between them, so the items recorded here are the ones that turn sends.
        nonlocal turn_conversation
        entries = load_action_subagent_transcript(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            process_id=payload["process_id"],
        )
        turn_conversation = build_action_subagent_conversation(
            entries,
            assigned_task=_assigned_task_message(payload),
            repair_notice=last_error,
        )
        # The parent's frozen head, byte for byte on every turn and for every
        # child of that parent: the cache prefix the task and the transcript
        # grow behind.
        return payload["action_context"]

    async def project_result(result: ReactToolResult) -> ReactToolResult:
        _raise_if_cancellation_requested(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
        )
        return result

    def complete(
        arguments: dict[str, JSONValue],
        _final_turn: bool,
    ) -> NativeReactCompletion[ActionSubagentTerminalSuccess]:
        nonlocal rejected_reports
        report = arguments.get("report")
        try:
            if not isinstance(report, str):
                raise ActionSubagentReportError(
                    "submit_subagent_report requires a string report"
                )
            terminal_result = build_action_subagent_success_result(report)
        except ActionSubagentReportError as exc:
            rejected_reports += 1
            if rejected_reports > _ACTION_SUBAGENT_MAX_REPORT_REPAIRS:
                raise ActionSubagentJobFailed(ACTION_SUBAGENT_EXECUTION_FAILED) from exc
            return NativeReactCompletion(
                value=None,
                final_text="",
                error_message=str(exc),
            )
        return NativeReactCompletion(value=terminal_result, final_text="")

    resumed = load_pending_action_subagent_approval(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=payload["process_id"],
    )
    if resumed is not None:
        # The saved gated request settles exactly once: the broker either runs
        # the approved call or reports the denial as one skipped result.
        settled = await execute_action_subagent_broker_tool(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            authority=broker_authority,
            tool_id=resumed.tool_id,
            args=resumed.arguments,
            tool_request_id=resumed.tool_request_id,
        )
        await record_tool_step(
            ReactLoopStep(
                run_id=payload["process_id"],
                step_number=1,
                step_kind="tool",
                status=settled.status,
                tool_name=resumed.tool_id,
                tool_args=resumed.arguments,
                tool_output=settled.output,
                error_message=settled.error_message,
            )
        )
    result = await run_native_react(
        NativeReactRunInput(
            run_id=payload["process_id"],
            tool_definitions=build_action_subagent_broker_tools(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                payload=payload,
                authority=broker_authority,
            ),
            terminal_tool=terminal_tool,
            complete=complete,
            build_prompt=build_turn_input,
            call_llm=call_llm,
            record_step=record_tool_step,
            project_tool_result=project_result,
            policy=ReactLoopPolicy(),
            plan_turn=lambda turn, remaining: _plan_turn(
                turn, max_parallel=max_parallel, remaining_tool_calls=remaining
            ),
            # The pause anchor holds only the call that asked; the resumed run
            # settles it, so the calls after it are answered as not run now.
            turn_interrupt=NativeReactTurnInterrupt(
                exception=ActionSubagentApprovalPause,
                not_run_reason=_not_run_reason(
                    "came after a call that waited for the user's approval"
                ),
            ),
        )
    )
    if result.loop_result.status != "success" or result.value is None:
        raise ActionSubagentJobFailed(ACTION_SUBAGENT_EXECUTION_FAILED)
    return result.value


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


def _plan_turn(
    turn: LlmToolCallTurn, *, max_parallel: int, remaining_tool_calls: int
) -> NativeReactTurnPlan:
    """Split one turn's calls by the parent's batch policy.

    Read-only calls run at once, a changing call runs alone in order, and a call
    the policy leaves out is answered with the parent's reason for it. A report
    that is not its turn's single call is one of them, so no requested work and
    no later correction is lost to a report ending the run early.
    """

    plan = plan_tool_batch(
        tuple(_PlannedCall(call) for call in turn.calls),
        max_parallel=max_parallel,
        remaining_tool_steps=remaining_tool_calls,
    )
    skipped = [
        (
            entry.call.call.name,
            entry.call.call.arguments,
            EXCLUSION_NOTICES[entry.reason],
        )
        for entry in (*plan.deferred, *plan.dropped)
    ]
    skipped.extend(
        (name, {}, PROVIDER_DROPPED_NOTICE) for name in turn.dropped_call_names
    )
    return NativeReactTurnPlan(
        calls=tuple(planned.call for planned in plan.calls),
        parallel=plan.mode == "parallel",
        skipped=tuple(
            NativeReactSkippedCall(
                name=name,
                arguments=arguments,
                reason=_not_run_reason(notice),
            )
            for name, arguments, notice in skipped
        ),
    )


def _not_run_reason(notice: str) -> str:
    return (
        f"Not run: this call {notice}. "
        "Request it again in a later turn if it is still needed."
    )


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
