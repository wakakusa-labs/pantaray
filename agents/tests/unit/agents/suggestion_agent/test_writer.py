"""The writer call turns the decided content into the user-facing text and its TODO."""

import json

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import (
    serve_lens_runs,
    writer_call,
)

from pantaray_agents import todo_ux_feature
from pantaray_agents.agents.core.mixins import llm_generation_mixin as mixin_mod
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.writer import parse_written_suggestion
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentRequest,
    SuggestionTodo,
)

POINT = (
    "The client asked twice for the March invoice; it is still unsent. "
    "Pantaray can write a reply with the invoice attached."
)
SUMMARY = "ACTION-ONLY summary: procedure, conditions and ambiguity."
INSIGHT = "RUN-ONLY insight: the user reviewed the billing sheet."
WRITTEN = "3月の請求書、先方から2回催促が来ています。添付して返信文を書きましょうか？"
TODO_LINE = "3月の請求書を添付して返信する"
WRITER_CALL = writer_call(
    WRITTEN, todo=TODO_LINE, todo_state="continue", todo_note="返信文を書く前です"
)


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
    serve_lens_runs(mock_llm_client, _decision(), WRITER_CALL)

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.SUCCESS
    writer_prompt = mock_llm_client.last_prompt
    assert writer_prompt is not None
    assert POINT in writer_prompt
    for leaked in (SUMMARY, INSIGHT, "suggestion task"):
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
    serve_lens_runs(mock_llm_client, _decision(), WRITER_CALL)

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.SUCCESS
    assert response.has_suggestion is True
    assert response.answer == WRITTEN
    assert response.todo == SuggestionTodo(
        text=TODO_LINE, outcome="continue", note="返信文を書く前です"
    )
    assert response.suggestion_summary == SUMMARY
    writer_step = _steps(mock_repository)[-1]
    assert writer_step["step_kind"] == "llm"
    assert writer_step["status"] == "success"
    assert json.loads(str(writer_step["llm_response_text"]))["message"] == WRITTEN


@pytest.mark.asyncio
async def test_advice_opens_its_todo_done_without_asking_the_writer(
    suggestion_agent: SuggestionAgent,
    mock_llm_client: MockLLMClient,
) -> None:
    advice = {**_decision(), "interaction_contract": "message_only"}
    serve_lens_runs(
        mock_llm_client, advice, writer_call(WRITTEN, "message_only", todo=TODO_LINE)
    )
    offered: list[set[str]] = []
    generate = suggestion_agent._generate_llm_tool_call

    async def spy(**kwargs: object):
        tools = kwargs["tools"]
        if tools[0].name == "write_suggestion":
            offered.append(set(tools[0].parameters["properties"]))
        return await generate(**kwargs)

    suggestion_agent._generate_llm_tool_call = spy
    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.SUCCESS
    assert response.todo == SuggestionTodo(text=TODO_LINE, outcome="done", note=None)
    assert offered == [{"message", "todo"}]


@pytest.mark.asyncio
async def test_off_the_writer_writes_the_message_alone_and_no_todo(
    suggestion_agent: SuggestionAgent,
    mock_llm_client: MockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(todo_ux_feature, "TODO_UX_ENABLED", False)
    serve_lens_runs(
        mock_llm_client,
        _decision(),
        {"tool_id": "write_suggestion", "args": {"message": WRITTEN}},
    )

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.SUCCESS
    assert (response.answer, response.todo) == (WRITTEN, None)


@pytest.mark.parametrize(
    ("state", "note", "expected"),
    [
        # A finished TODO shows no note, so a note the model added is dropped.
        ("done", "送りました", SuggestionTodo(text="t", outcome="done", note=None)),
        (
            "partial",
            " 下書きまで ",
            SuggestionTodo(text="t", outcome="partial", note="下書きまで"),
        ),
        ("partial", None, ValueError),
        ("continue", "  ", ValueError),
    ],
)
def test_an_offers_todo_state_needs_a_note_unless_done(
    state: str, note: str | None, expected: object
) -> None:
    arguments = {"message": "m", "todo": "t", "todo_state": state, "todo_note": note}
    if expected is ValueError:
        with pytest.raises(ValueError, match="todo_note"):
            parse_written_suggestion(arguments, "action_offer", with_todo=True)
        return
    written = parse_written_suggestion(arguments, "action_offer", with_todo=True)
    assert written.todo == expected


@pytest.mark.asyncio
async def test_a_writer_failure_publishes_nothing(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generate = suggestion_agent._generate_llm_tool_call

    async def failing_writer(**kwargs: object):
        if kwargs["stage"] == "suggestion_writer":
            raise RuntimeError("writer model unavailable")
        return await generate(**kwargs)

    serve_lens_runs(mock_llm_client, _decision())
    monkeypatch.setattr(suggestion_agent, "_generate_llm_tool_call", failing_writer)

    response = await suggestion_agent.process(_request())

    assert response.status == StatusType.ERROR
    assert response.has_suggestion is False
    assert response.answer == ""
    writer_step = _steps(mock_repository)[-1]
    assert writer_step["status"] == "error"
    assert "writer model unavailable" in str(writer_step["error_message"])
