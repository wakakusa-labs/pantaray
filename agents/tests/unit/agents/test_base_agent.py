import os
import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from pantaray_agents.agents.core import CountingSink, LlmUsage
from pantaray_agents.agents.core.base import BaseAgent
from pantaray_agents.schema.agent.base import (
    AgentError,
    AgentRequest,
    AgentResponse,
    ErrorSeverity,
    ErrorType,
    JSONValue,
    StatusType,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.errors import LlmProxyExecutionError

type AgentPayload = dict[str, JSONValue]


# テスト用のダミーエージェントクラス
class DummyAgent(BaseAgent[AgentResponse]):
    LLM_INFERENCE_PROFILE_ID = "dummy"

    def __init__(self, config: AgentPayload):
        # 環境変数の依存を回避するためのパッチ
        with patch.dict(
            os.environ,
            {
                "LLM_PROXY_URL": "https://llm-proxy.test",
            },
        ):
            # BaseAgentの初期化を呼び出すように修正
            super().__init__(config)
        # クライアントのモックは別途必要になる可能性があるが、ひとまずinitを呼び出す
        # self.client はテストでは使用しないか、モックされる
        self.llm_config = config.get("llm", {})

    async def _validate_request(self, request: AgentRequest) -> AgentRequest:  # type: ignore[override]
        raise NotImplementedError

    async def _fetch_context_data(self, request: AgentRequest) -> AgentPayload:  # type: ignore[override]
        raise NotImplementedError

    def _build_prompt(self, context_data: AgentPayload) -> str:  # type: ignore[override]
        raise NotImplementedError

    async def _process_llm_response(  # type: ignore[override]
        self, prompt: str
    ) -> AgentPayload:
        raise NotImplementedError

    def _create_success_response(  # type: ignore[override]
        self,
        request: AgentRequest,
        extracted_data: AgentPayload,
    ) -> AgentResponse:
        raise NotImplementedError

    async def _save_response(self, response: AgentResponse) -> None:  # type: ignore[override]
        raise NotImplementedError

    def _get_id_attrs(self) -> dict[str, str]:  # type: ignore[override]
        raise NotImplementedError

    def get_response_class(self) -> type[AgentResponse]:  # type: ignore[override]
        return AgentResponse

    def get_error_code_prefix(self) -> str:  # type: ignore[override]
        return "DUMMY"

    async def _handle_agent_error(  # type: ignore[override]
        self,
        error: AgentError,
        response_params: dict[str, object],
    ) -> AgentResponse:
        raise NotImplementedError


@pytest.fixture
def dummy_agent() -> DummyAgent:  # pylint: disable=redefined-outer-name
    # テスト用の最小限の設定
    return DummyAgent(config={})


# --- エラーハンドリングメソッドのテスト (_create_error) ---


def test_create_error(dummy_agent: DummyAgent):  # pylint: disable=redefined-outer-name
    try:
        raise ValueError("Test error message")
    except ValueError as e:
        error_obj = dummy_agent._create_error(
            e, ErrorType.VALIDATION_ERROR, "TEST_CODE", ErrorSeverity.WARNING
        )
        assert isinstance(error_obj, AgentError)
        assert error_obj.error_type == ErrorType.VALIDATION_ERROR
        assert error_obj.error_code == "TEST_CODE"
        assert error_obj.error_message == "Test error message"
        assert error_obj.severity == ErrorSeverity.WARNING
        # AgentError には stack_trace フィールドが無く、必要な場合は error_details に含める設計。
        # _create_error は軽量なエラーオブジェクトのみを生成するため、詳細は含めない。
        assert error_obj.error_details is None
        assert not hasattr(error_obj, "stack_trace")


# --- __init__ のテスト ---


def test_init_without_llm_proxy_url(mocker):
    """LLM_PROXY_URL が無くても（アカウントログイン無効時）エージェントを作れること。"""
    mocker.patch.dict(os.environ, {}, clear=True)

    class PlainAgent(BaseAgent[AgentResponse]):
        def get_response_class(self) -> type[AgentResponse]:
            return AgentResponse

        def get_error_code_prefix(self) -> str:
            return "PLAIN"

        async def _validate_request(self, request: AgentRequest) -> AgentRequest:  # type: ignore[override]
            raise NotImplementedError

        async def _fetch_context_data(self, request: AgentRequest) -> AgentPayload:  # type: ignore[override]
            raise NotImplementedError

        def _build_prompt(self, context_data: AgentPayload) -> str:  # type: ignore[override]
            raise NotImplementedError

        async def _process_llm_response(  # type: ignore[override]
            self, prompt: str
        ) -> AgentPayload:
            raise NotImplementedError

        def _create_success_response(  # type: ignore[override]
            self, request: AgentRequest, extracted_data: AgentPayload
        ) -> AgentResponse:
            raise NotImplementedError

        async def _save_response(self, response: AgentResponse) -> None:  # type: ignore[override]
            raise NotImplementedError

        def _get_id_attrs(self) -> dict[str, str]:  # type: ignore[override]
            raise NotImplementedError

        async def _handle_agent_error(  # type: ignore[override]
            self, error: AgentError, response_params: dict[str, object]
        ) -> AgentResponse:
            raise NotImplementedError

    assert PlainAgent(config={}).client is not None


# --- process のエラーハンドリングテスト ---


# ダミーのリクエストとレスポンスクラス
class DummyRequest(AgentRequest):
    request_id: str = "req-1"


class DummyResponse(AgentResponse):
    response_id: str = "res-1"


class DummyAgentForProcessTest(BaseAgent[DummyResponse]):
    """processメソッドのテスト専用エージェント"""

    LLM_INFERENCE_PROFILE_ID = "dummy"

    def __init__(self, config: AgentPayload):
        # 環境変数の依存を回避するためのパッチ
        with patch.dict(
            os.environ,
            {
                "LLM_PROXY_URL": "https://llm-proxy.test",
            },
        ):
            super().__init__(config)

    def get_response_class(self) -> type[DummyResponse]:  # type: ignore[override]
        return DummyResponse

    def get_error_code_prefix(self) -> str:  # type: ignore[override]
        return "DUMMY"

    async def _validate_request(self, request: AgentRequest) -> AgentRequest:  # type: ignore[override]
        return request  # シンプルに返す

    async def _fetch_context_data(self, request: AgentRequest) -> AgentPayload:  # type: ignore[override]
        # Dummy implementation
        return {"data": "context"}

    def _build_prompt(
        self, context_data: AgentPayload, prompt_template: str | None = None
    ) -> str:  # type: ignore[override]
        return (
            prompt_template.format(**context_data)
            if prompt_template
            else "dummy prompt"
        )

    async def _process_llm_response(self, _prompt: str) -> AgentPayload:
        """ダミー実装: 引数は使用しない"""
        return {"result": "dummy result"}

    def _create_success_response(  # type: ignore[override]
        self,
        request: AgentRequest,
        extracted_data: AgentPayload,
    ) -> DummyResponse:
        return DummyResponse(
            response_id="success",
            created_at=datetime.now(UTC).isoformat(),
            status=StatusType.SUCCESS,
            error=None,
        )

    async def _save_response(self, response: DummyResponse) -> None:  # type: ignore[override]
        pass  # 保存はしない

    def _get_id_attrs(self) -> dict[str, str]:  # type: ignore[override]
        return {"response_id": "request_id"}  # request_id を response_id に使う

    def _load_prompt_template(self) -> str:
        return "Test Prompt: {data}"

    async def _handle_agent_error(  # type: ignore[override]
        self,
        error: AgentError,
        response_params: dict[str, object],
    ) -> DummyResponse:
        # 実際のサブクラスではDB保存などを行うが、ここではエラーレスポンスを返すだけ
        return self._create_error_response(error, response_params)


@pytest.fixture
def agent_for_process(mocker):
    return DummyAgentForProcessTest(config={"llm_client": mocker.Mock()})


@pytest.mark.asyncio
async def test_process_handles_connection_error(agent_for_process, mocker):  # pylint: disable=redefined-outer-name
    """processがConnectionErrorを処理できるかテスト"""
    mocker.patch.object(
        agent_for_process,
        "_fetch_context_data",
        side_effect=ConnectionError("Network error"),
    )
    mocker.patch.object(
        agent_for_process,
        "_handle_fetch_context_error",
        wraps=agent_for_process._handle_fetch_context_error,
    )  # 呼び出しをスパイ

    response = await agent_for_process.process(DummyRequest())

    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.REPOSITORY_ERROR
    assert response.error.error_code == "DUMMY_FETCH_CONTEXT_ERROR"
    assert "Network error" in response.error.error_message
    agent_for_process._handle_fetch_context_error.assert_called_once()


@pytest.mark.asyncio
async def test_process_propagates_retryable_context_failure(
    agent_for_process,
    mocker,
):
    locked = sqlite3.OperationalError("database is locked")
    mocker.patch.object(
        agent_for_process,
        "_fetch_context_data",
        side_effect=locked,
    )

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        await agent_for_process.process(DummyRequest())

    assert exc_info.value is locked


@pytest.mark.asyncio
async def test_process_handles_runtime_error(agent_for_process, mocker):  # pylint: disable=redefined-outer-name
    """processがRuntimeError (System Error) を処理できるかテスト"""
    mocker.patch.object(
        agent_for_process,
        "_fetch_context_data",
        side_effect=RuntimeError("System failure"),
    )
    mocker.patch.object(
        agent_for_process,
        "_handle_system_error",
        wraps=agent_for_process._handle_system_error,
    )  # 呼び出しをスパイ

    response = await agent_for_process.process(DummyRequest())

    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.INTERNAL_ERROR
    assert response.error.error_code == "DUMMY_SYSTEM_ERROR"
    assert "System failure" in response.error.error_message
    agent_for_process._handle_system_error.assert_called_once()


@pytest.mark.asyncio
async def test_process_handles_validation_error(agent_for_process, mocker):  # pylint: disable=redefined-outer-name
    """processがTypeError (Validation Error) を処理できるかテスト"""
    mocker.patch.object(
        agent_for_process,
        "_validate_request",
        side_effect=TypeError("Invalid request type"),
    )
    mocker.patch.object(
        agent_for_process,
        "_handle_validation_error",
        wraps=agent_for_process._handle_validation_error,
    )

    response = await agent_for_process.process(DummyRequest())

    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.VALIDATION_ERROR
    assert (
        response.error.error_code == "DUMMY_DATA_ERROR"
    )  # Validation error uses DATA_ERROR code
    assert "Invalid request type" in response.error.error_message
    agent_for_process._handle_validation_error.assert_called_once()


@pytest.mark.asyncio
async def test_process_handles_general_error(agent_for_process, mocker):  # pylint: disable=redefined-outer-name
    """processが一般的なExceptionを処理できるかテスト"""
    mocker.patch.object(
        agent_for_process,
        "_fetch_context_data",
        side_effect=Exception("Something unexpected happened"),
    )
    mocker.patch.object(
        agent_for_process,
        "_handle_general_error",
        wraps=agent_for_process._handle_general_error,
    )

    response = await agent_for_process.process(DummyRequest())

    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.INTERNAL_ERROR
    assert (
        response.error.error_code == "DUMMY_PROCESSING_ERROR"
    )  # General error uses PROCESSING_ERROR code
    assert "Something unexpected happened" in response.error.error_message
    agent_for_process._handle_general_error.assert_called_once()


# --- _generate_llm_response のテスト ---


@pytest.mark.asyncio
async def test_generate_llm_response_success(agent_for_process, mocker):  # pylint: disable=redefined-outer-name
    """_generate_llm_response が正常に動作するかテスト"""
    # generate_content を AsyncMock にし、その return_value の text 属性を設定
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
    )
    mock_generate_content.return_value.text = "LLM response text"

    prompt = "Test prompt"
    response_text = await agent_for_process._generate_llm_response(
        prompt=prompt,
        sink=CountingSink(),
    )

    assert response_text == "LLM response text"
    mock_generate_content.assert_called_once()
    call_kwargs = mock_generate_content.call_args.kwargs
    assert call_kwargs["contents"] == [prompt]  # 画像なしの場合
    assert "model" not in call_kwargs
    assert (
        call_kwargs["config"].inference_profile
        == agent_for_process.LLM_INFERENCE_PROFILE_ID
    )


@pytest.mark.asyncio
async def test_generate_llm_response_api_error(agent_for_process, mocker):  # pylint: disable=redefined-outer-name
    """_generate_llm_response で Gemini API エラーが発生した場合"""
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        side_effect=Exception("Gemini API failed"),
    )

    prompt = "Test prompt for error"
    with pytest.raises(
        RuntimeError, match=r"LLM upstream error: Exception\('Gemini API failed'\)"
    ):
        await agent_for_process._generate_llm_response(
            prompt=prompt,
            sink=CountingSink(),
        )

    mock_generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_generate_llm_response_retries_retryable_proxy_error(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    """retryable な proxy error は一時失敗として最大5回まで再試行する。"""

    retryable_error = LlmProxyExecutionError(
        error_code="PROXY_WALLET_CHECK_FAILED",
        error_message="Wallet check failed before inference started.",
        retryable=True,
        local_job_id="req-wallet-retry",
        profile_id="dummy",
        usage_metadata={
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        },
    )
    response = MagicMock()
    response.text = "LLM response text"
    response.usage_metadata = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=[
            retryable_error,
            retryable_error,
            retryable_error,
            retryable_error,
            response,
        ],
    )
    sleep_mock = mocker.patch.object(
        agent_for_process,
        "_sleep_llm_retry_backoff",
        new_callable=mocker.AsyncMock,
    )

    sink = CountingSink()
    response_text = await agent_for_process._generate_llm_response(
        prompt="retry me",
        sink=sink,
    )

    assert response_text == "LLM response text"
    assert mock_generate_content.await_count == 5
    assert sleep_mock.await_count == 4
    assert sink.delta == LlmUsage(
        prompt_tokens=18,
        completion_tokens=6,
        fields={
            "prompt_tokens": 18,
            "completion_tokens": 6,
            "total_tokens": 24,
        },
    )


@pytest.mark.asyncio
async def test_generate_llm_structured_accumulates_retry_usage(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    retryable_error = LlmProxyExecutionError(
        error_code="PROXY_UPSTREAM_UNAVAILABLE",
        error_message="provider temporarily unavailable",
        retryable=True,
        usage_metadata={"prompt_tokens": 3, "completion_tokens": 1},
    )
    response = MagicMock()
    response.text = '{"result":"ok"}'
    response.parsed = {"result": "ok"}
    response.usage_metadata = {"prompt_tokens": 5, "completion_tokens": 2}
    mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=[retryable_error, response],
    )
    mocker.patch.object(
        agent_for_process,
        "_sleep_llm_retry_backoff",
        new_callable=mocker.AsyncMock,
    )

    sink = CountingSink()
    text, parsed = await agent_for_process._generate_llm_structured(
        prompt="retry structured",
        sink=sink,
        response_schema={"type": "object"},
    )

    assert text == '{"result":"ok"}'
    assert parsed == {"result": "ok"}
    assert sink.delta == LlmUsage(
        prompt_tokens=8,
        completion_tokens=3,
        fields={"prompt_tokens": 8, "completion_tokens": 3},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "PROXY_INSUFFICIENT_BALANCE",
        "PROXY_CONNECTION_NOT_CONFIGURED",
        "PROXY_CONTINUATION_PROVIDER_MISMATCH",
        "PROXY_MODEL_CAPABILITY_UNSUPPORTED",
    ],
)
async def test_generate_llm_response_propagates_non_retryable_proxy_error(
    agent_for_process, mocker, error_code
):  # pylint: disable=redefined-outer-name
    """設定・入力の変更が必要な失敗は外部要求を繰り返さず伝える。"""

    non_retryable_error = LlmProxyExecutionError(
        error_code=error_code,
        error_message="Wallet balance is insufficient; inference was not started.",
        retryable=False,
        local_job_id="req-wallet-stop",
        profile_id="dummy",
    )
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=non_retryable_error,
    )

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await agent_for_process._generate_llm_response(
            prompt="stop",
            sink=CountingSink(),
        )

    assert exc_info.value is non_retryable_error
    assert mock_generate_content.await_count == 1


@pytest.mark.asyncio
async def test_generate_llm_tool_call_does_not_retry_contract_violation(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    contract_error = LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="The model provider returned 2 tool calls; exactly one is required.",
        retryable=False,
        recovery="repair_next_turn",
        usage_metadata={
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
        },
        tool_call_violation_reason="multiple_calls",
        actual_tool_call_count=2,
    )
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=contract_error,
    )
    sink = CountingSink()
    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await agent_for_process._generate_llm_tool_call(
            sink=sink,
            prompt="select one tool",
            tools=(
                LlmToolDefinition(
                    name="completed",
                    description="Finish.",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
        )

    assert exc_info.value is contract_error
    assert mock_generate_content.await_count == 1
    assert sink.delta == LlmUsage(
        prompt_tokens=12,
        completion_tokens=3,
        fields={
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
        },
    )


@pytest.mark.asyncio
async def test_generate_llm_tool_call_retries_transient_proxy_failure(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    transient_error = LlmProxyExecutionError(
        error_code="PROXY_UPSTREAM_UNAVAILABLE",
        error_message="provider temporarily unavailable",
        retryable=True,
        usage_metadata={
            "prompt_tokens": 7,
            "completion_tokens": 2,
            "total_tokens": 9,
        },
    )
    response = MagicMock()
    response.tool_calls = (
        LlmToolCall(
            call_id="call-1",
            name="completed",
            arguments={},
        ),
    )
    response.dropped_tool_call_names = ()
    response.tool_continuation = None
    response.usage_metadata = {
        "prompt_tokens": 11,
        "completion_tokens": 3,
        "total_tokens": 14,
    }
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=[transient_error, response],
    )
    sleep_mock = mocker.patch.object(
        agent_for_process,
        "_sleep_llm_retry_backoff",
        new_callable=mocker.AsyncMock,
    )
    sink = CountingSink()
    turn = await agent_for_process._generate_llm_tool_call(
        sink=sink,
        prompt="select one tool",
        tools=(
            LlmToolDefinition(
                name="completed",
                description="Finish.",
                parameters={"type": "object", "properties": {}},
            ),
        ),
    )

    assert turn.call.name == "completed"
    assert mock_generate_content.await_count == 2
    assert sleep_mock.await_count == 1
    assert sink.delta == LlmUsage(
        prompt_tokens=18,
        completion_tokens=5,
        fields={
            "prompt_tokens": 18,
            "completion_tokens": 5,
            "total_tokens": 23,
        },
    )


@pytest.mark.asyncio
async def test_generate_llm_tool_call_stops_after_five_transport_attempts(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    transient_error = LlmProxyExecutionError(
        error_code="PROXY_UPSTREAM_UNAVAILABLE",
        error_message="provider temporarily unavailable",
        retryable=True,
        usage_metadata={"prompt_tokens": 2, "completion_tokens": 1},
    )
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=transient_error,
    )
    sleep_mock = mocker.patch.object(
        agent_for_process,
        "_sleep_llm_retry_backoff",
        new_callable=mocker.AsyncMock,
    )
    sink = CountingSink()
    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await agent_for_process._generate_llm_tool_call(
            sink=sink,
            prompt="select one tool",
            tools=(
                LlmToolDefinition(
                    name="completed",
                    description="Finish.",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
        )

    assert exc_info.value is transient_error
    assert mock_generate_content.await_count == 5
    assert sleep_mock.await_count == 4
    assert sink.delta == LlmUsage(
        prompt_tokens=10,
        completion_tokens=5,
        fields={"prompt_tokens": 10, "completion_tokens": 5},
    )


@pytest.mark.asyncio
async def test_generate_llm_tool_call_returns_the_whole_batch(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    response = MagicMock()
    response.tool_calls = (
        LlmToolCall(call_id="call-1", name="completed", arguments={}),
        LlmToolCall(call_id="call-2", name="completed", arguments={}),
    )
    response.dropped_tool_call_names = ("completed",)
    response.tool_continuation = None
    response.usage_metadata = None
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        return_value=response,
    )
    turn = await agent_for_process._generate_llm_tool_call(
        sink=CountingSink(),
        prompt="select tools",
        tools=(
            LlmToolDefinition(
                name="completed",
                description="Finish.",
                parameters={"type": "object", "properties": {}},
            ),
        ),
        max_parallel_tool_calls=2,
    )

    assert [call.call_id for call in turn.calls] == ["call-1", "call-2"]
    assert turn.call is turn.calls[0]
    assert turn.dropped_call_names == ("completed",)
    request = mock_generate_content.await_args.kwargs["config"].tool_use
    assert request.max_parallel_tool_calls == 2


@pytest.mark.asyncio
async def test_generate_llm_response_does_not_retry_non_retryable_internal_word(
    agent_for_process, mocker
):  # pylint: disable=redefined-outer-name
    """400 系の本文に INTERNAL が含まれても retryable と誤判定しない。"""

    error = RuntimeError("400 INVALID_ARGUMENT. INTERNAL note in provider payload")
    mock_generate_content = mocker.patch.object(
        agent_for_process.client.aio.models,
        "generate_content",
        new_callable=mocker.AsyncMock,
        side_effect=error,
    )

    with pytest.raises(RuntimeError, match="LLM upstream error"):
        await agent_for_process._generate_llm_response(
            prompt="stop",
            sink=CountingSink(),
        )

    assert mock_generate_content.await_count == 1


@pytest.mark.asyncio
async def test_handle_system_error_maps_proxy_insufficient_balance(
    agent_for_process,
):  # pylint: disable=redefined-outer-name
    """proxy の残高不足は domain code に戻す。"""

    exc = LlmProxyExecutionError(
        error_code="PROXY_INSUFFICIENT_BALANCE",
        error_message=(
            "Wallet balance is insufficient; inference was not started. "
            "See https://signed.example.invalid/wallet?token=secret"
        ),
        retryable=False,
        local_job_id="req-wallet-balance",
        profile_id="dummy",
        upstream_provider="openai",
    )

    response = await agent_for_process._handle_system_error(
        exc,
        DummyRequest(),
        agent_for_process._get_id_attrs(),
    )

    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.VALIDATION_ERROR
    assert response.error.error_code == "DUMMY_INSUFFICIENT_BALANCE"
    assert response.error.error_message == (
        "Wallet balance is insufficient; inference was not started. See <redacted_url>"
    )
    assert response.error.error_details == {
        "surface_id": "llm",
        "retryable": False,
        "recovery": "stop",
        "suggested_action": "abort",
        "local_job_id": "req-wallet-balance",
        "upstream_provider": "openai",
        "profile_id": "dummy",
        "proxy_error_code": "PROXY_INSUFFICIENT_BALANCE",
    }


@pytest.mark.asyncio
async def test_handle_system_error_maps_proxy_wallet_check_failed(
    agent_for_process,
):  # pylint: disable=redefined-outer-name
    """proxy の wallet check 失敗は専用の domain code に戻す。"""

    exc = LlmProxyExecutionError(
        error_code="PROXY_WALLET_CHECK_FAILED",
        error_message="Wallet check failed before inference started.",
        retryable=True,
        local_job_id="req-wallet-check",
        profile_id="dummy",
    )

    response = await agent_for_process._handle_system_error(
        exc,
        DummyRequest(),
        agent_for_process._get_id_attrs(),
    )

    assert response.status == StatusType.ERROR
    assert response.error is not None
    assert response.error.error_type == ErrorType.INTERNAL_ERROR
    assert response.error.error_code == "DUMMY_WALLET_CHECK_FAILED"
    assert response.error.error_details == {
        "surface_id": "llm",
        "retryable": True,
        "recovery": "retry_same_request",
        "suggested_action": "retry_later",
        "local_job_id": "req-wallet-check",
        "profile_id": "dummy",
        "proxy_error_code": "PROXY_WALLET_CHECK_FAILED",
    }
