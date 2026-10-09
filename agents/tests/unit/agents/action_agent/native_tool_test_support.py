from __future__ import annotations

import json

from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import ActionTurnReply
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.tool_use import LlmToolCall

DEFAULT_STEP_NOTE = "Reviewed the request and am running the next check."


def native_tool_turn(
    name: str,
    arguments: dict[str, JSONValue],
    *,
    call_id: str = "call-test",
    step_note: str | None = DEFAULT_STEP_NOTE,
) -> ActionTurnReply:
    """Build a supervisor turn of one tool call.

    Every supervisor tool schema carries a required ``step_note``; pass
    ``step_note=None`` to reproduce a call that omits it.
    """

    call_arguments: dict[str, JSONValue] = dict(arguments)
    if step_note is not None and STEP_NOTE_ARG not in call_arguments:
        call_arguments[STEP_NOTE_ARG] = step_note
    response = LlmActionTurnResponse(
        mode="action_turn",
        messages=[],
        calls=[
            LlmToolCall(
                call_id=call_id,
                name=name,
                arguments=call_arguments,
            ),
        ],
    )
    return ActionTurnReply(response=response, provider_turn=None)


def native_tool_turn_from_json(payload: str) -> ActionTurnReply:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise TypeError("test payload must be an object")
    name = parsed.get("tool_id")
    arguments = parsed.get("args")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise TypeError("test payload must contain tool_id and args")
    return native_tool_turn(name, arguments)


def native_tool_turn_sequence(*payloads: str) -> list[ActionTurnReply]:
    return [native_tool_turn_from_json(payload) for payload in payloads]


__all__ = [
    "DEFAULT_STEP_NOTE",
    "native_tool_turn",
    "native_tool_turn_from_json",
    "native_tool_turn_sequence",
]
