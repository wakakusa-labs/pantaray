"""Validate prompt rendering and examples against the runtime contracts."""

import json
import re
from unittest.mock import patch

import pytest

from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionFetchedContext,
    SuggestionStableMemoryContext,
)
from pantaray_agents.agents.suggestion_agent.output import parse_suggestion_output
from pantaray_agents.agents.suggestion_agent.writer import (
    SUGGESTION_WRITER_PROMPT_NAME,
    SUGGESTION_WRITER_VOICE_PROMPT_NAMES,
    build_writer_messages,
)
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.utils.prompt_loader import PromptLoader


@pytest.mark.parametrize(("language", "label"), [("ja", "Japanese"), ("en", "English")])
def test_real_prompt_renders_context_and_answer_language(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
    language: str,
    label: str,
) -> None:
    config = PromptLoader().load_config("suggestion/suggestion")
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        return_value=config,
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=SuggestionStableMemoryContext(
                prompt="stable-memory-evidence",
                has_facts=True,
                has_insights=True,
                pending_work="pending-work-evidence",
            ),
        )

    context: SuggestionFetchedContext = {
        "short_term_insight": "current-work-evidence",
        "reconsideration_reason": "reconsideration-hypothesis",
        "stable_memory_context": "stable-memory-evidence",
        "action_agent_capabilities": "action-capability-limits",
        "recent_suggestions": "suggestions-with-user-responses",
        "recent_activity_descriptions": "observed-activity-evidence",
        "recent_activity_summary_1h": "hourly-summary-evidence",
        "recent_activity_summaries_24h_1w_1m": "longer-summary-evidence",
        "context_density_signal": "context-availability-signal",
        "workspace_context_prompt": "registered-workspace-context",
    }
    agent._current_language = language  # noqa: SLF001
    rendered = agent._build_prompt(context)  # noqa: SLF001
    instruction = agent._system_instruction_for_request()  # noqa: SLF001

    for value in (*context.values(), "pending-work-evidence"):
        assert value in rendered
    assert f"one short sentence in {label} for the writer" in instruction
    assert "{answer_language}" not in instruction


@pytest.mark.parametrize("label", ["Japanese", "English"])
def test_real_writer_prompt_renders_only_the_decided_content(label: str) -> None:
    loader = PromptLoader()
    voice_name = SUGGESTION_WRITER_VOICE_PROMPT_NAMES.get(label)
    system, prompt = build_writer_messages(
        loader.load_config(SUGGESTION_WRITER_PROMPT_NAME),
        {
            "interaction_contract": "action_offer",
            "key_point": "decided-point",
            "deliverable": "decided-deliverable",
            "agent_session": True,
        },
        answer_language=label,
        voice_instruction=(
            loader.load_config(voice_name).system_instruction if voice_name else None
        ),
    )

    assert f"natural {label}" in system
    assert ("## Writing in Japanese" in system) is (label == "Japanese")
    assert "{" not in prompt
    for value in ("offer", "decided-point", "decided-deliverable", "yes"):
        assert value in prompt


def test_prompt_examples_are_accepted_by_the_suggestion_parser(
    suggestion_agent: SuggestionAgent,
) -> None:
    config = PromptLoader().load_config("suggestion/suggestion")
    examples = re.findall(
        r"```json\s*(.*?)\s*```", config.system_instruction or "", re.S
    )
    assert examples
    for example in examples:
        payload = json.loads(example)
        parsed = parse_suggestion_output(raw_text=example, parsed_output=None)
        assert parsed["has_suggestion"] == payload["has_suggestion"]
        assert parsed["interaction_contract"] == payload["interaction_contract"]
