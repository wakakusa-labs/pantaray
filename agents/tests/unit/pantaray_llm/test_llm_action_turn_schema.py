from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.pantaray_llm.test_llm_tool_use_schema import _tool

from pantaray_llm.contracts.action_turn import (
    ACTION_COMMENTARY_MAX_CHARACTERS,
    LlmActionTurnRequest,
    LlmActionTurnResponse,
    LlmCommentary,
)
from pantaray_llm.contracts.tool_use import LlmToolUseRequest


@pytest.mark.parametrize(
    "with_message, with_call", [(True, False), (False, True), (True, True)]
)
def test_action_turn_wire_preserves_messages_and_calls(
    with_message: bool, with_call: bool
) -> None:
    message = {
        "phase": "commentary",
        "source_message_id": "msg-1",
        "text": "まず調べます。",
    }
    call = {"call_id": "call-1", "name": "completed", "arguments": {}}
    payload = {
        "mode": "action_turn",
        "messages": [message] if with_message else [],
        "calls": [call] if with_call else [],
        "dropped_call_names": [],
    }
    assert (
        LlmActionTurnResponse.model_validate(payload).model_dump(mode="json") == payload
    )


def test_action_turn_rejects_empty_response() -> None:
    with pytest.raises(ValidationError, match="commentary or tool calls"):
        LlmActionTurnResponse(mode="action_turn", messages=[], calls=[])


@pytest.mark.parametrize("phase", [None, "final_answer", "unknown"])
def test_commentary_requires_explicit_commentary_phase(phase: str | None) -> None:
    message = {"source_message_id": "msg-1", "text": "まず調べます。"}
    if phase is not None:
        message["phase"] = phase
    with pytest.raises(ValidationError):
        LlmCommentary.model_validate(message)


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_commentary_rejects_blank_text(text: str) -> None:
    with pytest.raises(ValidationError):
        LlmCommentary(phase="commentary", source_message_id="msg-1", text=text)


def test_commentary_limit_applies_only_beside_calls() -> None:
    long = LlmCommentary(
        phase="commentary",
        source_message_id="msg-1",
        text="x" * (ACTION_COMMENTARY_MAX_CHARACTERS + 1),
    )
    call = {"call_id": "call-1", "name": "completed", "arguments": {}}
    # Without calls the text is the reply that ends the turn, however long.
    assert LlmActionTurnResponse(mode="action_turn", messages=[long], calls=[])
    with pytest.raises(ValidationError, match="beside tool calls"):
        LlmActionTurnResponse.model_validate(
            {"mode": "action_turn", "messages": [long], "calls": [call]}
        )


def test_action_request_requires_explicit_mode_and_disallows_continuation() -> None:
    tool = _tool().model_dump(mode="json")
    with pytest.raises(ValidationError, match="mode"):
        LlmActionTurnRequest.model_validate({"tools": [tool]})
    with pytest.raises(ValidationError, match="continuation_mode"):
        LlmActionTurnRequest.model_validate(
            {"mode": "action_turn", "tools": [tool], "continuation_mode": "stateless"}
        )


@pytest.mark.parametrize("action_turn", [False, True])
def test_native_requests_reject_duplicate_tool_names(action_turn: bool) -> None:
    with pytest.raises(ValidationError, match="tool names must be unique"):
        if action_turn:
            LlmActionTurnRequest(mode="action_turn", tools=[_tool(), _tool()])
        else:
            LlmToolUseRequest(tools=[_tool(), _tool()], continuation_mode="disabled")
