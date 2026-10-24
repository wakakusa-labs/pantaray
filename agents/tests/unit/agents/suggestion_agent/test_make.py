"""The selected lens run continues its own conversation to make its files."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import (
    MAKE_CONFIG,
    NUDGE_CONFIG,
    serve_lens_runs,
)

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    ActionTurnReply,
    LlmToolCallTurn,
)
from pantaray_agents.agents.suggestion_agent.lenses import (
    SUGGESTION_LENS_PROMPT_NAME,
    SUGGESTION_SELECTOR_PROMPT_NAME,
    SampledLens,
    decide_with_lenses,
)
from pantaray_agents.agents.suggestion_agent.make import FINISH_SUGGESTION_TOOL_NAME
from pantaray_agents.agents.suggestion_agent.nudges import PastSuggestions
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
    SuggestionMakeTools,
)
from pantaray_agents.conversation.loop import ConversationRequest
from pantaray_agents.local_runtime.tooling.suggestion_research.artifact_patch import (
    SuggestionArtifactPatch,
)
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.schema.agent.suggestion import SuggestionAgentRequest
from pantaray_agents.utils.prompt_loader import PromptLoader
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.conversation import LlmTurnToolResultItem
from pantaray_llm.contracts.tool_use import LlmToolCall

CHOSEN, OTHER = "take_over", "perspective"
POINT = "Pantaray can draft the reply to the venue."
MADE = "I drafted the reply to the venue."
SUBMIT = {
    "has_suggestion": True,
    "interaction_contract": "action_offer",
    "key_point": POINT,
    "suggestion_summary": "### Target\nThe venue reply",
    "target_context": {"organization_name": None, "project_name": None},
    "candidates": [],
}
WRITE = {
    "changes": [
        {"op": "add", "path": "reply.md", "new_lines": ["Hi"], "trailing_newline": True}
    ]
}


def _finish(point: str | None = MADE, artifacts: tuple[str, ...] = ("reply.md",)):
    return (
        FINISH_SUGGESTION_TOOL_NAME,
        {
            "has_suggestion": point is not None,
            "interaction_contract": None if point is None else "message_only",
            "key_point": point or "",
            "artifacts": list(artifacts),
        },
    )


class _Model:
    """Plays each lens run's turns, then the chosen run's make-stage turns."""

    def __init__(self, *, research: list, make: list) -> None:
        self.ids = itertools.count()
        self.turns = {
            (CHOSEN, False): [*research, ("submit_suggestion", SUBMIT)],
            (OTHER, False): [("submit_suggestion", {**SUBMIT, "key_point": "Else."})],
            (CHOSEN, True): make,
        }
        self.requests: dict[tuple[str, bool], list[ConversationRequest]] = {}
        self.calls: list[LlmToolCall] = []

    async def send(self, request: ConversationRequest, _sink: object):
        lens = CHOSEN if f"### {CHOSEN}" in str(request.conversation[0]) else OTHER
        key = (lens, FINISH_SUGGESTION_TOOL_NAME in {t.name for t in request.tools})
        self.requests.setdefault(key, []).append(request)
        name, arguments = self.turns[key].pop(0)
        call = LlmToolCall(call_id=f"c{next(self.ids)}", name=name, arguments=arguments)
        self.calls.append(call)
        return ActionTurnReply(
            response=LlmActionTurnResponse(
                mode="action_turn", messages=[], calls=[call]
            ),
            provider_turn=None,
        )


async def _select_first(**_kwargs: object) -> LlmToolCallTurn:
    arguments = {"choice": 1, "reason": "r"}
    return LlmToolCallTurn(
        calls=(LlmToolCall(call_id="s", name="select_suggestion", arguments=arguments),)
    )


async def _decide(model: _Model, folder: Path, steps: list[ReactLoopStep]):
    loader = PromptLoader()
    lens_config = loader.load_config(SUGGESTION_LENS_PROMPT_NAME)
    lens_config.role_rules = {lens: f"### {lens}" for lens in (CHOSEN, OTHER)}
    research = build_mock_suggestion_research_tools().definitions
    folder.mkdir()
    patch = SuggestionArtifactPatch(folder.resolve()).definition()
    tools = FixedSuggestionResearchTools(
        research, SuggestionMakeTools((*research, patch), folder.resolve())
    )

    async def record(step: ReactLoopStep) -> None:
        steps.append(step)

    return await decide_with_lenses(
        user_id="user",
        suggestion_id="sug",
        initial_prompt="shared context",
        system_instruction="rules",
        current_time="now",
        recent_suggestions="",
        past=PastSuggestions((), datetime(2026, 10, 24, tzinfo=UTC)),
        lenses=(SampledLens(CHOSEN, 0), SampledLens(OTHER, 0)),
        lens_config=lens_config,
        selector_config=loader.load_config(SUGGESTION_SELECTOR_PROMPT_NAME),
        nudge_config=NUDGE_CONFIG,
        make_config=MAKE_CONFIG,
        research_tools=tools,
        send_turn=model.send,
        generate_tool_call=_select_first,
        record_step=record,
    )


@pytest.mark.asyncio
async def test_the_selected_run_continues_its_research_to_make_a_file(
    tmp_path: Path,
) -> None:
    # A file it never made is refused before it writes one.
    model = _Model(
        research=[("memory_search", {"query": "venue"})],
        make=[_finish(), ("apply_patch", WRITE), _finish()],
    )
    steps: list[ReactLoopStep] = []

    decision = await _decide(model, tmp_path / "made", steps)

    research_call = model.calls[0]
    answered = {
        item.call_id: item.output
        for item in model.requests[(CHOSEN, True)][0].conversation
        if isinstance(item, LlmTurnToolResultItem)
    }
    # The same conversation: its research, then its submission answered.
    assert research_call.call_id in answered
    assert [o["status"] for o in answered.values()].count("selected") == 1
    assert decision.extraction["artifacts"] == ["reply.md"]
    assert decision.extraction["decided"] == {
        "interaction_contract": "message_only",
        "key_point": MADE,
    }
    assert (tmp_path / "made" / "reply.md").read_text() == "Hi\n"
    # Only the chosen run's make stage could write.
    for key, requests in model.requests.items():
        names = {tool.name for request in requests for tool in request.tools}
        assert ("apply_patch" in names) is (key == (CHOSEN, True))
    # The history reads the lens from the latest llm step with a tool_output.
    recorded = [s for s in steps if s.step_kind == "llm" and s.tool_output]
    latest = max(recorded, key=lambda step: step.step_number)
    assert latest.tool_output == {"selected_lens": CHOSEN}
    assert max(step.step_number for step in steps) > latest.step_number


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "kept"),
    [
        # Nothing made: the decision stands as it was.
        ([_finish(POINT, ())], POINT),
        # Making it showed the premise was wrong: the Suggestion is dropped.
        ([("apply_patch", WRITE), _finish(None, ())], None),
    ],
)
async def test_without_files_the_decision_stands_or_is_dropped(
    tmp_path: Path, make: list, kept: str | None
) -> None:
    decision = await _decide(_Model(research=[], make=make), tmp_path / "made", [])

    decided = decision.extraction["decided"]
    assert (decided and decided["key_point"]) == kept
    assert decision.extraction["artifacts"] == []


@pytest.mark.asyncio
async def test_the_files_made_reach_the_agent_response(
    suggestion_agent, mock_llm_client, tmp_path: Path
) -> None:
    serve_lens_runs(mock_llm_client, SUBMIT)
    name, arguments = _finish()
    mock_llm_client.queued_responses[-1] = {"tool_id": name, "args": arguments}
    # The fixture's artifact folder, as the make stage left it.
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "reply.md").write_text("Hi\n")

    response = await suggestion_agent.process(
        SuggestionAgentRequest(
            suggestion_id="sug-made",
            user_id="user-test",
            short_term_insight="Replying to the venue.",
            reconsideration_reason="The venue asked for a date.",
        )
    )

    assert response.interaction_contract == "message_only"
    assert response.artifacts == ["reply.md"]
