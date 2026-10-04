"""SuggestionAgent LLM応答処理テスト"""

import json

import pytest
from pydantic import ValidationError
from tests.unit.agents.suggestion_agent.prompt_support import serve_lens_runs

from pantaray_agents.agents.core.mixins import llm_generation_mixin as mixin_mod
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.output import parse_suggestion_output
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.agent.suggestion import (
    SuggestionStructuredOutput,
)
from pantaray_agents.utils.prompt_loader import PromptConfig


def _no_suggestion_output() -> dict[str, object]:
    return {
        "has_suggestion": False,
        "interaction_contract": None,
        "key_point": "",
        "deliverable": None,
        "agent_session": None,
        "suggestion_summary": None,
        "target_context": None,
    }


def _suggestion_output(point: str) -> dict[str, object]:
    return {
        "has_suggestion": True,
        "interaction_contract": "action_offer",
        "key_point": point,
        "deliverable": "A schedule with the mornings kept free.",
        "agent_session": False,
        "suggestion_summary": "Action handoff summary",
        "target_context": {
            "organization_name": "Wakakusa",
            "project_name": "Pantaray",
        },
    }


class _SpyGenerateContentConfig:  # noqa: D101
    last_kwargs: dict[str, object] | None = None

    def __init__(self, **kwargs):  # noqa: ANN003
        type(self).last_kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_llm_generation_mixin_extract_thoughts_best_effort_returns_joined_text() -> (
    None
):
    """Geminiレスポンスのpartsからthought=Trueのtextのみを抽出し連結できること。"""

    class _Part:  # noqa: D101
        def __init__(self, *, thought: bool, text: str | None):
            self.thought = thought
            self.text = text

    class _Content:  # noqa: D101
        def __init__(self, parts):
            self.parts = parts

    class _Candidate:  # noqa: D101
        def __init__(self, content):
            self.content = content

    class _Response:  # noqa: D101
        def __init__(self, candidates):
            self.candidates = candidates

    resp = _Response(
        candidates=[
            _Candidate(
                _Content(
                    [
                        _Part(thought=False, text="ANSWER"),
                        _Part(thought=True, text="T1"),
                        _Part(thought=True, text="T2"),
                    ]
                )
            )
        ]
    )
    assert mixin_mod.LLMGenerationMixin._extract_thoughts_best_effort(resp) == "T1\nT2"


def test_llm_generation_mixin_extract_thoughts_best_effort_missing_structure_returns_none() -> (
    None
):
    """レスポンス構造が想定外でも例外を出さずNoneになること（best-effort）。"""

    class _Response:  # noqa: D101
        pass

    assert (
        mixin_mod.LLMGenerationMixin._extract_thoughts_best_effort(_Response()) is None
    )


def test_llm_generation_mixin_extract_thoughts_best_effort_ignores_whitespace() -> None:
    """thought が空白のみの場合は None になること（空文字生成を避ける）。"""

    class _Part:  # noqa: D101
        def __init__(self, *, thought: bool, text: str | None):
            self.thought = thought
            self.text = text

    class _Content:  # noqa: D101
        def __init__(self, parts):
            self.parts = parts

    class _Candidate:  # noqa: D101
        def __init__(self, content):
            self.content = content

    class _Response:  # noqa: D101
        def __init__(self, candidates):
            self.candidates = candidates

    resp = _Response(
        candidates=[_Candidate(_Content([_Part(thought=True, text="   ")]))]
    )
    assert mixin_mod.LLMGenerationMixin._extract_thoughts_best_effort(resp) is None


def test_llm_generation_mixin_extracts_proxy_thinking() -> None:
    """cloud proxy 経由の thinking フィールドも thought として扱う。"""

    class _Response:  # noqa: D101
        thinking = "proxy thought"

    assert (
        mixin_mod.LLMGenerationMixin._extract_thoughts_best_effort(_Response())
        == "proxy thought"
    )


def test_llm_generation_mixin_consume_thoughts_ignores_empty_text(
    suggestion_agent: SuggestionAgent,
) -> None:
    """空/空白のみの thought は None として扱うことを確認。"""
    mixin_mod._LLM_THOUGHTS_CONTEXT.set("   ")
    assert suggestion_agent._consume_llm_thoughts() is None
    mixin_mod._LLM_THOUGHTS_CONTEXT.set("")
    assert suggestion_agent._consume_llm_thoughts() is None


def test_parse_suggestion_output_without_suggestion(
    suggestion_agent: SuggestionAgent,
) -> None:
    payload = _no_suggestion_output()
    result = parse_suggestion_output(
        raw_text=json.dumps(payload, ensure_ascii=False),
        parsed_output=SuggestionStructuredOutput.model_validate(payload),
    )

    assert result["thinking"] is None
    assert result["answer"] == ""
    assert result["decided"] is None
    assert result["has_suggestion"] is False


def test_parse_suggestion_output_with_plain_suggestion(
    suggestion_agent: SuggestionAgent,
) -> None:
    payload = _suggestion_output(
        "I want my schedule to be structured so that I can focus in the morning."
    )
    result = parse_suggestion_output(
        raw_text=json.dumps(payload, ensure_ascii=False),
        parsed_output=SuggestionStructuredOutput.model_validate(payload),
    )

    assert result["thinking"] is None
    # The decision carries no user-facing text; the writer call adds it.
    assert result["answer"] == ""
    assert result["decided"] == {
        "interaction_contract": "action_offer",
        "key_point": (
            "I want my schedule to be structured so that I can focus in the morning."
        ),
        "deliverable": "A schedule with the mornings kept free.",
        "agent_session": False,
    }
    assert result["has_suggestion"] is True
    assert result["suggestion_summary"] == "Action handoff summary"


@pytest.mark.parametrize(
    ("contract", "deliverable"),
    [("action_offer", None), ("message_only", "A reply to the client.")],
)
def test_parse_suggestion_output_requires_a_deliverable_exactly_for_an_offer(
    suggestion_agent: SuggestionAgent, contract: str, deliverable: str | None
) -> None:
    payload = _suggestion_output("The client asked for the invoice again.")
    payload["interaction_contract"] = contract
    payload["deliverable"] = deliverable

    with pytest.raises(ValueError, match="deliverable must be given exactly"):
        parse_suggestion_output(
            raw_text=json.dumps(payload, ensure_ascii=False),
            parsed_output=SuggestionStructuredOutput.model_validate(payload),
        )


def test_parse_suggestion_output_rejects_missing_suggestion_summary(
    suggestion_agent: SuggestionAgent,
) -> None:
    payload = _suggestion_output("Try task 1?")
    payload["suggestion_summary"] = None

    with pytest.raises(ValueError, match="suggestion_summary must be non-empty"):
        parse_suggestion_output(
            raw_text=json.dumps(payload, ensure_ascii=False),
            parsed_output=SuggestionStructuredOutput.model_validate(payload),
        )


def test_parse_suggestion_output_rejects_blank_suggestion_summary(
    suggestion_agent: SuggestionAgent,
) -> None:
    payload = _suggestion_output("Try task 1?")
    payload["suggestion_summary"] = "   "

    with pytest.raises(ValueError, match="suggestion_summary must be non-empty"):
        parse_suggestion_output(
            raw_text=json.dumps(payload, ensure_ascii=False),
            parsed_output=SuggestionStructuredOutput.model_validate(payload),
        )


def test_parse_suggestion_output_rejects_null_target_context_when_suggestion_exists(
    suggestion_agent: SuggestionAgent,
) -> None:
    payload = _suggestion_output("Try task 1?")
    payload["target_context"] = None

    with pytest.raises(ValueError, match="target_context must be an object"):
        parse_suggestion_output(
            raw_text=json.dumps(payload, ensure_ascii=False),
            parsed_output=SuggestionStructuredOutput.model_validate(payload),
        )


def test_suggestion_structured_output_requires_summary_key() -> None:
    payload = _suggestion_output("Try task 1?")
    del payload["suggestion_summary"]

    with pytest.raises(ValidationError, match="Field required"):
        SuggestionStructuredOutput.model_validate(payload)


def test_suggestion_structured_output_requires_target_context_key() -> None:
    payload = _suggestion_output("Try task 1?")
    del payload["target_context"]

    with pytest.raises(ValidationError, match="Field required"):
        SuggestionStructuredOutput.model_validate(payload)


def test_suggestion_target_context_requires_inner_keys() -> None:
    payload = _suggestion_output("Try task 1?")
    payload["target_context"] = {"organization_name": None}

    with pytest.raises(ValidationError, match="Field required"):
        SuggestionStructuredOutput.model_validate(payload)


def test_suggestion_structured_output_rejects_extra_top_level_keys() -> None:
    payload = _suggestion_output("Try task 1?")
    payload["workspace_root_name"] = "pantaray"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SuggestionStructuredOutput.model_validate(payload)


def test_suggestion_structured_output_rejects_extra_target_context_keys() -> None:
    payload = _suggestion_output("Try task 1?")
    target_context = payload["target_context"]
    assert isinstance(target_context, dict)
    target_context["workspace_root_path"] = "/tmp/pantaray"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SuggestionStructuredOutput.model_validate(payload)


def test_system_instruction_renders_answer_language(
    suggestion_agent: SuggestionAgent,
) -> None:
    suggestion_agent._prompt_config = PromptConfig(  # noqa: SLF001
        prompt="prompt",
        system_instruction=(
            "`key_point` must be written in {answer_language}.\n"
            'JSON example: {"has_suggestion": true}'
        ),
    )
    suggestion_agent._current_language = "ja"  # noqa: SLF001

    system_instruction = suggestion_agent._system_instruction_for_request()  # noqa: SLF001
    assert "`key_point` must be written in Japanese." in system_instruction
    assert '{"has_suggestion": true}' in system_instruction
    assert "You must respond in Japanese" not in system_instruction


def test_parse_suggestion_output_empty_text(
    suggestion_agent: SuggestionAgent,
) -> None:
    with pytest.raises(ValueError, match="Empty structured suggestion response"):
        parse_suggestion_output(
            raw_text="   ",
            parsed_output=None,
        )


@pytest.mark.asyncio
async def test_suggestion_agent_reasoning_mode_omits_temperature_and_sets_thinking_high(
    suggestion_agent: SuggestionAgent,
    mock_llm_client: MockLLMClient,
    monkeypatch,
) -> None:  # noqa: ANN001
    """SuggestionAgent が温度を上書きせず、HIGH thinking で reasoning モードを使う。"""

    monkeypatch.setattr(
        mixin_mod.types, "GenerateContentConfig", _SpyGenerateContentConfig
    )
    serve_lens_runs(mock_llm_client, _no_suggestion_output())

    suggestion_agent._current_user_id = "user-test"  # noqa: SLF001
    suggestion_agent._current_suggestion_id = "suggestion-test"  # noqa: SLF001

    _ = await suggestion_agent._process_llm_response("prompt")

    assert _SpyGenerateContentConfig.last_kwargs is not None
    assert "temperature" not in _SpyGenerateContentConfig.last_kwargs
    assert (
        _SpyGenerateContentConfig.last_kwargs.get("inference_profile") == "suggestion"
    )
