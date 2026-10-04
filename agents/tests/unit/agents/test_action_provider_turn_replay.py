"""思考の項目を戻すかどうかを、走りの側で決める規則（誰のものか、まだ受け取るか）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm import (
    provider_turns,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.context_budget import (
    PreparedWindow,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.provider_turns import (
    ActionProviderTurnStore,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.send import (
    send_executing_turn,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    StateTokenSink,
)
from pantaray_agents.local_runtime.runtime.connection_store import (
    ApiKeyConnection,
    LlmConnection,
    request_llm_connection,
)
from pantaray_agents.repositories.runtime_ports import ActionRepositoryPort
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnAssistantItem,
    OpenAiProviderTurn,
)
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_UPSTREAM_RATE_LIMITED,
    LlmProxyExecutionError,
)
from pantaray_llm.profiles.llm import ACTION_EXECUTING_PROFILE_ID

_IDENTITY = "api_key:openai:gpt-5.6-sol:0123456789abcdef"
_STEP_ID = "think-step"
_TURN = OpenAiProviderTurn(
    provider="openai",
    items=[{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}],
)


def _refused_input() -> LlmProxyExecutionError:
    return LlmProxyExecutionError(
        error_code=PROXY_INVALID_INPUT,
        error_message="OpenAI rejected the input.",
        retryable=False,
        upstream_status_code=400,
        upstream_code="invalid_request_error",
    )


def _window(store: ActionProviderTurnStore) -> PreparedWindow:
    """One item, carrying this store's turn or not, as the real window would."""
    return PreparedWindow(
        prompt="HEAD",
        recorded_prompt="HEAD",
        conversation=[
            LlmTurnAssistantItem(
                type="assistant",
                text=["調べます。"],
                provider_turn=store.replayable().get(_STEP_ID),
            )
        ],
        turn_context=None,
        world_state=None,
        file_inputs=(),
        history_bytes=0,
        rendered_bytes=0,
    )


async def _send(
    store: ActionProviderTurnStore,
    *,
    responses: list[object],
    connection: LlmConnection | None = None,
) -> tuple[LlmActionTurnResponse, Any]:
    agent = SimpleNamespace(_generate_llm_action_turn=AsyncMock(side_effect=responses))
    turn = await send_executing_turn(
        cast(ActionAgent, agent),
        sink=cast(StateTokenSink, None),
        prepared=_window(store),
        prepare=lambda: _window(store),
        store=store,
        connection=connection,
        tools=(),
        max_parallel_tool_calls=1,
        system_instruction="SYS",
    )
    return turn, agent._generate_llm_action_turn


def _store() -> ActionProviderTurnStore:
    return ActionProviderTurnStore(identity=_IDENTITY, turns={_STEP_ID: _TURN})


def _replayed(mock: Any) -> list[LlmProviderTurn | None]:
    return [
        cast(LlmTurnAssistantItem, call.kwargs["conversation"][0]).provider_turn
        for call in mock.await_args_list
    ]


# --- 拒否されたときの 1 回だけの再送 -------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_input_resends_the_same_turn_without_the_thinking() -> None:
    accepted = LlmActionTurnResponse(
        mode="action_turn",
        calls=[],
        messages=[
            LlmCommentary(phase="commentary", source_message_id="m1", text="了解")
        ],
    )
    store = _store()

    turn, mock = await _send(store, responses=[_refused_input(), accepted])

    assert turn is accepted
    assert _replayed(mock) == [_TURN, None]
    # Every later turn of this run keeps sending the structure without them.
    assert store.replayable() == {}


@pytest.mark.asyncio
async def test_a_second_refusal_is_raised_as_the_fault_it_is() -> None:
    """再送しても 400 なら、思考ではない入力の誤りなので今までどおり失敗する。"""

    store = _store()

    with pytest.raises(LlmProxyExecutionError) as raised:
        await _send(store, responses=[_refused_input(), _refused_input()])

    assert raised.value.error_code == PROXY_INVALID_INPUT


@pytest.mark.asyncio
async def test_a_refusal_of_a_turn_that_carried_none_is_raised_where_it_was() -> None:
    """思考を載せていない要求の 400 は、ほかの入力の誤り。再送で隠さない。"""

    store = ActionProviderTurnStore(identity=_IDENTITY)

    with pytest.raises(LlmProxyExecutionError):
        await _send(store, responses=[_refused_input()])


@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_refused_input_is_never_resent() -> None:
    store = _store()

    rate_limited = LlmProxyExecutionError(
        error_code=PROXY_UPSTREAM_RATE_LIMITED,
        error_message="Slow down.",
        retryable=True,
    )

    with pytest.raises(LlmProxyExecutionError):
        await _send(store, responses=[rate_limited])

    # The transport owns its own retries; nothing here touched the thinking.
    assert store.replayable() == {_STEP_ID: _TURN}


# --- 誰のものかで決める --------------------------------------------------------


def test_a_turn_is_kept_only_under_the_account_that_issued_it() -> None:
    """どこにも届かない経路（未設定）では要求を送らないので記録も無い。"""

    assert (
        ActionProviderTurnStore(identity=None).accept(step_id=_STEP_ID, turn=_TURN)
        is None
    )

    store = ActionProviderTurnStore(identity=_IDENTITY)
    record = store.accept(step_id=_STEP_ID, turn=_TURN)

    assert record is not None
    assert record.identity == _IDENTITY
    assert store.replayable() == {_STEP_ID: _TURN}


def test_model_switch_drops_the_old_models_opaque_turns() -> None:
    store = _store()
    new_identity = _IDENTITY.replace("gpt-5.6-sol", "gpt-6-luna")

    store.use_identity(new_identity)

    assert store.replayable() == {}
    record = store.accept(step_id="next-step", turn=_TURN)
    assert record is not None and record.identity == new_identity


@pytest.mark.asyncio
async def test_model_switch_does_not_replay_the_old_turn_on_the_next_send() -> None:
    accepted = LlmActionTurnResponse(
        mode="action_turn",
        calls=[],
        messages=[
            LlmCommentary(phase="commentary", source_message_id="m1", text="了解")
        ],
    )
    store = _store()
    store.use_identity(_IDENTITY.replace("gpt-5.6-sol", "gpt-6-luna"))

    _, mock = await _send(store, responses=[accepted])

    assert _replayed(mock) == [None]


@pytest.mark.asyncio
async def test_replay_and_send_keep_one_connection_snapshot() -> None:
    connection = ApiKeyConnection(
        provider="openai", model="gpt-6-luna", api_key="sk-not-a-real-key"
    )
    accepted = LlmActionTurnResponse(
        mode="action_turn",
        calls=[],
        messages=[
            LlmCommentary(phase="commentary", source_message_id="m1", text="了解")
        ],
    )
    store = _store()
    seen: list[LlmConnection | None] = []

    async def respond(**_kwargs: object) -> LlmActionTurnResponse:
        seen.append(request_llm_connection())
        return accepted

    agent = SimpleNamespace(_generate_llm_action_turn=AsyncMock(side_effect=respond))

    await send_executing_turn(
        cast(ActionAgent, agent),
        sink=cast(StateTokenSink, None),
        prepared=_window(store),
        prepare=lambda: _window(store),
        store=store,
        connection=connection,
        tools=(),
        max_parallel_tool_calls=1,
        system_instruction="SYS",
    )

    assert seen == [connection]
    assert _replayed(agent._generate_llm_action_turn) == [_TURN]


def _identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    route: str,
    connection: LlmConnection | None = None,
) -> str | None:
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: route)
    monkeypatch.setattr(provider_turns, "peek_llm_connection", lambda: connection)
    identity, _ = provider_turns.read_provider_turn_target(
        inference_profile=ACTION_EXECUTING_PROFILE_ID
    )
    return identity


def test_the_cloud_route_is_named_by_the_route_and_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud では思考を出したのは運用側の口座で、鍵もモデルもここからは見えない。

    分かるのは要求した profile までで、Cloud はそれを 1 つのモデルへ解決する。
    """

    connection = ApiKeyConnection(
        provider="openai", model="gpt-6-luna", api_key="sk-not-a-real-key"
    )
    cloud = _identity(monkeypatch, route="cloud", connection=connection)
    direct = _identity(monkeypatch, route="direct", connection=connection)

    assert cloud == f"cloud:{ACTION_EXECUTING_PROFILE_ID}"
    assert cloud is not None and "sk-not-a-real-key" not in cloud
    # 経路を変えて再開した Action は、もう一方の経路で得た思考を読み戻さない。
    assert direct is not None and cloud != direct
    assert _identity(monkeypatch, route="unconfigured") is None


def test_direct_replay_identity_is_built_from_the_connection_to_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ApiKeyConnection(
        provider="openai", model="gpt-6-luna", api_key="sk-not-a-real-key"
    )
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "direct")
    monkeypatch.setattr(provider_turns, "peek_llm_connection", lambda: connection)

    identity, snapshot = provider_turns.read_provider_turn_target(
        inference_profile=ACTION_EXECUTING_PROFILE_ID
    )

    assert snapshot is connection
    assert identity is not None and ":gpt-6-luna:" in identity


@pytest.mark.asyncio
async def test_a_cloud_run_reads_back_what_the_same_route_and_profile_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "cloud")
    repository = SimpleNamespace(
        get_action_provider_turns=AsyncMock(
            return_value=RepositoryResult(data={_STEP_ID: _TURN})
        )
    )

    store = await provider_turns.load_action_provider_turns(
        cast(ActionRepositoryPort, repository),
        user_id="user-1",
        action_id="action-1",
        inference_profile=ACTION_EXECUTING_PROFILE_ID,
    )

    assert repository.get_action_provider_turns.await_args.kwargs["identity"] == (
        f"cloud:{ACTION_EXECUTING_PROFILE_ID}"
    )
    assert store.replayable() == {_STEP_ID: _TURN}
