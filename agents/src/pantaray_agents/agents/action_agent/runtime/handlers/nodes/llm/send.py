"""Send one Executing THINK through the connection its identity was read from.

A refused replay is answered by the conversation layer
(``conversation/provider_turns.py``): this run discards the turns it holds and
sends the same turn once more without them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import ActionTurnReply
from pantaray_agents.conversation.provider_turns import (
    ProviderTurnStore,
    send_dropping_refused_turns,
)
from pantaray_agents.local_runtime.runtime.connection_store import (
    LlmConnection,
    bind_request_llm_connection,
)
from pantaray_llm.contracts.tool_use import LlmToolDefinition

from .context_budget import PreparedWindow
from .turn_input import ExecutingTurn

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
    from pantaray_agents.agents.action_agent.services.prompt_rendering_service import (
        PromptRenderingService,
    )
    from pantaray_agents.agents.action_agent.services.token_accounting_service import (
        StateTokenSink,
    )

# The stage this node bills and, through the agent's stage map, the inference
# profile every Executing THINK of a run is sent under.
EXECUTING_STAGE = "executing"


def build_executing_window(
    turn: ExecutingTurn,
    state: ActionAgentState,
    *,
    rendering: PromptRenderingService,
    repair_notice: str,
    store: ProviderTurnStore,
) -> Callable[[], PreparedWindow]:
    """One attempt's window, rebuilt on demand from the store as it stands."""

    def build() -> PreparedWindow:
        return turn.prepare(
            state,
            rendering=rendering,
            repair_notice=repair_notice,
            provider_turns=store.turns,
        )

    return build


async def send_executing_turn(
    agent: ActionAgent,
    *,
    sink: StateTokenSink,
    prepared: PreparedWindow,
    prepare: Callable[[], PreparedWindow],
    store: ProviderTurnStore,
    connection: LlmConnection | None,
    tools: tuple[LlmToolDefinition, ...],
    max_parallel_tool_calls: int,
    system_instruction: str,
) -> tuple[ActionTurnReply, PreparedWindow]:
    """Send ``prepared``; on a refused input, resend once without the turns.

    Returns the reply and the window that produced it, whose fingerprint the
    reply's own provider turn is recorded with.
    """

    async def send(window: PreparedWindow) -> ActionTurnReply:
        return await agent._generate_llm_action_turn(
            sink=sink,
            prompt=window.prompt,
            conversation=window.conversation,
            tools=tools,
            max_parallel_tool_calls=max_parallel_tool_calls,
            system_instruction=system_instruction,
            file_inputs=list(window.file_inputs),
            stage=EXECUTING_STAGE,
        )

    # A resent window is rebuilt from the emptied store, so only its conversation
    # differs; the recorded prompt and the input estimate stay the first one's.
    with bind_request_llm_connection(connection):
        return await send_dropping_refused_turns(
            send, prepared=prepared, prepare=prepare, store=store
        )


__all__ = ["EXECUTING_STAGE", "build_executing_window", "send_executing_turn"]
