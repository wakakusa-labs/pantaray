"""Validate prompt rendering and examples against the runtime contracts."""

import json
import re
from unittest.mock import patch

import pytest

from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.agent import (
    SUGGESTION_INITIAL_PROMPT_MAX_CHARS,
)
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionFetchedContext,
    SuggestionOpenTodo,
    SuggestionOpenTodos,
    SuggestionStableMemoryContext,
)
from pantaray_agents.agents.suggestion_agent.output import parse_suggestion_output
from pantaray_agents.agents.suggestion_agent.writer import (
    SUGGESTION_WRITER_PROMPT_NAME,
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
                open_todos=SuggestionOpenTodos(
                    (SuggestionOpenTodo("open-todo-evidence", None, None, "partial"),)
                ),
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
        "recent_activity_summary_24h": "daily-summary-evidence",
        "context_density_signal": "context-availability-signal",
        "workspace_context_prompt": "registered-workspace-context",
    }
    agent._current_language = language  # noqa: SLF001
    rendered = agent._build_prompt(context)  # noqa: SLF001
    instruction = agent._system_instruction_for_request()  # noqa: SLF001

    for value in (
        *context.values(),
        "pending-work-evidence",
        "## Open TODOs\n",
        "- open-todo-evidence (no project; partly done)\n",
    ):
        assert value in rendered
    assert f"the suggestion itself in {label}," in instruction
    assert "{answer_language}" not in instruction


def test_open_todos_take_only_the_room_the_other_sections_leave(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    todos = tuple(
        SuggestionOpenTodo(f"todo-{index:02d} " + "t" * 490, None, None, "not_started")
        for index in range(50)
    )
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
                prompt="m" * 5_000,
                has_facts=True,
                has_insights=True,
                # The snapshot's bound for insights/todos.md.
                pending_work="p" * 60_000,
                open_todos=SuggestionOpenTodos(todos, omitted=3),
            ),
        )
    context: SuggestionFetchedContext = {
        "short_term_insight": "i" * 6_000,
        "reconsideration_reason": "reason",
        "stable_memory_context": "m" * 5_000,
        "action_agent_capabilities": "capabilities",
        # Near the recent suggestions' 30,000-character bound.
        "recent_suggestions": "h" * 29_000,
        "recent_activity_descriptions": "activities",
        "recent_activity_summary_1h": "hourly",
        "recent_activity_summary_24h": "daily",
        "context_density_signal": "context_density: high",
        "workspace_context_prompt": "workspace",
    }
    agent._current_language = "en"  # noqa: SLF001

    rendered = agent._build_prompt(context)  # noqa: SLF001

    assert len(rendered) <= SUGGESTION_INITIAL_PROMPT_MAX_CHARS
    shown = re.findall(r"^- todo-(\d\d) ", rendered, re.M)
    # The newest TODOs stay, the oldest go, and the note counts every one left out.
    assert shown and shown[-1] == "49" and shown[0] != "00"
    assert f"[{3 + int(shown[0])} older open TODOs not shown]" in rendered


@pytest.mark.parametrize("label", ["Japanese", "English"])
def test_real_writer_prompt_renders_only_the_decided_content(label: str) -> None:
    system, prompt = build_writer_messages(
        PromptLoader().load_config(SUGGESTION_WRITER_PROMPT_NAME),
        {
            "interaction_contract": "action_offer",
            "key_point": "decided-point",
        },
        answer_language=label,
    )

    assert f"natural {label}" in system
    assert "{" not in prompt
    for value in ("offer", "decided-point"):
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
