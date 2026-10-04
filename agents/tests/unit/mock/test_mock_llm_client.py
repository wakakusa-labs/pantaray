import json

import pytest

from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_llm.contracts.tool_use import (
    LlmToolDefinition,
    LlmToolUseRequest,
)

# MockLLMClient のテストケース
# 各エージェントタイプに応じた応答形式（プレーン/JSON/一部タグ）のテスト


def test_get_response_text_action_prompt():
    """'action' を含むプロンプトに対する応答をテストする"""
    client = MockLLMClient()
    prompt = "Execute an action task based on the suggestion."
    response_text = client.get_response_text(prompt)
    # ActionAgent は <thinking>/<answer> を使わず、最終回答は <final_answer> を用いる
    assert "<final_answer>" in response_text
    assert "</final_answer>" in response_text
    assert "<thinking>" not in response_text
    assert "<answer>" not in response_text
    assert "<facts>" not in response_text


def test_get_response_text_insight_prompt():
    """'insight' を含むプロンプトに対する応答をテストする"""
    import json

    client = MockLLMClient()
    prompt = "Generate an insight task from the user interaction."
    response_text = client.get_response_text(prompt)
    payload = json.loads(response_text)
    assert payload["facts"] == "Mock facts."
    assert payload["short_term_insight_data"] == "Mock insight answer."
    assert "<thinking>" not in response_text
    assert "<answer>" not in response_text
    assert "<facts>" not in response_text


@pytest.mark.asyncio
async def test_aio_generate_content_suggestion():
    """aio.models.generate_content で 'suggestion' プロンプトをテストする"""
    client = MockLLMClient()
    prompt = "This is a suggestion task to suggest something to the user."
    # generate_content_async は MockLLMResponse を返す
    response_obj = await client.generate_content_async(contents=[prompt])
    payload = json.loads(response_obj.text)
    assert payload["has_suggestion"] is False
    assert payload["interaction_contract"] is None


@pytest.mark.asyncio
async def test_aio_generate_content_insight():
    """aio.models.generate_content で 'insight' プロンプトをテストする"""
    import json

    client = MockLLMClient()
    prompt = "Provide an insight task about the workflow."
    response = await client.aio.models.generate_content(contents=[prompt])
    payload = json.loads(response.text)
    assert payload["facts"] == "Mock facts."
    assert payload["short_term_insight_data"] == "Mock insight answer."


# MockLLMClientのresponses属性を外部から変更してテストするケース
# responses属性はpublicなので、テスト内で直接変更可能


def test_get_response_text_custom_response_suggestion():
    """responses属性をカスタマイズした場合の 'suggestion' 応答をテストする"""
    client = MockLLMClient()  # 正しい初期化
    custom_suggestion_response = {
        "thinking": "Custom thinking for suggestion.",
        "answer": "Custom answer for suggestion.",
    }
    client.responses["suggestion"] = custom_suggestion_response  # 属性を直接変更

    prompt = "Give me a suggestion task."
    response_text = client.get_response_text(prompt)
    # Suggestion では answer を <suggestion> タグで包んで返す
    assert "<suggestion>Custom answer for suggestion.</suggestion>" == response_text
    assert "Custom thinking for suggestion." not in response_text
    assert "<thinking>" not in response_text
    assert "<facts>" not in response_text


def test_get_response_text_custom_response_insight_with_facts():
    """responses属性をカスタマイズした場合の 'insight' 応答 (factsあり) をテストする"""
    import json

    client = MockLLMClient()  # 正しい初期化
    custom_insight_response = {
        "facts": "Some very custom facts.",
        "short_term_insight_data": "Profound custom answer for insight.",
    }
    client.responses["insight"] = custom_insight_response  # 属性を直接変更

    prompt = "I need an insight task now."
    response_text = client.get_response_text(prompt)
    payload = json.loads(response_text)
    assert payload["facts"] == "Some very custom facts."
    assert payload["short_term_insight_data"] == "Profound custom answer for insight."
    assert "<thinking>" not in response_text
    assert "<answer>" not in response_text


def test_get_response_text_custom_response_insight_without_facts():
    """responses属性をカスタマイズした場合の 'insight' 応答 (factsなし) をテストする"""
    import json

    client = MockLLMClient()  # 正しい初期化
    client.set_next_response(
        {"facts": "", "short_term_insight_data": "Answer without facts."}
    )

    prompt = "An insight task, but no facts please."
    response_text = client.get_response_text(prompt)
    payload = json.loads(response_text)
    assert payload["facts"] == ""
    assert payload["short_term_insight_data"] == "Answer without facts."
    assert "<facts>" not in response_text
    assert "<answer>" not in response_text
    assert "<thinking>" not in response_text


@pytest.mark.asyncio
async def test_mock_returns_a_batch_of_native_tool_calls():
    client = MockLLMClient()
    client.set_next_response(
        {
            "tool_calls": [
                {"tool_id": "read_file", "args": {"path": "a.md"}},
                {"tool_id": "read_file", "args": {"path": "b.md"}},
            ]
        }
    )

    response = await client.aio.models.generate_content(
        contents=["action task"],
        config={
            "tool_use": LlmToolUseRequest(
                tools=[
                    LlmToolDefinition(
                        name="read_file",
                        description="Read a file.",
                        parameters={"type": "object", "properties": {}},
                    )
                ],
                continuation_mode="disabled",
                max_parallel_tool_calls=2,
            )
        },
    )

    assert [call.name for call in response.tool_calls] == ["read_file", "read_file"]
    assert [call.arguments["path"] for call in response.tool_calls] == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_mock_rejects_more_tool_calls_than_allowed():
    client = MockLLMClient()
    client.set_next_response(
        {
            "tool_calls": [
                {"tool_id": "read_file", "args": {"path": "a.md"}},
                {"tool_id": "read_file", "args": {"path": "b.md"}},
            ]
        }
    )

    with pytest.raises(ValueError, match="max_parallel_tool_calls"):
        await client.aio.models.generate_content(
            contents=["action task"],
            config={
                "tool_use": LlmToolUseRequest(
                    tools=[
                        LlmToolDefinition(
                            name="read_file",
                            description="Read a file.",
                            parameters={"type": "object", "properties": {}},
                        )
                    ],
                    continuation_mode="disabled",
                )
            },
        )
