"""Prompt configs for SuggestionAgent tests that replace the real prompt files."""

from collections.abc import Callable

from pantaray_agents.agents.suggestion_agent.lenses import (
    LENS_WEIGHTS,
    SUGGESTION_LENS_PROMPT_NAME,
    SUGGESTION_SELECTOR_PROMPT_NAME,
)
from pantaray_agents.agents.suggestion_agent.make import SUGGESTION_MAKE_PROMPT_NAME
from pantaray_agents.agents.suggestion_agent.nudges import (
    SUGGESTION_NUDGES_PROMPT_NAME,
)
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.utils.prompt_loader import PromptConfig, PromptLoader

LENS_CONFIG = PromptConfig(
    prompt="Lens: {lens}",
    role_rules={lens: f"### {lens}" for lens in LENS_WEIGHTS},
)
SELECTOR_CONFIG = PromptConfig(
    prompt="select: {current_time} | {recent_suggestions} | {candidates}",
    system_instruction="Choose one.",
)

NUDGE_CONFIG = PromptLoader().load_config(SUGGESTION_NUDGES_PROMPT_NAME)
MAKE_CONFIG = PromptLoader().load_config(SUGGESTION_MAKE_PROMPT_NAME)


def prompt_configs(
    test_prompt: str, extra: dict[str, PromptConfig] | None = None
) -> Callable[[str], PromptConfig]:
    """Serve the lens and selector configs, `extra` by name, and test_prompt otherwise."""

    configs = {
        SUGGESTION_LENS_PROMPT_NAME: LENS_CONFIG,
        SUGGESTION_SELECTOR_PROMPT_NAME: SELECTOR_CONFIG,
        SUGGESTION_NUDGES_PROMPT_NAME: NUDGE_CONFIG,
        SUGGESTION_MAKE_PROMPT_NAME: MAKE_CONFIG,
        **(extra or {}),
    }
    return lambda name: configs.get(
        name, PromptConfig(prompt=test_prompt, system_instruction=None)
    )


NO_SUGGESTION = {
    "has_suggestion": False,
    "interaction_contract": None,
    "key_point": "",
    "suggestion_summary": None,
    "target_context": None,
}
SELECT_FIRST = {
    "tool_id": "select_suggestion",
    "args": {"choice": 1, "reason": "test"},
}


def serve_lens_runs(client: MockLLMClient, first: object) -> None:
    """The first lens run submits `first`, the other two find nothing, and the
    selector picks the first candidate when there is one, which then finishes
    its make stage with the same decision and no files."""

    client.set_next_response(first)
    suggested = isinstance(first, dict) and first.get("has_suggestion") is True
    kept = (
        {
            "tool_id": "finish_suggestion",
            "args": {
                "has_suggestion": True,
                "interaction_contract": first["interaction_contract"],
                "key_point": first["key_point"],
                "artifacts": [],
            },
        }
        if suggested
        else None
    )
    client.queued_responses = [
        NO_SUGGESTION,
        NO_SUGGESTION,
        *([SELECT_FIRST, kept] if suggested else []),
    ]
