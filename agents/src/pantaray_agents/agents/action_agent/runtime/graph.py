"""ActionAgent 用 LangGraph 構築ヘルパー。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict

from pantaray_agents.agents.core import TokenBudgetExceeded
from pantaray_agents.application.action.cancellation_service import (
    ActionCancellationService,
)
from pantaray_agents.application.action.ports import ActionStepEmitter
from pantaray_agents.application.action.response_service import ActionResponseService
from pantaray_agents.application.action.resume_service import ActionResumeService
from pantaray_agents.conversation.provider_turns import ProviderTurnStore
from pantaray_agents.local_runtime.runtime.action_user_adoption import (
    adopt_pending_action_user_steps_at_parent_think,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    resolve_local_runtime_db_config,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionResumeUserStep,
)
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.zanei import ZaneiTools
from pantaray_agents.utils.trace_context import get_trace_context

from ..services.prompt_rendering_service import PromptRenderingService
from ..services.token_accounting_service import (
    ActionTokenAccountingService,
    StateTokenSink,
)
from ..support.status import ACTION_TERMINAL_STATUSES
from . import compiled_graph as _compiled_graph
from .compiled_graph import (
    ACTION_GRAPH_RUNTIME_CONFIG_KEY,
    ResumeRouteName,
    cached_action_agent_compiled_graph,
)
from .error_redaction import emit_redacted_agent_error
from .handlers.nodes import (
    action_step,
    execution_think_step,
    finalize_step,
    initialize_context,
)
from .handlers.nodes.llm.provider_turns import load_action_provider_turns
from .handlers.nodes.llm.send import EXECUTING_STAGE
from .models.approval import PendingApprovalRequestModel
from .state import (
    ActionAgentState,
    ActionAgentStateConfig,
)
from .steps.counters import (
    get_llm_steps_taken,
    get_tool_steps_taken,
)
from .stop_watch import (
    ActionStopRequested,
    ActionStopWatch,
    build_action_stop_probe,
    converge_stopped_run,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent

logger = logging.getLogger(__name__)

type ActionEventPayload = Mapping[str, JSONValue]
RESUME_ROUTE_TO_NODE = _compiled_graph.RESUME_ROUTE_TO_NODE
clear_cached_action_agent_graph = _compiled_graph.clear_cached_action_agent_graph


def _should_finalize(state: ActionAgentState) -> bool:
    return bool(
        state.get("run_authority") == "superseded"
        or state.get("skip_persist")
        or state.get("status") in ACTION_TERMINAL_STATUSES
        or state.get("final_output")
        or state.get("phase") == "finalizing"
    )


class RuntimeServices(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    resume: ActionResumeService
    cancellation: ActionCancellationService
    response: ActionResponseService
    rendering: PromptRenderingService
    token_accounting: ActionTokenAccountingService


@dataclass(slots=True)
class ActionGraphRuntime:
    """LangGraph node dependencies and event callbacks."""

    agent: ActionAgent
    request: ActionAgentRequest
    state_config: ActionAgentStateConfig
    emit_action_step: ActionStepEmitter
    emit_error: Callable[[ActionEventPayload], Awaitable[None]]
    services: RuntimeServices
    intervening_user_step: ActionResumeUserStep | None = None
    # Raw computer-activity reader session for this run. In-memory only: it never
    # enters Action state, so it stays out of the runtime checkpoint.
    zanei_session: ZaneiTools | None = None
    # The provider turns this run may hand back, opened at its first THINK. Out
    # of the checkpoint too: a turn is 1-4 KB and the checkpoint is rewritten on
    # every step, so the turns live on their own step rows.
    provider_turns: ProviderTurnStore | None = None

    async def open_provider_turn_store(
        self, state: ActionAgentState
    ) -> ProviderTurnStore:
        """This run's store, reading back what earlier runs recorded, once."""

        if self.provider_turns is None:
            self.provider_turns = await load_action_provider_turns(
                self.agent.repository,
                user_id=state["user_id"],
                action_id=state["action_id"],
                inference_profile=self.agent._resolve_inference_profile_id(
                    stage=EXECUTING_STAGE
                ),
            )
        return self.provider_turns

    async def init(self, state: ActionAgentState) -> ActionAgentState:
        """Init ノード: ステート初期化を担当する。"""

        return await initialize_context(self.agent, state, self)

    async def _run_token_sink_node(
        self,
        state: ActionAgentState,
        operation: Callable[[StateTokenSink], Awaitable[ActionAgentState]],
    ) -> ActionAgentState:
        sink = StateTokenSink(self.services.token_accounting, state)
        try:
            result = await operation(sink)
        except TokenBudgetExceeded as exc:
            if not isinstance(exc.sink, StateTokenSink):
                raise
            await emit_redacted_agent_error(self.emit_error, exc.error)
            return exc.sink.state
        if sink.budget_error is not None:
            await emit_redacted_agent_error(self.emit_error, sink.budget_error)
            return sink.state
        return result

    async def think(self, state: ActionAgentState) -> ActionAgentState:
        """Select one action using the prompt contract for the lifecycle phase."""

        if _should_finalize(state):
            return state
        if await self.services.cancellation.check_cancellation(state):
            return state
        if (
            get_llm_steps_taken(state) < state["max_steps"]
            and get_tool_steps_taken(state) < state["max_tool_steps"]
        ):
            trace = get_trace_context()
            job_id = trace.local_job_id if trace is not None else None
            process_id = trace.extra.get("process_id") if trace is not None else None
            if job_id is not None or process_id is not None:
                if not job_id or not isinstance(process_id, str) or not process_id:
                    raise MigrationError(
                        "Action THINK local runtime identity is incomplete"
                    )
                db_path, busy_timeout_ms = resolve_local_runtime_db_config(
                    fallback=read_local_runtime_db_config
                )
                state = adopt_pending_action_user_steps_at_parent_think(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    job_id=job_id,
                    process_id=process_id,
                    request=self.request,
                    state=state,
                )
        phase = state.get("phase")
        if phase == "executing":
            # THINK が書く行は LLM 応答のあとにしか無いので、応答待ちの中断は
            # 部分的な行を残さない。停止要求はここで即座に HTTP 呼び出しを打ち切る。
            stop_watch = ActionStopWatch(probe=build_action_stop_probe(state))
            async with stop_watch:
                try:
                    return await stop_watch.run(
                        self._run_token_sink_node(
                            state,
                            lambda sink: execution_think_step(
                                self.agent,
                                state,
                                self,
                                sink=sink,
                            ),
                        )
                    )
                except ActionStopRequested:
                    converge_stopped_run(state)
                    return state
        raise RuntimeError(
            f"THINK is available only while executing; got phase={phase!r}."
        )

    async def action(self, state: ActionAgentState) -> ActionAgentState:
        """Execute the pending tool through the canonical Action path."""

        return await self._run_token_sink_node(
            state,
            lambda sink: action_step(self.agent, state, self, sink=sink),
        )

    async def finalize(self, state: ActionAgentState) -> ActionAgentState:
        """Done ノード: 最終結果を確定しストリーム終了処理を実施する。"""

        return await finalize_step(self.agent, state, self)

    async def resume_gate(self, state: ActionAgentState) -> ActionAgentState:
        """fresh run / restored run 共通の入口ノード。"""

        return state

    def route_from_resume(self, state: ActionAgentState) -> ResumeRouteName:
        """復元済み state の再開位置を決定する。"""

        return self.services.resume.graph_route_from_resume(state)

    def route_from_think(self, state: ActionAgentState) -> str:
        """Route one canonical THINK decision."""

        if _should_finalize(state):
            return "finalize"
        if state.get("phase") == "executing":
            return self.services.resume.graph_route_from_execution_think(state)
        raise RuntimeError(
            f"Unexpected Action lifecycle after THINK: {state.get('phase')!r}."
        )

    def route_after_action(self, state: ActionAgentState) -> str:
        """Route canonical parent Action continuation."""

        if _should_finalize(state):
            return "finalize"
        pending_approval = state.get("pending_approval_request")
        if isinstance(pending_approval, PendingApprovalRequestModel) or state.get(
            "current_approval_blockers"
        ):
            return "halt"
        if state.get("phase") in {"planning", "executing"}:
            return "think"
        raise RuntimeError(
            f"Unexpected Action lifecycle after continuation: {state.get('phase')!r}."
        )


def build_action_agent_graph(
    runtime: ActionGraphRuntime,
) -> Callable[[ActionAgentState], Awaitable[ActionAgentState]]:
    """Cached compiled graph から ActionAgent runner を返す。

    グラフ構造:
        init -> think -> action -> think -> finalize -> END
    """

    compiled_graph = cached_action_agent_compiled_graph()

    async def runner(state: ActionAgentState) -> ActionAgentState:
        """状態を受け取り LangGraph を逐次実行する簡易ランナー。"""

        graph_recursion_limit = (state["max_steps"] + state["max_tool_steps"]) * 2 + 10
        result = await compiled_graph.ainvoke(
            cast(Any, state),
            config={
                "recursion_limit": graph_recursion_limit,
                "configurable": {ACTION_GRAPH_RUNTIME_CONFIG_KEY: runtime},
            },
        )
        return cast(ActionAgentState, result)

    return runner
