"""suggestion_agent テスト共通フィクスチャ"""

import json
import os
from unittest.mock import patch

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import (
    NO_SUGGESTION,
    prompt_configs,
)

from pantaray_agents.agents.artifact_react import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
)
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.agents.suggestion_agent.react import SUGGESTION_TOOL_IDS
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
)
from pantaray_agents.agents.suggestion_agent.writer import (
    SUGGESTION_WRITER_PROMPT_NAME,
)
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.utils.prompt_loader import PromptConfig


async def _execute_research_tool(
    call: ReactToolCall,
    _step_number: int,
) -> ReactToolResult:
    return ReactToolResult(
        tool_name=call.tool_name,
        status="success",
        output={"status": "success"},
    )


def _research_tool(tool_id: str) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=tool_id,
        description="SuggestionAgent test research tool.",
        request_schema={"type": "object"},
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"const": "success"}},
            }
        ),
        execute=_execute_research_tool,
    )


@pytest.fixture(autouse=True)
def setup_env_vars():
    """テスト全体で必要な環境変数を設定するフィクスチャ"""
    with patch.dict(
        os.environ,
        {
            "LLM_PROXY_URL": "https://llm-proxy.test",
        },
    ):
        yield


@pytest.fixture(autouse=True)
def clear_mock_data():
    """各テストの前にモックデータをクリアするフィクスチャ"""
    MockRepository.clear_data()
    yield
    MockRepository.clear_data()


@pytest.fixture
def mock_repository() -> MockSuggestionAgentRepository:
    """MockSuggestionAgentRepositoryのフィクスチャ"""
    return MockSuggestionAgentRepository()


@pytest.fixture
def mock_llm_client() -> MockLLMClient:
    """MockLLMClientのフィクスチャ"""
    client = MockLLMClient()
    client.responses["suggestion"] = json.dumps(NO_SUGGESTION, ensure_ascii=False)
    return client


@pytest.fixture
def fixed_suggestion_research_tools() -> FixedSuggestionResearchTools:
    return FixedSuggestionResearchTools(
        tuple(_research_tool(tool_id) for tool_id in SUGGESTION_TOOL_IDS)
    )


@pytest.fixture
def suggestion_agent(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
    fixed_suggestion_research_tools: FixedSuggestionResearchTools,
) -> SuggestionAgent:
    """SuggestionAgentのテストインスタンス"""
    # プロンプトに "suggestion task" を含める
    test_prompt = (
        "This is a suggestion task. Memory: {stable_memory_context}\n"
        "Capabilities: {action_agent_capabilities}\n"
        "Insight: {short_term_insight}\n"
        "Reason: {reconsideration_reason}"
    )

    # MockLLMResponseクラスを定義 (BaseAgentが期待する形式に合わせる)
    class MockLLMResponse:
        def __init__(self, text_content):
            self.text = text_content
            # BaseAgent._generate_llm_response は response.text を使用します

    async def mock_generate_content_async(*args, **kwargs):
        prompt_text = ""
        if (
            "contents" in kwargs
            and kwargs["contents"]
            and isinstance(kwargs["contents"], list)
            and kwargs["contents"]
        ):
            prompt_text = kwargs["contents"][0]

        llm_response_str = mock_llm_client.get_response_text(prompt_text)
        return MockLLMResponse(llm_response_str)

    writer_prompt = PromptConfig(
        prompt="writer: {kind} | {key_point} | {deliverable} | {agent_session}",
        system_instruction="Write one message in {answer_language}.",
    )
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(
            test_prompt, {SUGGESTION_WRITER_PROMPT_NAME: writer_prompt}
        ),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=fixed_suggestion_research_tools,
            stable_memory=SuggestionStableMemoryContext(
                prompt="No stable memory roots are available.",
                has_facts=False,
                has_insights=False,
            ),
        )
        agent.client = mock_llm_client
        # 初期化後にプロンプトが正しく設定されたかアサート
        assert agent.task_suggestion_prompt == test_prompt, (
            f"Expected prompt '{test_prompt}', but got '{agent.task_suggestion_prompt}'"
        )
        yield agent
