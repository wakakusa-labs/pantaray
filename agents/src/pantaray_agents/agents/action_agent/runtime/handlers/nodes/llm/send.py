"""Send one Executing THINK, and answer a refused replay by dropping it once.

The projection hands a turn back only behind the prefix it was produced behind,
which is everything the desktop can check. What it cannot is who the provider
now is behind a route: on the cloud route the operator may move a profile to
another model or organization, and an ``encrypted_content`` or a signature from
the old one is refused. When the provider refuses an input that carried turns,
this run discards the turns it holds and sends the same turn once more without
them, dropping nothing else, so a request refused for anything else in its input
is refused again and raised exactly as it was before. Turns produced after that
go back as usual.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from pantaray_agents.local_runtime.runtime.connection_store import (
    LlmConnection,
    bind_request_llm_connection,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.conversation import LlmTurnAssistantItem
from pantaray_llm.contracts.tool_use import LlmToolDefinition
from pantaray_llm.errors import PROXY_INVALID_INPUT, LlmProxyExecutionError

from .context_budget import PreparedWindow
from .provider_turns import ActionProviderTurnStore
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

logger = logging.getLogger(__name__)
# The stage this node bills and, through the agent's stage map, the inference
# profile every Executing THINK of a run is sent under.
EXECUTING_STAGE = "executing"


def build_executing_window(
    turn: ExecutingTurn,
    state: ActionAgentState,
    *,
    rendering: PromptRenderingService,
    repair_notice: str,
    store: ActionProviderTurnStore,
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
    store: ActionProviderTurnStore,
    connection: LlmConnection | None,
    tools: tuple[LlmToolDefinition, ...],
    max_parallel_tool_calls: int,
    system_instruction: str,
) -> tuple[LlmActionTurnResponse, PreparedWindow]:
    """Send ``prepared``; on a refused input, resend once without the turns.

    Returns the response and the window that produced it, whose fingerprint the
    response's own provider turn is recorded with.
    """

    async def send(
        window: PreparedWindow,
    ) -> tuple[LlmActionTurnResponse, PreparedWindow]:
        response = await agent._generate_llm_action_turn(
            sink=sink,
            prompt=window.prompt,
            conversation=window.conversation,
            tools=tools,
            max_parallel_tool_calls=max_parallel_tool_calls,
            system_instruction=system_instruction,
            file_inputs=list(window.file_inputs),
            stage=EXECUTING_STAGE,
        )
        return response, window

    with bind_request_llm_connection(connection):
        try:
            return await send(prepared)
        except LlmProxyExecutionError as exc:
            # Only a request that carried one is resent, so a refusal about
            # anything else in the input is raised exactly where it was.
            if exc.error_code != PROXY_INVALID_INPUT or not any(
                isinstance(item, LlmTurnAssistantItem)
                and item.provider_turn is not None
                for item in prepared.conversation or ()
            ):
                raise
            store.turns.clear()
            logger.warning(
                "Execution THINK discarded this run's provider turns: the provider "
                "refused the input (%s). Resending without them, which also drops "
                "this run's prompt cache read for one turn.",
                exc.upstream_code or exc.error_code,
            )
        # The window is rebuilt from the store, which now holds nothing. Only the
        # conversation differs; the recorded prompt and the input estimate do not.
        return await send(prepare())


__all__ = ["EXECUTING_STAGE", "build_executing_window", "send_executing_turn"]
