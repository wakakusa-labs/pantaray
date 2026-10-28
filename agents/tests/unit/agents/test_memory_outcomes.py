"""The outcome stage continues the Memory update's own conversation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from pantaray_agents.agents.memory_agent.outcomes import (
    RECORD_OUTCOMES_TOOL_NAME,
    judge_outcomes,
    parse_outcomes,
)
from pantaray_agents.agents.memory_file_editor.runner import run_memory_file_editor
from pantaray_agents.local_runtime.runtime.suggestion_outcomes import DueOutcome
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)

from .test_memory_file_editor_runner import (
    _read_tool,
    _reply,
    _run_input,
    _scripted,
)

_EVIDENCE: dict[str, JSONValue] = {
    "source": "activity_summary",
    "ref": "summary-1",
    "observed_at": "2026-02-25",
    "note": "Reviews closed within a day.",
}
_OBSERVED = '{"output": {"summary_id": "summary-1"}}'


def _failing_tool() -> ReactToolDefinition:
    async def execute(_call: object, _step_number: int) -> ReactToolResult:
        return tool_error_response(
            tool_name="lookup", error_code="NOT_FOUND", message="no summary-1 here"
        )

    return ReactToolDefinition(
        name="lookup",
        description="Look a record up.",
        request_schema={"type": "object"},
        response_schema={"type": "object"},
        execute=execute,
    )


def _due(suggestion_id: str, checkpoint_days: int = 7) -> DueOutcome:
    return DueOutcome(
        suggestion_id=suggestion_id,
        checkpoint_days=checkpoint_days,
        delivered_at="2026-02-20T09:00:00.000Z",
        message="Try the shorter checklist.",
        interaction_contract="message_only",
        hypothesis="Reviews close sooner.",
        user_reaction=None,
        accepted_at=None,
        rejected_at=None,
        dismiss_reason=None,
        action_status=None,
        earlier_verdicts=(),
    )


def _outcome(
    suggestion_id: str,
    *,
    checkpoint_days: int = 7,
    verdict: str = "improved",
    evidence: list[JSONValue] | None = None,
) -> dict[str, JSONValue]:
    return {
        "suggestion_id": suggestion_id,
        "checkpoint_days": checkpoint_days,
        "verdict": verdict,
        "evidence": [_EVIDENCE] if evidence is None else evidence,
        "reason": "Reviews closed sooner after the suggestion.",
    }


@pytest.mark.parametrize(
    ("outcomes", "error"),
    [
        ([_outcome("s-1")], "missing"),
        ([_outcome("s-1"), _outcome("s-2"), _outcome("s-3")], "not due"),
        ([_outcome("s-1"), _outcome("s-2", checkpoint_days=1)], "not due"),
        ([_outcome("s-1"), _outcome("s-1"), _outcome("s-2")], "exactly one"),
        ([_outcome("s-1"), _outcome("s-2", verdict="helpful")], "verdict"),
        ([_outcome("s-1"), _outcome("s-2", evidence=[])], "needs evidence"),
        (
            # A prefix of an id a tool returned is not that id.
            [
                _outcome("s-1"),
                _outcome("s-2", evidence=[{**_EVIDENCE, "ref": "summary"}]),
            ],
            "not found",
        ),
    ],
)
def test_an_ending_that_does_not_judge_every_due_item_is_rejected(
    outcomes: list[JSONValue], error: str
) -> None:
    with pytest.raises((ValueError, ValidationError), match=error):
        parse_outcomes(
            {"outcomes": outcomes}, (_due("s-1"), _due("s-2")), observed=_OBSERVED
        )


def test_unknown_needs_no_evidence() -> None:
    (judged,) = parse_outcomes(
        {"outcomes": [_outcome("s-1", verdict="unknown", evidence=[])]},
        (_due("s-1"),),
        observed="",
    )
    assert judged.verdict == "unknown"


@pytest.mark.asyncio
async def test_judging_continues_the_update_behind_the_same_head(
    tmp_path: Path,
) -> None:
    send, requests = _scripted(
        _reply(("read_memory", {"path": "facts.md"})),
        _reply(("completed", {})),
        _reply((RECORD_OUTCOMES_TOOL_NAME, {"outcomes": [_outcome("s-1")]})),
    )
    # The update read summary-1, so the judging stage may cite it.
    conversation = await run_memory_file_editor(
        _run_input(tmp_path, send, tools=(_read_tool("summary-1 closed"),))
    )

    judged = await judge_outcomes(
        conversation,
        (_due("s-1"),),
        tools=(_read_tool(),),
        instruction="Judge each due suggestion.",
        now="2026-03-01 12:00 +00:00",
    )

    assert [(item.suggestion_id, item.verdict) for item in judged] == [
        ("s-1", "improved")
    ]
    update, judging = requests[1], requests[2]
    assert (judging.prompt, judging.system_instruction) == (
        update.prompt,
        update.system_instruction,
    )
    # The update's history goes on: its `completed` call, its answer, and the
    # judging instruction.
    assert judging.conversation[: len(update.conversation)] == update.conversation
    completed, answer, instruction = judging.conversation[len(update.conversation) :]
    assert isinstance(completed, LlmTurnAssistantItem)
    assert isinstance(answer, LlmTurnToolResultItem)
    assert answer.call_id == completed.calls[0].call_id
    assert answer.output == {"status": "published"}
    assert isinstance(instruction, LlmTurnUserItem)
    assert {tool.name for tool in judging.tools} >= {RECORD_OUTCOMES_TOOL_NAME}
    assert "completed" not in {tool.name for tool in judging.tools}


@pytest.mark.asyncio
async def test_the_instruction_survives_omission_and_refs_must_have_been_read(
    tmp_path: Path,
) -> None:
    send, requests = _scripted(
        _reply(("read_memory", {"path": "facts.md"})),
        _reply(("completed", {})),
        # summary-1 is in the due list, an error and then the refusal, which
        # quotes it: none of them shows the record, so a resend is refused too.
        _reply(("lookup", {})),
        _reply((RECORD_OUTCOMES_TOOL_NAME, {"outcomes": [_outcome("s-1")]})),
        _reply((RECORD_OUTCOMES_TOOL_NAME, {"outcomes": [_outcome("s-1")]})),
        _reply(("read_memory", {"path": "summaries"})),
        _reply((RECORD_OUTCOMES_TOOL_NAME, {"outcomes": [_outcome("s-1")]})),
    )
    conversation = await run_memory_file_editor(
        _run_input(tmp_path, send, tools=(_read_tool(),))
    )
    due = replace(_due("s-1"), hypothesis="summary-1 shows reviews closing sooner.")
    # The window has moved past every output up to the judging instruction.
    omitted = replace(
        conversation,
        window=replace(conversation.window, omit_before=len(conversation.history) + 2),
    )

    judged = await judge_outcomes(
        omitted,
        (due,),
        tools=(_read_tool("summary-1 closed in a day"), _failing_tool()),
        instruction="Judge each due suggestion.",
        now="2026-03-01 12:00 +00:00",
    )

    assert [item.verdict for item in judged] == ["improved"]
    answer, instruction = requests[2].conversation[len(conversation.history) :]
    assert isinstance(answer, LlmTurnToolResultItem) and answer.output == "…"
    assert isinstance(instruction, LlmTurnUserItem)
    text = str(instruction.content[0])
    assert "Judge each due suggestion." in text and '"s-1"' in text
    for refused in (requests[4], requests[5]):
        rejected = refused.conversation[-1]
        assert isinstance(rejected, LlmTurnToolResultItem)
        assert "not found" in str(rejected.output)
