"""SuggestionAgent 初期化、バリデーション、基本処理テスト"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import (
    prompt_configs,
    serve_lens_runs,
)

from pantaray_agents.agents.capability_envelopes import (
    ACTION_AGENT_CAPABILITY_ENVELOPE,
)
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionFetchedContext,
    SuggestionStableMemoryContext,
)
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
)
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.schema.agent.base import (
    ErrorType,
    StatusType,
)
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentRequest,
    SuggestionAgentResponse,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.schema.repository_errors import FetchContextError

_STABLE_MEMORY = SuggestionStableMemoryContext(
    prompt="Stable memory summary",
    has_facts=True,
    has_insights=True,
)


def _prompt_context() -> SuggestionFetchedContext:
    return {
        "short_term_insight": "insight",
        "reconsideration_reason": "reason",
        "stable_memory_context": "hello",
        "action_agent_capabilities": "capabilities",
        "recent_suggestions": "suggestions",
        "recent_activity_descriptions": "activities",
        "recent_activity_summary_1h": "hourly",
        "recent_activity_summaries_24h_1w_1m": "summaries",
        "context_density_signal": "context_density: low",
        "workspace_context_prompt": "workspace",
    }


def _no_suggestion_output() -> dict[str, object]:
    return {
        "has_suggestion": False,
        "interaction_contract": None,
        "key_point": "",
        "suggestion_summary": None,
        "target_context": None,
    }


def _suggestion_output(point: str) -> dict[str, object]:
    return {
        "has_suggestion": True,
        "interaction_contract": "action_offer",
        "key_point": point,
        "deliverable": "Task 1 done.",
        "agent_session": False,
        "suggestion_summary": "Action handoff summary",
        "target_context": {
            "organization_name": "Wakakusa",
            "project_name": "Pantaray",
        },
    }


def test_suggestion_agent_requires_repository() -> None:
    with pytest.raises(ValueError):
        SuggestionAgent(
            config={},
            repository=None,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=_STABLE_MEMORY,
        )


def test_suggestion_agent_build_prompt_rejects_unknown_placeholders(
    mock_repository: MockSuggestionAgentRepository, mock_llm_client: MockLLMClient
) -> None:
    """未供給のテンプレート値は fail-fast する。"""
    test_prompt = (
        "This is a suggestion task.\n"
        "Memory: {stable_memory_context}\n"
        "Extra: {new_placeholder}\n"
    )
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=_STABLE_MEMORY,
        )
        with pytest.raises(KeyError, match="new_placeholder"):
            _ = agent._build_prompt(_prompt_context())  # noqa: SLF001


def test_suggestion_agent_build_prompt_requires_context_density_signal(
    mock_repository: MockSuggestionAgentRepository, mock_llm_client: MockLLMClient
) -> None:
    """context_density_signal 欠落時は KeyError で fail-fast すること。"""
    test_prompt = "Density:\n{context_density_signal}\n"
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=_STABLE_MEMORY,
        )
        context = dict(_prompt_context())
        del context["context_density_signal"]
        with pytest.raises(KeyError, match="context_density_signal"):
            _ = agent._build_prompt(context)  # type: ignore[arg-type]  # noqa: SLF001


@pytest.mark.parametrize("prompt_chars", [124_000, 124_001])
def test_suggestion_agent_build_prompt_character_budget(
    prompt_chars: int,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    agent = SuggestionAgent(
        config={"llm_client": mock_llm_client},
        repository=mock_repository,
        research_tools=build_mock_suggestion_research_tools(),
        stable_memory=_STABLE_MEMORY,
    )
    context = _prompt_context()
    context["workspace_context_prompt"] = ""
    template_chars = len(agent._build_prompt(context))
    context["workspace_context_prompt"] = "文" * (prompt_chars - template_chars)

    if prompt_chars == 124_000:
        rendered = agent._build_prompt(context)
        assert len(rendered) == prompt_chars
        assert context["workspace_context_prompt"] in rendered
    else:
        with pytest.raises(
            ValueError, match="124001 characters.*124000-character budget"
        ):
            agent._build_prompt(context)


@pytest.mark.asyncio
async def test_suggestion_agent_sends_large_activity_context_without_truncation(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    summaries = {
        "1h": "直近一時間の活動。" * 400,
        "24h": "一日の活動要約。" * 600,
        "1w": "一週間の活動要約。" * 250,
        "1m": "一か月の活動要約。" * 250,
    }
    description = "直近の作業を調査しています。" * 100
    mock_repository.data["activity_summaries"] = [
        {
            "user_id": "user-test",
            "summary_type": kind,
            "summary": summary,
            "period_start": "2026-09-08T19:00:00+00:00",
            "period_end": "2026-09-08T20:00:00+00:00",
            "source_ids": [f"src-{kind}"],
        }
        for kind, summary in summaries.items()
    ]
    mock_repository.data["activity_descriptions"] = [
        {
            "user_id": "user-test",
            "description": f"作業{i}: {description}",
            "created_at": "2026-09-08T20:00:00+00:00",
            "period_start": "2026-09-08T19:56:00+00:00",
            "period_end": "2026-09-08T20:00:00+00:00",
        }
        for i in range(3)
    ]
    agent = SuggestionAgent(
        config={"llm_client": mock_llm_client},
        repository=mock_repository,
        research_tools=build_mock_suggestion_research_tools(),
        stable_memory=_STABLE_MEMORY,
    )
    serve_lens_runs(mock_llm_client, _no_suggestion_output())

    response = await agent.process(
        SuggestionAgentRequest(
            suggestion_id="sug-large-context",
            user_id="user-test",
            short_term_insight="調査結果を実装へ反映する段階です。",
            reconsideration_reason="調査が完了しました。",
        )
    )

    assert response.status == StatusType.SUCCESS
    prompt = mock_llm_client.last_prompt
    assert prompt is not None
    assert 16_000 < len(prompt) < 124_000
    for summary in summaries.values():
        assert summary in prompt
    for i in range(3):
        assert f"作業{i}: {description}" in prompt


def test_action_capability_envelope_is_principled_not_a_raw_tool_list() -> None:
    envelope = ACTION_AGENT_CAPABILITY_ENVELOPE

    assert "Read and search files within authorized workspace scopes." in envelope
    assert "Research and extract information from the web." in envelope
    assert "Create or modify files and durable workspace artifacts" in envelope
    assert "tests, and verification" in envelope
    assert "coherent multi-stage work" in envelope
    assert "Tool:" not in envelope
    assert "ID:" not in envelope
    assert "thinking" not in envelope

    assert "web_search" not in envelope
    assert "apply_patch" not in envelope


@pytest.mark.asyncio
async def test_suggestion_agent_process_includes_action_capabilities_in_prompt(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
    fixed_suggestion_research_tools: FixedSuggestionResearchTools,
) -> None:
    """process 経路で capability envelope と density signal を埋め込む。"""
    test_prompt = (
        "This is a suggestion task.\n"
        "Capabilities:\n"
        "{action_agent_capabilities}\n"
        "Density:\n"
        "{context_density_signal}\n"
    )
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=fixed_suggestion_research_tools,
            stable_memory=_STABLE_MEMORY,
        )
        agent.client = mock_llm_client

        req = SuggestionAgentRequest(
            suggestion_id="sug-tools-1",
            user_id="user-test",
            short_term_insight="# Insight\nThe user is fixing a parser bug.",
            reconsideration_reason="The user switched goals.",
        )
        serve_lens_runs(mock_llm_client, _no_suggestion_output())
        _ = await agent.process(req)
        prompt_used = mock_llm_client.last_prompt or ""
        assert "Capabilities:" in prompt_used
        assert "Read and search files" in prompt_used
        assert "Create or modify files" in prompt_used
        assert "tests, and verification" in prompt_used
        assert "ID:" not in prompt_used
        assert "context_density: low" in prompt_used


@pytest.mark.asyncio
async def test_suggestion_agent_process_no_suggestion(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
):
    """提案がない場合の正常系テスト"""
    # テストデータ準備
    mock_repository.data["insights"] = [
        {
            "insight_id": "ins-for-sug-no-sug",
            "user_id": "user-test",
            "insight_data": "Some insight content for no suggestion test.",
            "created_at": datetime.now(UTC),
            "status": "success",
        }
    ]
    mock_repository.data["projects"] = []

    request = SuggestionAgentRequest(
        suggestion_id="sug-123",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    serve_lens_runs(mock_llm_client, _no_suggestion_output())

    response = await suggestion_agent.process(request)

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.SUCCESS
    assert not response.has_suggestion
    assert response.answer == ""
    assert response.thinking is None
    assert response.suggestion_id == "sug-123"


@pytest.mark.asyncio
async def test_suggestion_agent_process_with_suggestion(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
):
    """提案がある場合の正常系テスト"""
    # インサイトデータを準備
    mock_repository.data["insights"] = [
        {
            "insight_id": "ins-test",
            "user_id": "user-test",
            "insight_data": "User is active and using feature X.",
            "created_at": datetime.now(UTC),
            "status": "success",
        }
    ]
    mock_repository.data["projects"] = []

    request = SuggestionAgentRequest(
        suggestion_id="sug-456",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    serve_lens_runs(mock_llm_client, _suggestion_output("Task 1 is ready."))
    mock_llm_client.responses["default"] = "Try task 1?"  # the writer's reply

    response = await suggestion_agent.process(request)

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.SUCCESS
    assert response.has_suggestion
    assert response.answer == "Try task 1?"
    assert response.thinking is None
    assert response.suggestion_summary == "Action handoff summary"
    assert response.target_context is not None
    assert response.target_context.organization_name == "Wakakusa"
    assert response.target_context.project_name == "Pantaray"
    assert response.suggestion_id == "sug-456"


@pytest.mark.asyncio
async def test_suggestion_agent_uses_preloaded_stable_memory(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
):
    """run開始時に固定したmemory seedだけを初期プロンプトへ渡す。"""
    request = SuggestionAgentRequest(
        suggestion_id="sug-storage-1",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    # DB側のinsights取得は呼ばれないことを強制
    mock_repository.get_latest_insights = AsyncMock(
        side_effect=AssertionError("must not call get_latest_insights")
    )

    with (
        patch.object(
            suggestion_agent,
            "_build_prompt",
            wraps=suggestion_agent._build_prompt,
        ) as mock_build_prompt,
    ):
        serve_lens_runs(mock_llm_client, _no_suggestion_output())
        _ = await suggestion_agent.process(request)

        assert mock_build_prompt.call_count >= 1
        ctx = mock_build_prompt.call_args[0][0]
        assert ctx["stable_memory_context"] == "No stable memory roots are available."
        assert "insight_data" not in ctx
        assert "fact_data" not in ctx


@pytest.mark.asyncio
async def test_suggestion_agent_validation_error(
    suggestion_agent: SuggestionAgent, mock_repository: MockSuggestionAgentRepository
):
    """リクエストバリデーションエラーのテスト"""
    invalid_request = {"wrong_field": "value"}  # 不正なリクエスト

    response = await suggestion_agent.process(invalid_request)

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.VALIDATION_ERROR
    assert "SUGGESTION_DATA_ERROR" in response.error.error_code
    assert response.has_suggestion is False
    # エラー時は suggestion_id がデフォルト値 ("error_id") になる想定
    assert response.suggestion_id == "error_id"


@pytest.mark.asyncio
async def test_suggestion_agent_llm_error(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
):
    """LLM APIエラーのテスト"""
    # テストデータ準備: KeyError: 'latest_insight_content' を避けるため
    mock_repository.data["insights"] = [
        {
            "insight_id": "ins-for-sug-llm-err",
            "user_id": "user-test",  # request.user_id と一致させる
            "insight_data": "Some insight content for suggestion LLM error test.",
            "created_at": datetime.now(UTC),
            "status": "success",
        }
    ]
    mock_repository.data["projects"] = []  # insights の外に移動

    request = SuggestionAgentRequest(
        suggestion_id="sug-789",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    # LLM呼び出し時にエラーを発生させる
    mock_llm_client.set_next_error(RuntimeError("LLM down"))

    # _build_prompt をモックして呼び出し引数を確認
    with patch.object(
        suggestion_agent, "_build_prompt", wraps=suggestion_agent._build_prompt
    ):
        response = await suggestion_agent.process(request)

    # _build_prompt が呼び出されたか確認 (LLMエラーなので実際には呼び出されないはずだが、もしKeyErrorが先に発生していれば呼び出されている)
    # mock_build_prompt.assert_called_once()
    # call_args = mock_build_prompt.call_args[0][0] # 最初の位置引数 (context_data)
    # assert "latest_insight_content" in call_args
    # assert "project_names_string" in call_args
    # assert "capture_timestamps_formatted" in call_args

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.LLM_API_ERROR
    # Assert the expected code based on RuntimeError handling in BaseAgent
    assert response.error.error_code == "SUGGESTION_LLM_RESPONSE_ERROR"
    # Check if error_message is not None before using 'in'
    assert response.error.error_message is not None
    assert "LLM down" in response.error.error_message
    assert response.has_suggestion is False
    assert response.suggestion_id == "sug-789"


@pytest.mark.asyncio
async def test_suggestion_agent_repository_error_on_fetch(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
):
    """インサイト取得時のリポジトリエラーのテスト"""
    # _fetch_context_data の途中で repository 呼び出しが落ちたときのエラー経路を確認する。

    request = SuggestionAgentRequest(
        suggestion_id="sug-fetch-err",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    # 直近提案履歴取得でリポジトリエラーを発生させる
    with patch.object(
        mock_repository,
        "get_recent_suggestions",
        side_effect=ConnectionError("DB connection error during fetch"),
    ):
        response = await suggestion_agent.process(request)

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert (
        response.error.error_type == ErrorType.REPOSITORY_ERROR
    )  # 修正: INTERNAL_ERROR から REPOSITORY_ERROR へ
    assert (
        response.error.error_code == "SUGGESTION_FETCH_CONTEXT_ERROR"
    )  # CONNECTION_ERROR から FETCH_CONTEXT_ERROR へ変更
    assert "DB connection error during fetch" in response.error.error_message
    assert response.suggestion_id == "sug-fetch-err"


@pytest.mark.asyncio
async def test_suggestion_agent_repository_fetch_context_error_on_activity_summary(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
):
    request = SuggestionAgentRequest(
        suggestion_id="sug-fetch-context-err",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    with patch.object(
        mock_repository,
        "get_recent_activity_summary_1h",
        side_effect=FetchContextError(
            "Failed to fetch activity_summaries.summary (record_id=s1)"
        ),
    ):
        response = await suggestion_agent.process(request)

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.REPOSITORY_ERROR
    assert response.error.error_code == "SUGGESTION_FETCH_CONTEXT_ERROR"
    assert response.suggestion_id == "sug-fetch-context-err"


@pytest.mark.asyncio
async def test_suggestion_agent_propagates_retryable_repository_result(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
) -> None:
    request = SuggestionAgentRequest(
        suggestion_id="sug-retryable-context",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    with patch.object(
        mock_repository,
        "get_recent_activity_descriptions",
        new=AsyncMock(
            return_value=RepositoryResult(
                error="database is locked",
                retryable=True,
            )
        ),
    ):
        with pytest.raises(FetchContextError) as exc_info:
            await suggestion_agent.process(request)

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_suggestion_agent_repository_error_on_save(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
):
    """agent.process は terminal persistence を行わない。"""
    # テストデータ準備: KeyError: 'latest_insight_content' を避けるため
    mock_repository.data["insights"] = [
        {
            "insight_id": "ins-for-sug-save-err",
            "user_id": "user-test",  # request.user_id と一致させる
            "insight_data": "Some insight content for suggestion save error test.",
            "created_at": datetime.now(UTC),
            "status": "success",
        }
    ]
    mock_repository.data["projects"] = []  # insights の外に移動

    request = SuggestionAgentRequest(
        suggestion_id="sug-save-err",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )
    serve_lens_runs(mock_llm_client, _suggestion_output("Task 1 is ready."))
    mock_llm_client.responses["default"] = "Try task 1?"  # the writer's reply

    response = await suggestion_agent.process(request)

    assert isinstance(response, SuggestionAgentResponse)
    assert response.status == StatusType.SUCCESS
    assert response.suggestion_id == "sug-save-err"
    saved = await mock_repository.get_suggestion(
        user_id="user-test",
        suggestion_id="sug-save-err",
    )
    assert saved.data is None


@pytest.mark.asyncio
async def test_validate_request_other_pydantic_model(suggestion_agent: SuggestionAgent):
    """_validate_request に他のpydanticモデルを渡すテスト"""
    from unittest.mock import MagicMock

    # プロパティを持つモックオブジェクトを作成
    mock_request = MagicMock()
    mock_request.model_dump.return_value = {
        "suggestion_id": "sug-123",
        "user_id": "user-test",
        "short_term_insight": "# Insight\nThe user is fixing a parser bug.",
        "reconsideration_reason": "The user switched goals.",
    }

    result = await suggestion_agent._validate_request(mock_request)

    assert isinstance(result, SuggestionAgentRequest)
    assert result.suggestion_id == "sug-123"
    assert result.user_id == "user-test"
