from __future__ import annotations

import asyncio
import copy
import logging
import traceback
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pantaray_agents.agents.action_agent.runtime.log_safety import exception_type_name
from pantaray_agents.agents.action_agent.runtime.models.tool_call import (
    NextActionModel,
    PendingToolBatchModel,
    PendingToolCallModel,
    ToolBatchModeModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    ensure_context as _ensure_context,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.runtime.steps.approval_pause import (
    ApprovalPauseToolStep,
    persist_approval_pause_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    TerminalStepEmission,
    record_tool_step,
)
from pantaray_agents.agents.action_agent.runtime.stop_watch import (
    ActionStopRequested,
    ActionStopWatch,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    StateTokenSink,
)
from pantaray_agents.agents.core import TokenBudgetExceeded
from pantaray_agents.application.action.ports import ActionStepEventPersistenceError
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    APPLY_PATCH_TOOL_ID,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    canceled_command_output,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin

from ...tool_runtime.formal_step import (
    failed_result_error,
    finalized_execution_step,
)
from ...tool_runtime.outcome_line import build_outcome_line
from ...tool_runtime.request_identity import consume_resumed_tool_step_id
from ...tool_runtime.shared import (
    ApprovalRequiredToolControl,
    FailedToolControl,
    ToolExecutionActor,
)
from ...tools import ToolExecutionResult as ToolExecutionPayload
from ...tools import ToolValidationError, run_tool
from .. import common
from .builders import _build_tool_history_entry
from .call_slots import (
    ToolCallSlot,
)
from .failures import (
    finalize_tool_call_interrupted,
    finalize_tool_call_not_executed,
    finalize_tool_call_timeout,
    finalize_tool_execution_failure,
    save_token_accounting_error_step,
)
from .helpers import (
    _apply_tool_result_state,
    _phase_after_tool_execution,
    _resolve_parent_step_id,
)
from .state_access import (
    ToolArgsPayload,
)
from .validation_flow import (
    handle_tool_validation_error,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime

logger = logging.getLogger(__name__)

UNCANCELLABLE_TOOL_IDS = frozenset({APPLY_PATCH_TOOL_ID})
"""停止要求でも中断せず完走させる呼び出し。

``apply_patch`` の本体は 1 つのスレッドがワークスペースの排他ロックを取って走る短い
書き込みで、await 側を cancel してもそのスレッドは書き続ける。中断できるのは「結果を
待つのをやめる」ところまでで、書き込み自体は止まらないので、cancel は「半分書けた
ファイルを canceled として記録する」結果にしかならない。完走を待つほうが速く、かつ
正しい。ほかの書き込み系ツール（``bash`` / ``run_python``）は sandbox のプロセス
グループごと kill できるので、この例外には入らない。
"""


@dataclass(slots=True)
class RemainingToolBatch:
    """まだ実行していないバッチ呼び出し列。

    checkpoint の ``next_action`` は「まだ実行していない呼び出しだけ」を載せる契約なので、
    1 件を永続化する直前にその呼び出しを外し、残りを ``next_action`` へ書き戻す。クラッシュ
    後の resume はこの残りだけを再実行する。

    持ち越すのは逐次バッチだけ。並列バッチの兄弟は宣言順どおりに完了するとは限らないのに、
    行の ``step_number`` は宣言順で固定されており、crash recovery は
    ``ORDER BY step_number DESC`` で checkpoint を 1 行選ぶ
    （``action_startup_recovery_authority.py:219``）。先に完了した後ろの呼び出しの行が選ばれると、
    その残りには既に完了・永続化済みの前の呼び出しが載っており、再実行した結果がその呼び出しの
    History Ref を奪ってしまう。並列バッチは読み取り専用 allowlist だけなので、持ち越さず次の
    THINK に決め直させるほうが安い。

    ``mode`` が ``None`` なのは ``batch`` を持たない next_action（batch 以前の checkpoint と
    承認再開の単発経路）で、そこにも持ち越す残りは存在しない。
    """

    mode: ToolBatchModeModel | None
    decided_at: str
    pending: list[ToolCallSlot]

    def consume(self, slot: ToolCallSlot) -> None:
        self.pending = [item for item in self.pending if item is not slot]

    def as_next_action(self) -> NextActionModel | None:
        if self.mode != "sequential" or not self.pending:
            return None
        calls = tuple(
            PendingToolCallModel(
                call=slot.call,
                step_note=slot.step_note,
                origin=cast(ActionToolCallOrigin, slot.origin),
            )
            for slot in self.pending
        )
        return NextActionModel(
            tool=calls[0].call,
            batch=PendingToolBatchModel(calls=calls, mode=self.mode),
            decided_at=self.decided_at,
        )


def _carry_remaining_batch(
    state: ActionAgentState,
    remaining: RemainingToolBatch,
    *,
    executed: ToolCallSlot,
) -> None:
    """永続化の直前に、実行済み呼び出しを残バッチから外して checkpoint へ載せる。"""

    remaining.consume(executed)
    state["next_action"] = remaining.as_next_action()


async def execute_tool_call(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    sink: StateTokenSink,
    slot: ToolCallSlot,
    actor: ToolExecutionActor,
    remaining: RemainingToolBatch,
    isolate_state: bool,
    timeout_seconds: float | None,
    stop_watch: ActionStopWatch,
) -> ActionAgentState:
    """1 呼び出しを実行し、その終端ステップを永続化する。"""

    tool_def = slot.tool_def
    tool_id = slot.tool_id
    started_at = now_utc_iso()
    args_payload: ToolArgsPayload = dict(slot.call.args)

    exec_result: ToolExecutionPayload | None = None
    last_exception: Exception | None = None
    last_traceback = ""
    tool_invocation_ids: tuple[str, ...] = ()
    tool_step_id = consume_resumed_tool_step_id(state) or str(uuid.uuid4())
    if stop_watch.stop_observed:
        # 停止はこの呼び出しを発行する前に確定していた。外部への影響が無いことは
        # 確定しているので、打ち切った呼び出しとは別の終端で残す。
        await finalize_tool_call_not_executed(
            agent,
            state,
            runtime,
            slot=slot,
            step_id=tool_step_id,
            args_payload=args_payload,
            started_at=started_at,
        )
        return state
    if isolate_state:
        execution_state = copy.deepcopy(state)
        sink.rebind_state(execution_state)
    else:
        execution_state = state
    call_timeout = asyncio.timeout(timeout_seconds)
    try:
        async with call_timeout:
            exec_result = await stop_watch.run(
                run_tool(
                    agent,
                    tool_def,
                    args_payload,
                    execution_state,
                    sink=sink,
                    runtime=runtime,
                    step_number=slot.step_number,
                    actor=actor,
                    step_id=tool_step_id,
                    origin=slot.origin,
                ),
                cancellable=slot.tool_id not in UNCANCELLABLE_TOOL_IDS,
            )
        exec_result.step_id = tool_step_id
        state = execution_state
        if exec_result.tool_invocation_id is not None:
            tool_invocation_ids = (exec_result.tool_invocation_id,)
    except ActionStopRequested as stopped:
        # 呼び出し自身の監査行と tool result は run_tool / broker が canceled で閉じた。
        # 残る終端（history と step 行）をここで書き、run の収束は呼び出し側が行う。
        # kill 直前までの stdout/stderr は、その cancellation が運んでくる。
        await finalize_tool_call_interrupted(
            agent,
            state,
            runtime,
            slot=slot,
            step_id=tool_step_id,
            args_payload=args_payload,
            started_at=started_at,
            partial_output=canceled_command_output(stopped.cancellation),
        )
        return state
    except TokenBudgetExceeded:
        raise
    except ActionStepEventPersistenceError:
        raise
    except ToolValidationError as exc:
        return await handle_tool_validation_error(
            agent,
            runtime,
            state,
            tool_id=tool_id,
            tool_def=tool_def,
            args_payload=args_payload,
            started_at=started_at,
            exc=exc,
            step_id=tool_step_id,
            prior_tool_invocation_ids=(),
            slot=slot,
        )
    except Exception as exc:  # noqa: BLE001
        if call_timeout.expired():
            await finalize_tool_call_timeout(
                agent,
                state,
                runtime,
                slot=slot,
                step_id=tool_step_id,
                args_payload=args_payload,
                started_at=started_at,
                timeout_seconds=timeout_seconds or 0.0,
            )
            return state
        tool_invocation_id = getattr(exc, "tool_invocation_id", None)
        if isinstance(tool_invocation_id, str) and tool_invocation_id:
            tool_invocation_ids = (tool_invocation_id,)
        last_exception = exc
        last_traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        logger.error(
            "Tool execution failed: tool_id=%s exception_type=%s exception_message=%s",
            tool_id,
            exception_type_name(exc),
            str(exc),
        )

    if exec_result is None:
        assert last_exception is not None
        audit_persistence_failed = await finalize_tool_execution_failure(
            agent,
            state,
            runtime,
            slot=slot,
            step_id=tool_step_id,
            args_payload=args_payload,
            started_at=started_at,
            exc=last_exception,
            traceback_text=last_traceback,
            tool_invocation_ids=tool_invocation_ids,
        )
        if audit_persistence_failed:
            set_status_with_updated_at(state, status="error")
            state["final_output"] = ""
            state["next_action"] = None
        return state

    if state.get("status") == "error":
        await save_token_accounting_error_step(
            agent,
            state,
            runtime,
            slot=slot,
            exec_result=exec_result,
            args_payload=args_payload,
            tool_invocation_ids=tool_invocation_ids,
        )
        return state

    completed_at = exec_result.completed_at
    approval_control = (
        exec_result.control
        if isinstance(exec_result.control, ApprovalRequiredToolControl)
        else None
    )
    approval_required = approval_control is not None
    formal_result = finalized_execution_step(
        exec_result,
        error=(
            failed_result_error(
                exec_result.control,
                error_code=f"ACTION_TOOL_{tool_def.tool_id.upper()}_FAILED",
            )
            if isinstance(exec_result.control, FailedToolControl)
            else None
        ),
    )
    history_entry = _build_tool_history_entry(
        step_id=exec_result.step_id,
        step_number=slot.step_number,
        phase=state["phase"],
        summary=slot.step_note,
        tool_id=tool_def.tool_id,
        started_at=started_at,
        completed_at=completed_at,
        result_line=build_outcome_line(
            tool_def.tool_id,
            formal_result.output.output,
            status=(
                "awaiting_approval"
                if approval_required
                else "failed"
                if formal_result.status == "error"
                else "ok"
            ),
            error_code=(
                formal_result.error["error_code"] if formal_result.error else None
            ),
        ),
        args=args_payload,
        output=formal_result.output.output,
        attachments=exec_result.attachments,
        short_step_id=slot.short_step_id,
        origin=slot.origin,
        agents_md=exec_result.agents_md,
    )
    common.append_history_entry(
        state,
        scope_handle=slot.scope_handle,
        entry=history_entry,
    )
    parent_step_id = _resolve_parent_step_id(state)

    if approval_required:
        assert approval_control is not None
        if state.get("phase") == "planning":
            raise RuntimeError(
                "Planning tool allowlist must not require interactive approval."
            )
        context = _ensure_context(state)
        context["tool_validation_error_streak"] = 0
        # preflight が承認要と判定した時点で、この呼び出しの本体はまだ実行されていない。
        # 未実行の呼び出しだけを残す契約どおり、自分自身を含めた残りを持ち越す。
        state["next_action"] = remaining.as_next_action()
        state["updated_at"] = completed_at
        await persist_approval_pause_checkpoint(
            agent,
            state,
            owner="supervisor",
            tool_id=tool_def.tool_id,
            args=args_payload,
            requested_at=completed_at,
            tool_step=ApprovalPauseToolStep(
                step_id=exec_result.step_id,
                action_id=state["action_id"],
                step_number=slot.step_number,
                step_name=f"tool::{tool_def.tool_id}",
                tool_id=tool_def.tool_id,
                tool_args={"tool_id": tool_def.tool_id, "args": args_payload},
                approval=approval_control,
                finalized_output=formal_result.output,
                started_at=exec_result.started_at,
                completed_at=completed_at,
                prompt_tokens=exec_result.prompt_tokens,
                completion_tokens=exec_result.completion_tokens,
                execution_time_ms=exec_result.execution_time_ms,
                retry_count=0,
                parent_step_id=parent_step_id,
                goal_handle=slot.scope_handle,
                user_id=str(state.get("user_id") or ""),
                short_step_id=slot.short_step_id,
                local_step_number=slot.local_step_number,
                tool_invocation_ids=tool_invocation_ids,
                origin=slot.origin,
            ),
            build_agent_error=runtime.services.response.build_agent_error,
            logger=logger,
        )
        return state

    state.pop("pending_approval_request", None)
    state.pop("current_approval_blockers", None)
    _apply_tool_result_state(
        state,
        completed_at=completed_at,
        reset_validation_streak=True,
    )
    _carry_remaining_batch(state, remaining, executed=slot)
    state["phase"] = _phase_after_tool_execution(state)

    await record_tool_step(
        agent,
        state,
        terminal_emission=TerminalStepEmission(runtime.emit_action_step, tool_def.name),
        step_id=exec_result.step_id,
        action_id=state["action_id"],
        step_number=slot.step_number,
        step_name=f"tool::{tool_def.tool_id}",
        tool_args={"tool_id": tool_def.tool_id, "args": args_payload},
        result=formal_result,
        started_at=exec_result.started_at,
        completed_at=completed_at,
        prompt_tokens=exec_result.prompt_tokens,
        completion_tokens=exec_result.completion_tokens,
        execution_time_ms=exec_result.execution_time_ms,
        retry_count=0,
        parent_step_id=parent_step_id,
        goal_handle=slot.scope_handle,
        user_id=str(state.get("user_id") or ""),
        short_step_id=slot.short_step_id,
        local_step_number=slot.local_step_number,
        tool_invocation_ids=tool_invocation_ids,
        subagent_collection_receipt=exec_result.subagent_collection_receipt,
        origin=slot.origin,
    )

    return state
