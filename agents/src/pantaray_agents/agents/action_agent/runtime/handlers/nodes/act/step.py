from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.conversation_service import (
    advance_all_supervisor_cursors,
)
from pantaray_agents.agents.action_agent.runtime.models.tool_call import (
    ToolBatchModeModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    get_tool_steps_taken,
)
from pantaray_agents.agents.action_agent.runtime.stop_watch import (
    ActionStopWatch,
    build_action_stop_probe,
    converge_stopped_run,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    StateTokenSink,
)
from pantaray_agents.config_tunables import load_local_runtime_tunables

from ...tool_runtime.shared import (
    ToolExecutionActor,
)
from .call_execution import RemainingToolBatch, execute_tool_call
from .call_slots import (
    PendingCall,
    ToolCallSlot,
    allocate_tool_call_slots,
    call_timeout_seconds,
)
from .helpers import (
    _extract_previous_step_note,
    _resolve_act_tool_contract,
)
from .state_access import (
    canonical_next_action as _canonical_next_action,
)
from .state_access import (
    raw_next_action as _raw_next_action,
)
from .validation_flow import (
    handle_invalid_tool_call,
    handle_malformed_next_action,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime

logger = logging.getLogger(__name__)


async def action_step(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    sink: StateTokenSink,
) -> ActionAgentState:
    """Action ノードの処理。"""

    if (
        state.get("run_authority") == "superseded"
        or state.get("skip_persist")
        or state.get("phase") == "finalizing"
    ):
        return state
    if state.get("status") not in {"processing", None}:
        return state

    canceled = await runtime.services.cancellation.check_cancellation(state)
    if canceled:
        return state

    (
        supervisor_act_tools_by_id,
        supervisor_act_tool_ids,
        tool_execution_actor,
    ) = _resolve_act_tool_contract(state)

    raw_next_action = _raw_next_action(state)
    next_action = _canonical_next_action(raw_next_action)
    if raw_next_action is not None and next_action is None:
        return await handle_malformed_next_action(
            agent,
            runtime,
            state,
            raw_next_action=raw_next_action,
            tools_by_id=supervisor_act_tools_by_id,
            allowed_tool_ids=supervisor_act_tool_ids,
        )

    tool_call = next_action.tool if next_action else None
    if tool_call is None:
        logger.warning("Action ノードにツールが設定されていません")
        return state

    assert next_action is not None
    batch = next_action.batch
    # ``batch`` を持たない next_action は checkpoint 復元と承認再開の単発経路。その
    # step_note は直前の THINK 行の要約から読む。
    declared_calls = (
        [(pending.call, pending.step_note, pending.origin) for pending in batch.calls]
        if batch is not None
        else [(tool_call, _extract_previous_step_note(state), None)]
    )
    batch_mode: ToolBatchModeModel = batch.mode if batch is not None else "sequential"

    pending_calls: list[PendingCall] = []
    for call, step_note, origin in declared_calls:
        tool_def = supervisor_act_tools_by_id.get(call.tool_id)
        if tool_def is None:
            return await handle_invalid_tool_call(
                agent,
                runtime,
                state,
                tool_id=call.tool_id,
                malformed_exc=None,
                allowed_tool_ids=supervisor_act_tool_ids,
            )
        pending_calls.append(
            PendingCall(
                call=call, tool_def=tool_def, step_note=step_note, origin=origin
            )
        )

    tool_budget = state["max_tool_steps"]
    tool_used = get_tool_steps_taken(state)
    if tool_used >= tool_budget:
        timeout_error = runtime.services.response.build_agent_error(
            error_type="budget_exceeded",
            error_code="ACTION_PROCESSING_TIMEOUT",
            error_message="Action tool step budget exceeded during action execution.",
            error_details={
                "tool_steps_used": tool_used,
                "tool_steps_budget": tool_budget,
                "phase": state.get("phase"),
            },
        )
        append_state_error(state, error=timeout_error)
        set_status_with_updated_at(state, status="error")
        state["final_output"] = ""
        state["next_action"] = None
        return state

    state = copy.deepcopy(state)
    sink.rebind_state(state)
    state["goal_conversations"] = advance_all_supervisor_cursors(
        state["goal_conversations"]
    )
    slots = allocate_tool_call_slots(state, calls=pending_calls)
    remaining = RemainingToolBatch(
        mode=batch.mode if batch is not None else None,
        decided_at=next_action.decided_at,
        pending=list(slots),
    )

    # 実行中に届いた停止要求は、ノード境界を待たずに実行中の呼び出しを cancel する。
    stop_watch = ActionStopWatch(probe=build_action_stop_probe(state))
    async with stop_watch:
        if len(slots) == 1:
            state = await execute_tool_call(
                agent,
                state,
                runtime,
                sink=sink,
                slot=slots[0],
                actor=tool_execution_actor,
                remaining=remaining,
                isolate_state=True,
                timeout_seconds=None,
                stop_watch=stop_watch,
            )
            if stop_watch.stop_observed:
                converge_stopped_run(state)
            return state

        # バッチは K 件ぶんの副作用をまとめて開始するので、開始直前にもう一度だけ
        # 停止要求を読む（設計: preflight と実行開始のあいだ）。
        if await runtime.services.cancellation.check_cancellation(state):
            return state
        run_batch = (
            _run_parallel_batch if batch_mode == "parallel" else _run_sequential_batch
        )
        state = await run_batch(
            agent,
            state,
            runtime,
            sink=sink,
            slots=slots,
            actor=tool_execution_actor,
            remaining=remaining,
            stop_watch=stop_watch,
        )

    if stop_watch.stop_observed:
        # 打ち切った呼び出しは既に canceled として永続化済み。ここで run を収束させる。
        converge_stopped_run(state)
        return state
    if state.get("status") in {"processing", None}:
        # 停止要求が実行の合間に確定していた場合の収束。
        await runtime.services.cancellation.check_cancellation(state)
    return state


async def _run_sequential_batch(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    sink: StateTokenSink,
    slots: tuple[ToolCallSlot, ...],
    actor: ToolExecutionActor,
    remaining: RemainingToolBatch,
    stop_watch: ActionStopWatch,
) -> ActionAgentState:
    """宣言順に 1 件ずつ実行する。run が終端化したらそこで打ち切る。"""

    for slot in slots:
        state = await execute_tool_call(
            agent,
            state,
            runtime,
            sink=sink,
            slot=slot,
            actor=actor,
            remaining=remaining,
            isolate_state=True,
            timeout_seconds=None,
            stop_watch=stop_watch,
        )
        # 次の 1 件は新しい副作用を開始するので、ポーリング結果ではなく耐久的な
        # 停止要求そのものをここで読み直す（直前のポーリング以降に確定した停止を
        # 取りこぼさない）。停止が確定していれば残りは 1 件も開始せず、
        # ``execute_tool_call`` がそれぞれを「未実行」として確定する。
        await stop_watch.stop_requested()
        if state.get("status") not in {"processing", None}:
            break
        if state.get("pending_approval_request") is not None:
            # all-or-nothing: 承認待ちになった呼び出しから先は一切開始しない。承認・拒否の
            # あとで、その呼び出しと残りを resume が続きから実行する。
            break
    return state


async def _run_parallel_batch(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    sink: StateTokenSink,
    slots: tuple[ToolCallSlot, ...],
    actor: ToolExecutionActor,
    remaining: RemainingToolBatch,
    stop_watch: ActionStopWatch,
) -> ActionAgentState:
    """読み取り専用の呼び出しをセマフォ下で同時に走らせる。

    ``parallel`` を宣言したツールは state を書き換えないので、兄弟呼び出しは 1 つの state
    を共有し、履歴と永続化だけをそれぞれの slot に書く。

    このパスは承認 pause を持たない。``parallel`` を宣言したツールのうち broker を通るのは
    ``read`` / ``list`` / ``glob`` / ``grep`` だけで、いずれも
    ``NON_PREFLIGHT_BROKERED_TOOL_IDS`` に入っていて capability 検査しか受けない。承認を
    要求しうるツール（``apply_patch`` / ``bash`` / ``run_python``）は ``sequential`` なので、
    バッチに 1 件でもあれば逐次パスへ落ちる。
    """

    semaphore = asyncio.Semaphore(
        load_local_runtime_tunables().action_agent.max_parallel_tool_calls
    )

    async def _run(slot: ToolCallSlot) -> None:
        async with semaphore:
            await execute_tool_call(
                agent,
                state,
                runtime,
                sink=sink,
                slot=slot,
                actor=actor,
                remaining=remaining,
                isolate_state=False,
                timeout_seconds=call_timeout_seconds(slot),
                stop_watch=stop_watch,
            )

    # 停止要求が届いたら ``stop_watch`` が兄弟呼び出しをまとめて cancel し、各呼び出しが
    # 自分の canceled 終端を書く。ここでは全件が終端を書き終えるのを待ってから、最初の
    # 例外だけを run の失敗として持ち上げる。
    results = await asyncio.gather(
        *(_run(slot) for slot in slots), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return state


__all__ = ["action_step"]
