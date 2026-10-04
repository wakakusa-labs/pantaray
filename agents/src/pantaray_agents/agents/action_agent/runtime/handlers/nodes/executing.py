"""Executing phase の Supervisor THINK 実装。"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from pantaray_agents.agents.action_agent.runtime.error_redaction import (
    emit_redacted_agent_error,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.assistant_message import (
    prepare_llm_turn_commit,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    EXCLUSION_NOTICES,
    PROVIDER_DROPPED_NOTICE,
    ToolBatchPlan,
    plan_tool_batch,
)
from pantaray_agents.agents.action_agent.runtime.models.tool_call import (
    PendingToolBatchModel,
    PendingToolCallModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    build_next_action,
    build_tool_call,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    ensure_context,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    get_llm_steps_taken,
    get_tool_steps_taken,
)
from pantaray_agents.agents.action_agent.runtime.steps.llm import (
    LLMStepPersistenceError,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    StateTokenSink,
)
from pantaray_agents.agents.action_agent.tools import (
    DRAFT_FINAL_ANSWER_TOOL_ID,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    build_native_action_tools,
    select_tool_registry,
    split_step_note,
)
from pantaray_agents.agents.action_agent.tools.base import ToolPolicyValidationError
from pantaray_agents.agents.core.tool_call_repair import (
    build_tool_call_repair_feedback,
)
from pantaray_agents.application.action.ports import ActionAssistantMessageEmission
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import StepType
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.conversation import LlmProviderTurn
from pantaray_llm.contracts.tool_use import LlmToolCall
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.providers.openai_responses.retry_policy import (
    LLM_TOOL_CALL_MAX_CONSECUTIVE_ERRORS,
)

from . import common
from .llm import context_budget
from .llm.helpers import (
    _apply_llm_step_state_update,
    _infer_supervisor_think_short_step_id,
)
from .llm.provider_turns import read_provider_turn_target
from .llm.recorder import record_llm_step
from .llm.send import EXECUTING_STAGE, build_executing_window, send_executing_turn
from .llm.turn_input import build_executing_turn

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime


logger = logging.getLogger(__name__)
EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS = LLM_TOOL_CALL_MAX_CONSECUTIVE_ERRORS


def _resolve_history_started_at(state: ActionAgentState) -> str:
    """Supervisor THINK の履歴開始時刻を解決する。"""
    supervisor_history = common.get_history_for_scope(
        state, scope_handle=common.SUPERVISOR_SCOPE_HANDLE
    )
    if supervisor_history:
        last_entry = supervisor_history[-1]
        completed_at = str(last_entry.get("completed_at") or "")
        if completed_at:
            return completed_at
    return str(state.get("started_at", "") or "")


def _accept_native_calls(
    calls: Sequence[LlmToolCall],
    *,
    llm_step_id: str,
    allowed_tool_ids: set[str],
) -> tuple[tuple[PendingToolCallModel, ...], str | None]:
    """宣言順に全呼び出しを検証する。1 件でも違反すればターン全体を repair へ戻す。

    `step_note` は呼び出しごとの必須引数で、ハンドラへは渡さず履歴の要約として運ぶ。
    """

    accepted: list[PendingToolCallModel] = []
    seen_call_ids: set[str] = set()
    for index, call in enumerate(calls):
        tool_id = str(call.name or "").strip()
        label = f"call #{index + 1} (tool_id {tool_id!r})"
        if call.call_id in seen_call_ids:
            return (), f"{label}: call_id must be unique within the response."
        seen_call_ids.add(call.call_id)
        if tool_id not in allowed_tool_ids:
            return (), (
                f"{label} is not listed in Available Tools for the current step. "
                f"Allowed tool_id values: {', '.join(sorted(allowed_tool_ids))}."
            )
        try:
            step_note, handler_args = split_step_note(call.arguments)
        except ToolPolicyValidationError as exc:
            return (), f"{label}: {exc}"
        accepted.append(
            PendingToolCallModel(
                call=build_tool_call(tool_id=tool_id, args=handler_args),
                step_note=step_note,
                origin=ActionToolCallOrigin(
                    llm_step_id=llm_step_id, call_id=call.call_id
                ),
            )
        )
    return tuple(accepted), None


def _build_batch_notice(
    plan: ToolBatchPlan[PendingToolCallModel],
    *,
    provider_dropped_call_names: Sequence[str],
) -> str | None:
    """このターンで実行しない呼び出しをモデルへ伝える結果行を作る。"""

    excluded = [
        f"{entry.call.tool_id} ({EXCLUSION_NOTICES[entry.reason]})"
        for entry in (*plan.deferred, *plan.dropped)
    ]
    excluded.extend(
        f"{name} ({PROVIDER_DROPPED_NOTICE})" for name in provider_dropped_call_names
    )
    if not excluded:
        return None
    requested = len(plan.calls) + len(excluded)
    return (
        f"Ran {len(plan.calls)} of {requested} requested tool calls. "
        f"Not run this turn: {', '.join(excluded)}. "
        "Request whatever is still needed in a later turn."
    )


def _batch_summary(batch: PendingToolBatchModel) -> str:
    """THINK 行の要約。単発は step_note そのもの、バッチは宣言順の一覧。"""

    if len(batch.calls) == 1:
        return batch.calls[0].step_note
    return "\n".join(
        f"{index}. {pending.tool_id}: {pending.step_note}"
        for index, pending in enumerate(batch.calls, start=1)
    )


def _repair_may_reuse_think_identity(state: ActionAgentState) -> bool:
    """修復ターンが直前 THINK の History Ref を再利用してよいかを判定する。

    再利用は「同じ論理ステップをやり直す」という宣言であり、直前 THINK が決めた呼び出しが
    1 件だけで、その 1 行を再試行が置き換える場合にしか成り立たない。

    バッチが 2 件以上の logical step を消費していたら、THINK と同じ番号を持つ History Ref
    (``S-n-TOOL``) は完了済みの先頭呼び出しの行を指している。そこで再利用すると
    ``_head_step_identity``（``act/call_slots.py``）が見る差分 ``state["step"] - THINK の
    step_number`` が 0 に戻り、再試行の先頭がその行を ``upsert_history_entry`` で置き換えて
    完了済みの結果を失う。よってこの場合は THINK に新しい番号を与え、再試行のバッチにも
    新しい History Ref を割り当てる。
    """

    for entry in reversed(
        common.get_history_for_scope(state, common.SUPERVISOR_SCOPE_HANDLE)
    ):
        if entry.get("phase") != "executing":
            continue
        if entry.get("step_type") != StepType.LLM_OUTPUT:
            continue
        think_step_number = entry.get("step_number")
        return (
            entry["tool_id"] is not None
            and isinstance(think_step_number, int)
            and state["step"] - think_step_number == 1
        )
    return False


def _think_result_line(
    *,
    parse_failed: bool,
    batch_notice: str | None,
    window_rebuilt: bool,
) -> str | None:
    """THINK 行の結果行。リセットが起きたことは決定論的な 1 文で残す。"""

    parts = [context_budget.CONTEXT_RESET_RESULT_LINE] if window_rebuilt else []
    if parse_failed:
        parts.append("Execution THINK output invalid")
    elif batch_notice:
        parts.append(batch_notice)
    return " ".join(parts) or None


async def execution_think_step(  # noqa: C901
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    sink: StateTokenSink,
) -> ActionAgentState:
    """Adopt one Supervisor turn and pass its calls to the existing runtime."""

    if state.get("status") not in {"processing", None}:
        return state
    if state.get("phase") != "executing":
        raise RuntimeError(
            "Execution THINK requires lifecycle phase='executing'; "
            f"got {state.get('phase')!r}."
        )

    # --- Step budget check (LLM / Tool) ---
    # max_steps: LLM ステップ予算（意思決定/LLM呼び出し）
    # max_tool_steps: ツール実行ステップ予算
    llm_budget = state["max_steps"]
    tool_budget = state["max_tool_steps"]
    llm_used = get_llm_steps_taken(state)
    tool_used = get_tool_steps_taken(state)
    if llm_used >= llm_budget or tool_used >= tool_budget:
        timeout_error = runtime.services.response.build_agent_error(
            error_type="budget_exceeded",
            error_code="ACTION_PROCESSING_TIMEOUT",
            error_message="Action step budget exceeded during execution thinking.",
            error_details={
                "llm_steps_used": llm_used,
                "llm_steps_budget": llm_budget,
                "tool_steps_used": tool_used,
                "tool_steps_budget": tool_budget,
                "phase": "executing",
            },
        )
        append_state_error(state, error=timeout_error)
        set_status_with_updated_at(state, status="error")
        state["final_output"] = ""
        state["next_action"] = None
        return state

    context = ensure_context(state)
    window_rebuilt = False

    allowed_tool_ids: set[str] = set(SUPERVISOR_SINGLE_REACT_TOOL_IDS)
    native_tools = build_native_action_tools(
        select_tool_registry(tuple(sorted(allowed_tool_ids)))
    )

    turn = build_executing_turn(agent, state, runtime, tools=native_tools)
    provider_turns = await runtime.open_provider_turn_store(state)
    inference_profile = agent._resolve_inference_profile_id(stage=EXECUTING_STAGE)

    step_id = str(uuid.uuid4())
    step_started_at = datetime.now(UTC)

    # --- output repair loop ---
    prompt_text = ""
    attempt_errors: list[str] = []
    response_text = ""
    step_completed_at = step_started_at
    parse_failed = False
    accepted_turn: LlmActionTurnResponse | None = None
    accepted_provider_turn: LlmProviderTurn | None = None

    batch: PendingToolBatchModel | None = None
    batch_notice: str | None = None
    thinking = ""

    # 1 ターンで受け取れる呼び出し数。予算より多く要求しても実行できないので、
    # provider へは実行可能な件数だけを要求する。
    remaining_tool_steps = tool_budget - tool_used
    max_parallel_tool_calls = (
        load_local_runtime_tunables().action_agent.max_parallel_tool_calls
    )
    requested_max_calls = min(max_parallel_tool_calls, remaining_tool_steps)

    for attempt in range(EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS):
        repair_notice = ""
        if attempt > 0:
            reason = (
                attempt_errors[-1] if attempt_errors else "previous output was invalid"
            )
            allowed_notice = (
                "\nAllowed tool_id values in this mode: "
                + ", ".join(sorted(allowed_tool_ids))
                + "\n"
            )
            repair_notice = (
                "\n\n"
                "# System Notice\n"
                f"The previous output could not be processed because: {reason}\n"
                f"{allowed_notice}"
                "Return commentary and/or allowed tool calls with valid arguments."
            )

        request_identity, connection = read_provider_turn_target(
            inference_profile=inference_profile
        )
        provider_turns.use_identity(request_identity)
        prepare = build_executing_window(
            turn,
            state,
            rendering=runtime.services.rendering,
            repair_notice=repair_notice,
            store=provider_turns,
        )
        try:
            prepared = prepare()
        except context_budget.ContextCapacityExceeded as exc:
            error = runtime.services.response.build_agent_error(
                error_type="context_capacity_exceeded",
                error_code="ACTION_CONTEXT_CAPACITY_EXCEEDED",
                error_message=str(exc),
            )
            await emit_redacted_agent_error(runtime.emit_error, error)
            append_state_error(state, error=error)
            set_status_with_updated_at(state, status="error")
            state["final_output"] = ""
            state["next_action"] = None
            if attempt == 0:
                return state
            parse_failed = True
            break
        prompt_text = prepared.recorded_prompt
        window_rebuilt = window_rebuilt or prepared.did_rebuild
        usage_before = sink.delta
        try:
            native_turn = await send_executing_turn(
                agent,
                sink=sink,
                prepared=prepared,
                prepare=prepare,
                store=provider_turns,
                connection=connection,
                tools=native_tools,
                max_parallel_tool_calls=requested_max_calls,
                system_instruction=turn.system_instruction,
            )
        except LlmProxyExecutionError as exc:
            if exc.recovery != "repair_next_turn":
                raise
            attempt_errors.append(
                build_tool_call_repair_feedback(exc, max_tool_calls=requested_max_calls)
            )
            logger.warning(
                "Execution THINK attempt %d returned an invalid tool call: %s",
                attempt + 1,
                exc.tool_call_violation_reason or "invalid_response",
            )
            continue
        finally:
            context_budget.record_think_usage(
                context,
                usage_before=usage_before,
                usage_after=sink.delta,
                rendered_bytes=prepared.rendered_bytes,
                did_rebuild=prepared.did_rebuild,
            )
        response_text = native_turn.model_dump_json()
        thinking = agent._consume_llm_thoughts() or ""
        provider_turn = agent._consume_action_provider_turn()
        step_completed_at = datetime.now(UTC)

        # 許可外 tool_id と step_note 違反はどちらも repair loop へ戻す。
        accepted_calls, failure_reason = _accept_native_calls(
            native_turn.calls,
            llm_step_id=step_id,
            allowed_tool_ids=allowed_tool_ids,
        )
        if failure_reason is not None:
            attempt_errors.append(failure_reason)
            continue
        if accepted_calls:
            plan = plan_tool_batch(
                accepted_calls,
                max_parallel=max_parallel_tool_calls,
                remaining_tool_steps=remaining_tool_steps,
            )
            # A turn of nothing but held-back calls runs nothing; its notice
            # still reaches the model, as a turn without calls does.
            batch = (
                PendingToolBatchModel(calls=plan.calls, mode=plan.mode)
                if plan.calls
                else None
            )
            batch_notice = _build_batch_notice(
                plan,
                provider_dropped_call_names=native_turn.dropped_call_names,
            )
        accepted_turn = native_turn
        accepted_provider_turn = provider_turn
        if DRAFT_FINAL_ANSWER_TOOL_ID in native_turn.dropped_call_names or any(
            call.tool_id == DRAFT_FINAL_ANSWER_TOOL_ID for call in accepted_calls
        ):
            accepted_turn = native_turn.model_copy(update={"messages": []})
        break
    else:
        parse_failed = True
        error = runtime.services.response.build_agent_error(
            error_type="llm_output_error",
            error_code="ACTION_EXECUTION_THINK_OUTPUT_INVALID",
            error_message=(
                "Execution THINK could not select an executable action after retries."
            ),
            error_details={
                "attempts": EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS,
                "errors": list(attempt_errors),
            },
        )
        await emit_redacted_agent_error(runtime.emit_error, error)
        append_state_error(state, error=error)
        set_status_with_updated_at(state, status="error")
        state["final_output"] = ""
        state["next_action"] = None

    context["last_supervisor_prompt"] = prompt_text
    if await runtime.services.cancellation.check_cancellation(state):
        return state

    decided_at = format_utc_iso(step_completed_at)
    adopted_state = state
    state = copy.deepcopy(state)
    llm_turn = (
        prepare_llm_turn_commit(
            state,
            llm_step_id=step_id,
            messages=accepted_turn.messages,
            occurred_at=decided_at,
        )
        if accepted_turn is not None
        else None
    )
    # ``batch`` がバッチ全体の正本。``tool`` は単発実行しか消費しない経路のための
    # 先頭要素の射影で、両者は NextActionModel 側で一致を検証する。
    state["next_action"] = (
        build_next_action(
            tool=batch.calls[0].call,
            batch=batch,
            decided_at=decided_at,
        )
        if batch
        else None
    )
    # Tools share their THINK position; a turn without calls consumes its own row.
    step_number = state["step"]
    if batch is None and (accepted_turn is not None or parse_failed):
        state["step"] += 1
    # THINK は LLM ステップとしてカウントする（LLM/Tool の内訳を保持）
    _apply_llm_step_state_update(state, decided_at=decided_at)

    # New utterances reserve positions, so a repair cannot reuse the preceding THINK.
    scope_handle = common.SUPERVISOR_SCOPE_HANDLE
    if not (llm_turn and llm_turn.messages) and _repair_may_reuse_think_identity(state):
        local_step_number, short_step_id = _infer_supervisor_think_short_step_id(
            state,
            scope_handle=scope_handle,
            phase="executing",
        )
    else:
        local_step_number = common.get_or_increment_local_step_number(
            state, scope_handle
        )
        short_step_id = common.build_short_step_id(
            scope_handle, local_step_number, StepType.LLM_OUTPUT
        )

    try:
        await record_llm_step(
            agent,
            state,
            scope_handle=scope_handle,
            step_id=step_id,
            step_number=step_number,
            step_name="supervisor_think",
            phase="executing",
            summary="" if batch is None else _batch_summary(batch),
            result_line=_think_result_line(
                parse_failed=parse_failed,
                batch_notice=batch_notice,
                window_rebuilt=window_rebuilt,
            ),
            tool_id=None if batch is None else batch.calls[0].call.tool_id,
            llm_prompt_text=prompt_text,
            llm_response_text=response_text,
            thinking=thinking,
            args=(
                None
                if batch is None
                else cast(dict[str, object], dict(batch.calls[0].call.args))
            ),
            status="error" if parse_failed else "success",
            started_at=format_utc_iso(step_started_at),
            completed_at=decided_at,
            history_started_at=_resolve_history_started_at(state),
            history_completed_at=decided_at,
            short_step_id=short_step_id,
            local_step_number=local_step_number,
            infer_parent_from_scope_history=True,
            llm_turn=llm_turn,
            # Recorded without the repair notice: the notice belongs to the
            # attempt that was rejected, and a later turn replaying it would
            # break the append-only input the prompt cache reads.
            turn_context=prepared.turn_context,
            world_state=prepared.world_state,
            # Held for the rest of this run as it is written, so the next turn
            # hands back the same bytes whether it reads them from here or,
            # after a restart, from the row this writes.
            provider_turn=provider_turns.accept(
                step_id=step_id,
                turn=accepted_provider_turn,
            ),
        )
    except LLMStepPersistenceError:
        if await runtime.services.cancellation.check_cancellation(adopted_state):
            return adopted_state
        raise
    adopted_state.update(state)
    if llm_turn is not None:
        for message in llm_turn.messages:
            await runtime.emit_action_step(
                ActionAssistantMessageEmission(
                    step_id=message.step_id, step_number=message.step_number
                )
            )

    return adopted_state


__all__ = ["execution_think_step"]
