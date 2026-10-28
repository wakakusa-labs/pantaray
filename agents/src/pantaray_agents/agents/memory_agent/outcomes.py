"""The outcome stage: a Memory update, its memory published, continues to judge
what delivered suggestions changed, and ends with ``record_outcomes``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pantaray_agents.agents.memory_file_editor.runner import MemoryEditorConversation
from pantaray_agents.conversation.loop import Continue, Finish, IdleTurn
from pantaray_agents.local_runtime.runtime.suggestion_outcomes import (
    DueOutcome,
    JudgedOutcome,
    OutcomeEvidence,
    OutcomeVerdictValue,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolDefinition
from pantaray_agents.utils.local_time import describe_utc_timestamp
from pantaray_llm.contracts.conversation import LlmTurnToolResultItem
from pantaray_llm.contracts.tool_use import LlmToolDefinition

OUTCOMES_PROMPT_NAME = "memory_update_outcomes"
OUTCOMES_PROMPT_VERSION = "1.0"
RECORD_OUTCOMES_TOOL_NAME = "record_outcomes"
# Its own bound, apart from the update's: a few lookups per suggestion, at most
# MAX_OUTCOMES_PER_RUN of them. No measurement yet; widen when real runs stop
# at the limit with records still to read.
OUTCOMES_MAX_LLM_TURNS = 12
OUTCOMES_MAX_TOOL_CALLS = 24
_RECORD_REQUIRED = (
    "Respond with tool calls. When every due suggestion is judged, call "
    f"`{RECORD_OUTCOMES_TOOL_NAME}` alone."
)
_VERDICTS: tuple[str, ...] = get_args(OutcomeVerdictValue.__value__)


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    observed_at: str
    note: str = Field(min_length=1)


class _Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion_id: str
    checkpoint_days: int
    verdict: OutcomeVerdictValue
    evidence: list[_Evidence]
    reason: str = Field(min_length=1)


class _Outcomes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcomes: list[_Outcome]


def record_outcomes_tool(due: tuple[DueOutcome, ...]) -> LlmToolDefinition:
    evidence: dict[str, JSONValue] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "ref", "observed_at", "note"],
        "properties": {
            "source": {"type": "string"},
            "ref": {"type": "string"},
            "observed_at": {"type": "string"},
            "note": {"type": "string"},
        },
    }
    return LlmToolDefinition(
        name=RECORD_OUTCOMES_TOOL_NAME,
        description="Record one outcome verdict for every due suggestion.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["outcomes"],
            "properties": {
                "outcomes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "suggestion_id",
                            "checkpoint_days",
                            "verdict",
                            "evidence",
                            "reason",
                        ],
                        "properties": {
                            "suggestion_id": {
                                "type": "string",
                                "enum": [item.suggestion_id for item in due],
                            },
                            "checkpoint_days": {"type": "integer"},
                            "verdict": {"type": "string", "enum": list(_VERDICTS)},
                            "evidence": {"type": "array", "items": evidence},
                            "reason": {"type": "string"},
                        },
                    },
                }
            },
        },
    )


def parse_outcomes(
    arguments: Mapping[str, object], due: tuple[DueOutcome, ...], *, observed: str
) -> tuple[JudgedOutcome, ...]:
    """The verdicts, one per due item; ValueError says what to fix.

    Verdicts are never rewritten, so every evidence ref has to appear verbatim
    in ``observed``: the tool results the run received.
    """

    outcomes = _Outcomes.model_validate(arguments).outcomes
    keys = [(item.suggestion_id, item.checkpoint_days) for item in outcomes]
    expected = {(item.suggestion_id, item.checkpoint_days) for item in due}
    missing = sorted(expected - set(keys))
    extra = sorted(set(keys) - expected)
    if missing or extra or len(keys) != len(set(keys)):
        raise ValueError(
            "give exactly one outcome per due item; "
            f"missing: {missing}, not due: {extra}"
        )
    unsupported = [
        item.suggestion_id
        for item in outcomes
        if item.verdict != "unknown" and not item.evidence
    ]
    if unsupported:
        raise ValueError(
            f"{unsupported}: a verdict other than unknown needs evidence; "
            "cite the records or judge it unknown"
        )
    unseen = sorted(
        {
            entry.ref
            for item in outcomes
            for entry in item.evidence
            if entry.ref not in observed
        }
    )
    if unseen:
        raise ValueError(
            f"refs {unseen} were not found in any tool result you received; "
            "cite each id or path exactly as a tool returned it, with no "
            "abbreviation or ellipsis, or judge the suggestion unknown"
        )
    return tuple(
        JudgedOutcome(
            suggestion_id=item.suggestion_id,
            checkpoint_days=item.checkpoint_days,
            verdict=item.verdict,
            evidence=tuple(
                OutcomeEvidence(
                    source=entry.source,
                    ref=entry.ref,
                    observed_at=entry.observed_at,
                    note=entry.note,
                )
                for entry in item.evidence
            ),
            reason=item.reason,
        )
        for item in outcomes
    )


def _local(value: str | None) -> str | None:
    return None if value is None else describe_utc_timestamp(value)


def _due_item(item: DueOutcome) -> JSONValue:
    return {
        "suggestion_id": item.suggestion_id,
        "checkpoint_days": item.checkpoint_days,
        "delivered_at": describe_utc_timestamp(item.delivered_at),
        "message": item.message,
        "interaction_contract": item.interaction_contract,
        "hypothesis": item.hypothesis,
        "context_not_evidence": {
            "user_reaction": item.user_reaction,
            "accepted_at": _local(item.accepted_at),
            "rejected_at": _local(item.rejected_at),
            "dismiss_reason": item.dismiss_reason,
            "action_status": item.action_status,
        },
        "earlier_verdicts": [
            {
                "checkpoint_days": verdict.checkpoint_days,
                "verdict": verdict.verdict,
                "evidence": [
                    {
                        "source": entry["source"],
                        "ref": entry["ref"],
                        "observed_at": entry["observed_at"],
                        "note": entry["note"],
                    }
                    for entry in verdict.evidence
                ],
                "reason": verdict.reason,
                "judged_at": describe_utc_timestamp(verdict.judged_at),
            }
            for verdict in item.earlier_verdicts
        ],
    }


async def judge_outcomes(
    conversation: MemoryEditorConversation,
    due: tuple[DueOutcome, ...],
    *,
    tools: tuple[ReactToolDefinition, ...],
    instruction: str,
    now: str,
) -> tuple[JudgedOutcome, ...]:
    """Continue the published update to its accepted ``record_outcomes``."""

    # What the model received from tools, in both stages; the due list is not
    # a tool result, so citing it alone proves nothing.
    observed = [
        entry.item.model_dump_json()
        for entry in conversation.history
        if isinstance(entry.item, LlmTurnToolResultItem)
    ]

    def observe(item: LlmTurnToolResultItem) -> None:
        observed.append(item.model_dump_json())

    def decide(turn: IdleTurn) -> Finish[tuple[JudgedOutcome, ...]] | Continue:
        if turn.ending_call is None:
            return Continue(_RECORD_REQUIRED)
        try:
            return Finish(
                parse_outcomes(
                    turn.ending_call.arguments, due, observed="\n".join(observed)
                )
            )
        except (ValidationError, ValueError) as exc:
            return Continue(
                f"{RECORD_OUTCOMES_TOOL_NAME} was rejected: {str(exc)[:1200]}"
            )

    due_json = json.dumps(
        [_due_item(item) for item in due], ensure_ascii=False, indent=1
    )
    return await conversation.continue_with(
        answer={"status": "published"},
        message=f"{instruction}\n\nNow: {now}\n\n<due>\n{due_json}\n</due>",
        tools=tools,
        ending_tool=record_outcomes_tool(due),
        decide=decide,
        observe=observe,
        max_turns=OUTCOMES_MAX_LLM_TURNS,
        max_tool_calls=OUTCOMES_MAX_TOOL_CALLS,
    )


__all__ = [
    "OUTCOMES_MAX_LLM_TURNS",
    "OUTCOMES_MAX_TOOL_CALLS",
    "OUTCOMES_PROMPT_NAME",
    "OUTCOMES_PROMPT_VERSION",
    "RECORD_OUTCOMES_TOOL_NAME",
    "judge_outcomes",
    "parse_outcomes",
    "record_outcomes_tool",
]
