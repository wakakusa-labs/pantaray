"""ツール呼び出しが正常な結果を返さなかったときの終端ステップ確定。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.updates import append_state_error
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    FinalizedToolStepResult,
    TerminalStepEmission,
    record_tool_step,
)
from pantaray_agents.agents.action_agent.runtime.tool_attachments import ToolAttachment
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    CanceledCommandOutput,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.agent.base import AgentError, JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output

from ...tool_runtime.formal_step import (
    finalize_error_step,
    finalize_synthetic_step,
    finalized_execution_step,
)
from ...tool_runtime.outcome_line import OutcomeStatus, build_outcome_line
from ...tool_runtime.shared import (
    NOT_EXECUTED_OUTPUT_KIND,
    ToolCompletionAuditPersistenceError,
)
from ...tools import ToolExecutionResult as ToolExecutionPayload
from .. import common
from .builders import _build_tool_history_entry
from .call_slots import ToolCallSlot
from .helpers import _apply_tool_result_state, _resolve_parent_step_id
from .state_access import ToolArgsPayload, latest_state_error_payload

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime

# 並列バッチで 1 件が返らなくなったときの終端コード。兄弟呼び出しは影響を受けない。
TOOL_BATCH_TIMEOUT_ERROR_CODE = "TOOL_BATCH_TIMEOUT"
# 発行済みのまま停止操作で打ち切られた呼び出しの終端コード。
TOOL_CANCELED_ERROR_CODE = "ACTION_TOOL_CANCELED"
# 発行される前に停止操作が届いた呼び出しの終端コード。
TOOL_NOT_EXECUTED_ERROR_CODE = "ACTION_TOOL_NOT_EXECUTED"

# 次の THINK が読む文。停止で終わった呼び出しについて、モデルが知りうることだけを言う。
# プロンプトと同じ英語で書く。
TOOL_INTERRUPTED_MESSAGE = (
    "The user stopped the run while this call was in flight. Any output on this "
    "step is the last progress that was captured before the call was killed; "
    "later output may be missing, and whether the call changed anything outside "
    "this agent is unknown. Check the current state before running it again."
)
TOOL_NOT_EXECUTED_MESSAGE = (
    "The user stopped the run before this call was issued. It never ran, so it "
    "changed nothing and left nothing to check."
)
_TOKEN_BUDGET_EXCEEDED_CODE = "ACTION_TOKEN_BUDGET_EXCEEDED"


async def save_token_accounting_error_step(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    exec_result: ToolExecutionPayload,
    args_payload: ToolArgsPayload,
    tool_invocation_ids: tuple[str, ...],
) -> None:
    """トークン予算超過で停止した呼び出しを stopped として確定する。"""

    step_error = AgentError.model_validate(
        latest_state_error_payload(state, error_code=_TOKEN_BUDGET_EXCEEDED_CODE)
    )
    formal_result = finalized_execution_step(
        exec_result,
        status="error",
        error=step_error,
    )
    await _record_failed_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=exec_result.step_id,
        args_payload=args_payload,
        history_args=args_payload,
        attachments=exec_result.attachments,
        formal_result=formal_result,
        result_line=build_outcome_line(
            slot.tool_id,
            formal_result.output.output,
            status="stopped",
        ),
        started_at=exec_result.started_at,
        completed_at=exec_result.completed_at,
        apply_result_state=False,
        prompt_tokens=exec_result.prompt_tokens,
        completion_tokens=exec_result.completion_tokens,
        execution_time_ms=exec_result.execution_time_ms,
        tool_invocation_ids=tool_invocation_ids,
    )


async def finalize_tool_execution_failure(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    started_at: str,
    exc: Exception,
    traceback_text: str,
    tool_invocation_ids: tuple[str, ...],
) -> bool:
    """例外で終わった呼び出しを error step として確定し、監査永続化失敗かを返す。"""

    audit_persistence_failed = isinstance(exc, ToolCompletionAuditPersistenceError)
    finalized_error = common.finalized_tool_error(exc)
    tool_error = runtime.services.response.build_agent_error(
        error_type=(
            "persistence_error" if audit_persistence_failed else "tool_execution_error"
        ),
        error_code=(
            "ACTION_TOOL_AUDIT_PERSISTENCE_FAILED"
            if audit_persistence_failed
            else f"ACTION_TOOL_{slot.tool_id.upper()}_FAILED"
        ),
        error_message=(
            "Tool failed; details are stored in the action tool result file."
            if finalized_error is not None
            else str(exc)
        ),
        severity="error" if audit_persistence_failed else "warning",
        error_details={
            "tool_id": slot.tool_id,
            "args": args_payload,
            "attempts": 1,
        },
    )
    await _finalize_unreturned_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=step_id,
        args_payload=args_payload,
        started_at=started_at,
        exc=exc,
        traceback_text=traceback_text,
        tool_error=tool_error,
        finalized_error=finalized_error,
        tool_invocation_ids=tool_invocation_ids,
    )
    return audit_persistence_failed


async def finalize_tool_call_timeout(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    started_at: str,
    timeout_seconds: float,
) -> None:
    """per-call timeout を超えた呼び出しを ``TOOL_BATCH_TIMEOUT`` として確定する。"""

    exc = TimeoutError(
        f"{slot.tool_id} exceeded its {timeout_seconds:g}s tool call timeout."
    )
    tool_error = runtime.services.response.build_agent_error(
        error_type="tool_execution_error",
        error_code=TOOL_BATCH_TIMEOUT_ERROR_CODE,
        error_message=str(exc),
        severity="warning",
        error_details={
            "tool_id": slot.tool_id,
            "timeout_seconds": timeout_seconds,
        },
    )
    await _finalize_unreturned_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=step_id,
        args_payload=args_payload,
        started_at=started_at,
        exc=exc,
        traceback_text="",
        tool_error=tool_error,
        finalized_error=None,
        tool_invocation_ids=(),
    )


async def finalize_tool_call_interrupted(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    started_at: str,
    partial_output: CanceledCommandOutput | None,
) -> None:
    """発行済みのまま停止操作で打ち切った呼び出しを終端として確定する。

    呼び出しが取った監査行と tool result 行は、その呼び出し自身の境界
    （``run_tool`` / broker）が ``canceled`` で閉じている。ここが書くのは残りの終端、
    History 行と step 行だけ。``agent_action_steps.status`` に ``canceled`` は無い
    （DB 制約）ので、行は ``error`` + ``ACTION_TOOL_CANCELED`` で残す。

    この step が載せる本文は、次の THINK がこの呼び出しについて知りうる全てになる。
    kill 直前までに取れていた部分出力をそのまま残し、その先が欠けていることと外部への
    影響が不明であることを本文に書く。捨てて「stopped」とだけ書くと、次のターンは同じ
    副作用をやり直しうる。

    この step 行が載せる checkpoint は「停止後に再開したらどこから続くか」も決める。
    打ち切った呼び出しも逐次バッチの残りもユーザーが止めた作業なので持ち越さず、
    次の THINK に履歴を読んで決め直させる。
    """

    await _record_stopped_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=step_id,
        args_payload=args_payload,
        started_at=started_at,
        error_code=TOOL_CANCELED_ERROR_CODE,
        message=TOOL_INTERRUPTED_MESSAGE,
        output=_interrupted_output(partial_output),
        outcome_status="interrupted",
    )


async def finalize_tool_call_not_executed(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    started_at: str,
) -> None:
    """停止操作が届いた時点でまだ発行していなかった呼び出しを終端として確定する。

    並列バッチでセマフォ待ちだった兄弟と、逐次バッチの残りがこれにあたる。呼び出しは
    一度も発行されていないので外部への影響が無いことは確定していて、そう言い切れる
    のはこの状態だけ。打ち切った呼び出しと同じ「stopped by user」で畳むと、次の
    THINK は「やり直してよい」と「状態を確認してからにせよ」を区別できなくなる。
    """

    await _record_stopped_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=step_id,
        args_payload=args_payload,
        started_at=started_at,
        error_code=TOOL_NOT_EXECUTED_ERROR_CODE,
        message=TOOL_NOT_EXECUTED_MESSAGE,
        output={"kind": NOT_EXECUTED_OUTPUT_KIND},
        outcome_status="not_executed",
    )


def _interrupted_output(
    partial_output: CanceledCommandOutput | None,
) -> dict[str, JSONValue]:
    """打ち切り直前までに取れていた出力を、通常の結果と同じキーで載せる。

    ``bash`` / ``run_python`` は sandbox のブローカが kill 直前までの stdout / stderr を
    持っており、完走した呼び出しと同じキーで返す。ほかのツールは中断時の部分出力を
    持たないので、本文は「発行済み・出力なし・結果不明」になる。
    """

    if partial_output is None:
        return {}
    return {
        "exit_code": -1,
        "stdout": partial_output.stdout,
        "stderr": partial_output.stderr,
    }


async def _record_stopped_call(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    started_at: str,
    error_code: str,
    message: str,
    output: dict[str, JSONValue],
    outcome_status: OutcomeStatus,
) -> None:
    state["next_action"] = None
    completed_at = now_utc_iso()
    tool_error = runtime.services.response.build_agent_error(
        error_type="canceled_error",
        error_code=error_code,
        error_message=f"{slot.tool_id}: {message}",
        severity="warning",
        error_details={"tool_id": slot.tool_id},
    )
    formal_result = finalize_synthetic_step(
        state,
        step_id=step_id,
        status="error",
        output=build_runtime_tool_error_output(
            error_type="ActionStopped",
            message=message,
            details={"tool_id": slot.tool_id},
        )
        | output,
        error=tool_error,
    )
    await _record_failed_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=step_id,
        args_payload=args_payload,
        # 中断された呼び出しは引数を履歴に残す。何が走りかけたのか分からなければ、
        # 「再試行の前に状態を確認せよ」は実行できない指示になる。
        history_args=args_payload,
        attachments=None,
        formal_result=formal_result,
        result_line=build_outcome_line(
            slot.tool_id,
            formal_result.output.output,
            status=outcome_status,
        ),
        started_at=started_at,
        completed_at=completed_at,
        apply_result_state=True,
        prompt_tokens=None,
        completion_tokens=None,
        execution_time_ms=None,
        tool_invocation_ids=(),
    )


async def _finalize_unreturned_call(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    started_at: str,
    exc: Exception,
    traceback_text: str,
    tool_error: AgentError,
    finalized_error: FinalizedToolOutput | None,
    tool_invocation_ids: tuple[str, ...],
) -> None:
    completed_at = now_utc_iso()
    append_state_error(state, error=tool_error)
    error_payload = common._build_tool_error_payload(  # noqa: SLF001
        exc,
        attempt=1,
        max_attempts=1,
        traceback_text=traceback_text,
    )
    formal_result = finalize_error_step(
        state,
        step_id=step_id,
        finalized_output=finalized_error,
        raw_output={"error": error_payload, "tool_id": slot.tool_id},
        error=tool_error,
    )
    await _record_failed_call(
        agent,
        state,
        runtime,
        slot=slot,
        step_id=step_id,
        args_payload=args_payload,
        # A failed call keeps its arguments too: the conversation replays the
        # call the model made, and one without them reads as a call it never
        # wrote.
        history_args=args_payload,
        attachments=None,
        formal_result=formal_result,
        result_line=build_outcome_line(
            slot.tool_id,
            formal_result.output.output,
            status="failed",
            error_code=tool_error.error_code,
        ),
        started_at=started_at,
        completed_at=completed_at,
        apply_result_state=True,
        prompt_tokens=None,
        completion_tokens=None,
        execution_time_ms=None,
        tool_invocation_ids=tool_invocation_ids,
    )


async def _record_failed_call(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    slot: ToolCallSlot,
    step_id: str,
    args_payload: ToolArgsPayload,
    history_args: ToolArgsPayload | None,
    attachments: list[ToolAttachment] | None,
    formal_result: FinalizedToolStepResult,
    result_line: str,
    started_at: str,
    completed_at: str,
    apply_result_state: bool,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    execution_time_ms: int | None,
    tool_invocation_ids: tuple[str, ...],
) -> None:
    history_entry = _build_tool_history_entry(
        step_id=step_id,
        step_number=slot.step_number,
        phase=state["phase"],
        summary=slot.step_note,
        tool_id=slot.tool_id,
        started_at=started_at,
        completed_at=completed_at,
        result_line=result_line,
        args=history_args,
        output=formal_result.output.output,
        attachments=attachments,
        short_step_id=slot.short_step_id,
        origin=slot.origin,
    )
    common.append_history_entry(
        state,
        scope_handle=slot.scope_handle,
        entry=history_entry,
    )
    if apply_result_state:
        _apply_tool_result_state(
            state,
            completed_at=completed_at,
            reset_validation_streak=True,
        )
    parent_step_id = _resolve_parent_step_id(state)
    await record_tool_step(
        agent,
        state,
        terminal_emission=TerminalStepEmission(
            runtime.emit_action_step, slot.tool_def.name
        ),
        step_id=step_id,
        action_id=state["action_id"],
        step_number=slot.step_number,
        step_name=f"tool::{slot.tool_id}",
        tool_args={"tool_id": slot.tool_id, "args": args_payload},
        result=formal_result,
        started_at=started_at,
        completed_at=completed_at,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        execution_time_ms=execution_time_ms,
        retry_count=0,
        parent_step_id=parent_step_id,
        goal_handle=slot.scope_handle,
        user_id=str(state.get("user_id") or ""),
        short_step_id=slot.short_step_id,
        local_step_number=slot.local_step_number,
        tool_invocation_ids=tool_invocation_ids,
        origin=slot.origin,
    )


__all__ = [
    "TOOL_BATCH_TIMEOUT_ERROR_CODE",
    "TOOL_CANCELED_ERROR_CODE",
    "TOOL_INTERRUPTED_MESSAGE",
    "TOOL_NOT_EXECUTED_ERROR_CODE",
    "TOOL_NOT_EXECUTED_MESSAGE",
    "finalize_tool_call_interrupted",
    "finalize_tool_call_not_executed",
    "finalize_tool_call_timeout",
    "finalize_tool_execution_failure",
    "save_token_accounting_error_step",
]
