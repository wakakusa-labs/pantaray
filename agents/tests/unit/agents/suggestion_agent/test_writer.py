"""The writer call turns the decided content into the user-facing Suggestion text."""

import pytest

from pantaray_agents.agents.core.mixins import llm_generation_mixin as mixin_mod
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.writer import (
    SUGGESTION_WRITER_VOICE_PROMPT_NAMES,
)
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import SuggestionAgentRequest
from pantaray_agents.utils.prompt_loader import PromptConfig

POINT = "The client asked twice for the March invoice; it is still unsent."
DELIVERABLE = "A reply to the client with the March invoice attached."
SUMMARY = "ACTION-ONLY summary: procedure, conditions and ambiguity."
DETAILS = "DECISION-ONLY details: supporting facts for the run."
INSIGHT = "RUN-ONLY insight: the user reviewed the billing sheet."
WRITTEN = "3月の請求書、先方から2回催促が来ています。添付して返信文を書きましょうか？"


def _request() -> SuggestionAgentRequest:
    return SuggestionAgentRequest(
        suggestion_id="sug-writer",
        user_id="user-test",
        short_term_insight=INSIGHT,
        reconsideration_reason="The invoice is overdue.",
    )


def _decision() -> dict[str, object]:
    return {
        "has_suggestion": True,
        "interaction_contract": "action_offer",
        "key_point": POINT,
        "details": DETAILS,
        "deliverable": DELIVERABLE,
        "agent_session": False,
        "suggestion_summary": SUMMARY,
        "target_context": {"organization_name": None, "project_name": None},
    }


class _SpyConfig:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)


def _steps(repository: MockSuggestionAgentRepository) -> list[dict[str, object]]:
    rows = repository.data.get("suggestion_run_steps", [])
    return sorted(
        (row for row in rows if row["suggestion_id"] == "sug-writer"),
        key=lambda row: row["step_number"],
    )


@pytest.mark.asyncio
async def test_the_writer_receives_only_the_decided_content(
    suggestion_agent: SuggestionAgent,
    mock_llm_client: MockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SpyConfig.calls = []
    monkeypatch.setattr(mixin_mod.types, "GenerateContentConfig", _SpyConfig)
    mock_llm_client.set_next_response(_decision())
    mock_llm_client.responses["default"] = WRITTEN

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.SUCCESS
    writer_prompt = mock_llm_client.last_prompt
    assert writer_prompt is not None
    assert POINT in writer_prompt
    assert DELIVERABLE in writer_prompt
    for leaked in (SUMMARY, DETAILS, INSIGHT, "suggestion task"):
        assert leaked not in writer_prompt
    writer_config = _SpyConfig.calls[-1]
    assert writer_config["system_instruction"] == "Write one message in English."
    assert writer_config["inference_profile"] == "suggestion"


@pytest.mark.asyncio
async def test_the_published_answer_comes_from_the_writer(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    mock_llm_client.set_next_response(_decision())
    mock_llm_client.responses["default"] = WRITTEN

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.SUCCESS
    assert response.has_suggestion is True
    assert response.answer == WRITTEN
    assert response.suggestion_summary == SUMMARY
    writer_step = _steps(mock_repository)[-1]
    assert writer_step["step_kind"] == "llm"
    assert writer_step["status"] == "success"
    assert writer_step["llm_response_text"] == WRITTEN


@pytest.mark.asyncio
async def test_a_writer_failure_publishes_nothing(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_writer(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("writer model unavailable")

    mock_llm_client.set_next_response(_decision())
    monkeypatch.setattr(suggestion_agent, "_generate_llm_response", failing_writer)

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.ERROR
    assert response.has_suggestion is False
    assert response.answer == ""
    writer_step = _steps(mock_repository)[-1]
    assert writer_step["status"] == "error"
    assert "writer model unavailable" in str(writer_step["error_message"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "gets_voice"), [("ja", True), ("en", False)])
async def test_only_a_language_with_a_voice_supplement_gets_it(
    suggestion_agent: SuggestionAgent,
    mock_llm_client: MockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    gets_voice: bool,
) -> None:
    voice = PromptConfig(prompt="", system_instruction="JAPANESE-VOICE-RULES")

    def load(name: str) -> PromptConfig:
        assert name == SUGGESTION_WRITER_VOICE_PROMPT_NAMES["Japanese"]
        return voice

    _SpyConfig.calls = []
    monkeypatch.setattr(mixin_mod.types, "GenerateContentConfig", _SpyConfig)
    monkeypatch.setattr(suggestion_agent, "_load_prompt_config", load)
    mock_llm_client.set_next_response(_decision())
    mock_llm_client.responses["default"] = WRITTEN
    request = _request()
    request.language = language

    response = await suggestion_agent.process(request)

    assert response.status == StatusType.SUCCESS
    system = str(_SpyConfig.calls[-1]["system_instruction"])
    assert system.startswith("Write one message in ")
    assert ("JAPANESE-VOICE-RULES" in system) is gets_voice
