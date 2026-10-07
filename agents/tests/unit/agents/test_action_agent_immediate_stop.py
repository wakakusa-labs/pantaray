"""停止操作は実行中の呼び出しを待たずに run を canceled へ収束させる。

停止は DB の耐久行（job の停止フェンス / Action の canceled 終端）で表現される。
ここでは実際にその行を書き、ACTION / THINK ノードが実行中の await を打ち切って
終端行を書き切るところまでを検証する。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.test_action_agent_tool_batch_execution import (
    _PATCH_ARGS,
    PATCH_TOOL,
    SQL_CALL,
    SQL_TOOL,
    _act,
    _build_fixture,
    _persisted_tool_steps,
    _success_result,
    _think,
    _tool_history,
    _turn,
)

from pantaray_agents.agents.action_agent.runtime import graph as graph_module
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act import (
    call_execution,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.failures import (
    TOOL_INTERRUPTED_MESSAGE,
    TOOL_NOT_EXECUTED_MESSAGE,
)
from pantaray_agents.agents.action_agent.runtime.stop_watch import (
    ActionStopRequested,
    ActionStopWatch,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    attach_canceled_command_output,
    canceled_command_output,
)
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.utils.trace_context import TraceContextManager

# 停止から収束までの上限。停止ポーリング間隔 (0.25s) の数倍を超えたら回帰とみなす。
STOP_LATENCY_BUDGET_SECONDS = 1.5
_JOB_ID = "job-immediate-stop"
# 中断時の部分出力を持つ唯一のツール種別（sandbox 経由のコマンド実行）。
BASH_TOOL = "bash"


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _record_user_stop(*, db_path: Path, action_id: str) -> None:
    """Stop トランザクションが書く耐久行と同じものを書く。"""

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_actions SET status = 'canceled', updated_at = ? "
            "WHERE action_id = ?",
            (datetime.now(UTC).isoformat(), action_id),
        )


async def _stop_after(started: asyncio.Event, *, db_path: Path, action_id: str) -> None:
    await asyncio.wait_for(started.wait(), timeout=5)
    _record_user_stop(db_path=db_path, action_id=action_id)


@pytest.mark.asyncio
async def test_stop_cancels_a_long_running_tool_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """終わらない bash 相当の呼び出しでも、停止は 1 呼び出しの完了を待たない。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-single",
        allowed_tool_ids=(SQL_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn(SQL_CALL)
    )
    state = await _think(agent, runtime, state)

    started = asyncio.Event()
    canceled = asyncio.Event()

    async def _blocking_run_tool(_agent, tool_def, _args, _state, **kwargs):
        del tool_def, kwargs
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            canceled.set()
            raise
        raise AssertionError("the stopped call must not return a result")

    monkeypatch.setattr(call_execution, "run_tool", _blocking_run_tool)

    loop = asyncio.get_running_loop()
    with TraceContextManager(local_job_id=_JOB_ID):
        act_task = asyncio.ensure_future(_act(agent, runtime, state))
        await _stop_after(
            started, db_path=tmp_path / "runtime.db", action_id=request.action_id
        )
        stop_requested_at = loop.time()
        state = await asyncio.wait_for(act_task, timeout=10)

    assert loop.time() - stop_requested_at < STOP_LATENCY_BUDGET_SECONDS
    assert canceled.is_set()
    assert state["status"] == "canceled"
    assert state["next_action"] is None
    assert [entry["result_line"] for entry in _tool_history(state)] == [
        f"{SQL_TOOL}: interrupted by user, outcome unknown"
    ]
    # 発行済みの呼び出しは「結果が不明である」ことを本文で言う。次の THINK はこの
    # 本文だけを見て、同じ副作用をやり直してよいかを判断する。
    assert _tool_history(state)[0]["output"]["error"]["message"] == (
        TOOL_INTERRUPTED_MESSAGE
    )
    persisted = _persisted_tool_steps(agent, request.action_id)
    assert [step["status"] for step in persisted] == ["error"]
    assert persisted[0]["error"]["error_code"] == "ACTION_TOOL_CANCELED"
    # 停止後の再開（RC1）は次の THINK から続く: checkpoint に未実行の呼び出しを残さない
    # （``next_action`` が None の checkpoint はキー自体を落とす）。
    assert "next_action" not in persisted[0]["runtime_state_checkpoint"]


@pytest.mark.asyncio
async def test_stop_cancels_every_in_flight_call_of_a_parallel_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """並列バッチは 3 件すべてを打ち切り、3 件すべてを canceled で確定する。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-parallel",
        allowed_tool_ids=(SQL_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn(SQL_CALL, SQL_CALL, SQL_CALL)
    )
    state = await _think(agent, runtime, state)
    assert state["next_action"].batch.mode == "parallel"

    all_started = asyncio.Event()
    in_flight = 0
    canceled_calls = 0

    async def _blocking_run_tool(_agent, tool_def, _args, _state, **kwargs):
        del tool_def, kwargs
        nonlocal in_flight, canceled_calls
        in_flight += 1
        if in_flight == 3:
            all_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            canceled_calls += 1
            raise
        finally:
            in_flight -= 1
        raise AssertionError("the stopped call must not return a result")

    monkeypatch.setattr(call_execution, "run_tool", _blocking_run_tool)

    with TraceContextManager(local_job_id=_JOB_ID):
        act_task = asyncio.ensure_future(_act(agent, runtime, state))
        await _stop_after(
            all_started, db_path=tmp_path / "runtime.db", action_id=request.action_id
        )
        state = await asyncio.wait_for(act_task, timeout=10)

    assert canceled_calls == 3
    assert in_flight == 0
    assert state["status"] == "canceled"
    assert [entry["result_line"] for entry in _tool_history(state)] == [
        f"{SQL_TOOL}: interrupted by user, outcome unknown"
    ] * 3
    assert len(_persisted_tool_steps(agent, request.action_id)) == 3


@pytest.mark.asyncio
async def test_stop_starts_no_further_call_of_a_sequential_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """逐次バッチは打ち切った 1 件だけを確定し、残りは 1 件も開始しない。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-sequential",
        allowed_tool_ids=(PATCH_TOOL, SQL_TOOL),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn(SQL_CALL, (PATCH_TOOL, _PATCH_ARGS))
    )
    state = await _think(agent, runtime, state)
    assert state["next_action"].batch.mode == "sequential"

    started = asyncio.Event()
    started_tool_ids: list[str] = []

    async def _blocking_run_tool(_agent, tool_def, _args, _state, **kwargs):
        del kwargs
        started_tool_ids.append(tool_def.tool_id)
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("the stopped call must not return a result")

    monkeypatch.setattr(call_execution, "run_tool", _blocking_run_tool)

    with TraceContextManager(local_job_id=_JOB_ID):
        act_task = asyncio.ensure_future(_act(agent, runtime, state))
        await _stop_after(
            started, db_path=tmp_path / "runtime.db", action_id=request.action_id
        )
        state = await asyncio.wait_for(act_task, timeout=10)

    assert started_tool_ids == [SQL_TOOL]
    assert state["status"] == "canceled"
    # 打ち切った 1 件と、発行される前に止まった残りは別の終端になる。残りは外部への
    # 影響が無いことが確定しているので、そう言い切れる。
    assert [entry["result_line"] for entry in _tool_history(state)] == [
        f"{SQL_TOOL}: interrupted by user, outcome unknown",
        f"{PATCH_TOOL}: not executed, stopped by user",
    ]
    assert _tool_history(state)[1]["output"]["error"]["message"] == (
        TOOL_NOT_EXECUTED_MESSAGE
    )
    persisted = _persisted_tool_steps(agent, request.action_id)
    assert [step["error"]["error_code"] for step in persisted] == [
        "ACTION_TOOL_CANCELED",
        "ACTION_TOOL_NOT_EXECUTED",
    ]


@pytest.mark.asyncio
async def test_stop_confirmed_between_sequential_calls_starts_no_further_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """直前のポーリング以降に確定した停止でも、次の 1 件は開始しない。

    1 件目が停止直後に返ると 0.25s のポーリングはまだ走っておらず、観測済みフラグ
    だけを見ていると 2 件目の副作用が始まってしまう。逐次の呼び出し境界では耐久行を
    読み直す。
    """

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-boundary",
        allowed_tool_ids=(PATCH_TOOL, SQL_TOOL),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn(SQL_CALL, (PATCH_TOOL, _PATCH_ARGS))
    )
    state = await _think(agent, runtime, state)
    assert state["next_action"].batch.mode == "sequential"

    started_tool_ids: list[str] = []

    async def _stopping_run_tool(_agent, tool_def, _args, _state, **kwargs):
        started_tool_ids.append(tool_def.tool_id)
        # 1 件目の実行中に停止が確定する。停止ポーリングはまだ 1 度も走っていない。
        _record_user_stop(db_path=tmp_path / "runtime.db", action_id=request.action_id)
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _stopping_run_tool)

    with TraceContextManager(local_job_id=_JOB_ID):
        state = await asyncio.wait_for(_act(agent, runtime, state), timeout=10)

    assert started_tool_ids == [SQL_TOOL]
    assert state["status"] == "canceled"
    assert state["next_action"] is None
    assert [entry["result_line"] for entry in _tool_history(state)][1] == (
        f"{PATCH_TOOL}: not executed, stopped by user"
    )


@pytest.mark.asyncio
async def test_stop_lets_a_running_apply_patch_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``apply_patch`` は打ち切らない。完走を待ってから run を収束させる。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-patch",
        allowed_tool_ids=(PATCH_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn((PATCH_TOOL, _PATCH_ARGS))
    )
    state = await _think(agent, runtime, state)

    started = asyncio.Event()

    async def _slow_patch_run_tool(_agent, tool_def, _args, _state, **kwargs):
        started.set()
        # 停止ポーリングが必ず 1 回走る長さ。cancel されるならここで届く。
        await asyncio.sleep(0.6)
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _slow_patch_run_tool)

    with TraceContextManager(local_job_id=_JOB_ID):
        act_task = asyncio.ensure_future(_act(agent, runtime, state))
        await _stop_after(
            started, db_path=tmp_path / "runtime.db", action_id=request.action_id
        )
        state = await asyncio.wait_for(act_task, timeout=10)

    assert [entry["result_line"] for entry in _tool_history(state)] == [
        f"{PATCH_TOOL}: ok, 0 files"
    ]
    assert state["status"] == "canceled"
    assert state["next_action"] is None
    assert [
        step["status"] for step in _persisted_tool_steps(agent, request.action_id)
    ] == ["success"]


@pytest.mark.asyncio
async def test_stop_aborts_the_think_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """THINK の LLM 応答待ちも打ち切り、THINK 行を残さずに canceled へ収束する。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-think",
        allowed_tool_ids=(SQL_TOOL,),
    )
    monkeypatch.setattr(
        graph_module,
        "adopt_pending_action_user_steps_at_parent_think",
        lambda **kwargs: kwargs["state"],
    )
    started = asyncio.Event()
    canceled = asyncio.Event()

    async def _blocking_llm_call(**_kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            canceled.set()
            raise
        raise AssertionError("the stopped THINK must not return a turn")

    agent._generate_llm_action_turn = _blocking_llm_call  # type: ignore[attr-defined]

    with TraceContextManager(local_job_id=_JOB_ID, extra={"process_id": "proc-stop"}):
        think_task = asyncio.ensure_future(runtime.think(state))
        await _stop_after(
            started, db_path=tmp_path / "runtime.db", action_id=request.action_id
        )
        state = await asyncio.wait_for(think_task, timeout=10)

    assert canceled.is_set()
    assert state["status"] == "canceled"
    assert state["next_action"] is None
    assert not [
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.LLM_OUTPUT
    ]


def test_only_apply_patch_is_exempt_from_immediate_cancellation() -> None:
    """完走を待つ例外は ``apply_patch`` だけ。

    ほかのクラスは打ち切れる: ``bash`` / ``run_python`` は sandbox のプロセス
    グループごと kill され、``web_*`` / ``memory_*`` / ``thinking`` は HTTP 呼び出しが
    中断され、``capture_screen`` は保留中の capture 要求を閉じ、``wait_subagents`` は
    待ち合わせを打ち切る。ここを広げると「実行中の呼び出しを待つ」旧挙動が戻る。
    """

    assert call_execution.UNCANCELLABLE_TOOL_IDS == frozenset({PATCH_TOOL})


@pytest.mark.asyncio
async def test_stop_keeps_the_partial_output_of_the_killed_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """kill 直前までの stdout/stderr は捨てず、モデルが読む本文に載せる。

    ``bash`` / ``run_python`` のブローカは、プロセスグループを kill する直前までに
    受け取っていた出力を、伝播させる ``CancelledError`` に載せて返す
    （``command_sandbox_client.attach_canceled_command_output``）。ここではそのブローカと
    同じ形で cancel を投げ、終端の本文が完走した呼び出しと同じキーで部分出力を持ち、
    そこまでが確定した進捗であることを言うのを確かめる。
    """

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-stop-partial",
        allowed_tool_ids=(BASH_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn((BASH_TOOL, {"command": "make build"}))
    )
    state = await _think(agent, runtime, state)

    started = asyncio.Event()

    async def _killed_command_run_tool(_agent, tool_def, _args, _state, **kwargs):
        del tool_def, kwargs
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as canceled:
            attach_canceled_command_output(
                canceled, stdout="compiled 3 of 9\n", stderr="warning: slow\n"
            )
            raise
        raise AssertionError("the stopped call must not return a result")

    monkeypatch.setattr(call_execution, "run_tool", _killed_command_run_tool)

    with TraceContextManager(local_job_id=_JOB_ID):
        act_task = asyncio.ensure_future(_act(agent, runtime, state))
        await _stop_after(
            started, db_path=tmp_path / "runtime.db", action_id=request.action_id
        )
        state = await asyncio.wait_for(act_task, timeout=10)

    entry = _tool_history(state)[0]
    assert entry["output"]["stdout"] == "compiled 3 of 9\n"
    assert entry["output"]["stderr"] == "warning: slow\n"
    assert entry["output"]["exit_code"] == -1
    assert entry["output"]["error"]["message"] == TOOL_INTERRUPTED_MESSAGE
    assert entry["result_line"] == (
        f"{BASH_TOOL}: interrupted by user, outcome unknown, 30 chars captured"
    )
    # 何が走りかけたのかが分からなければ「状態を確認してから再試行せよ」は使えない。
    assert entry["args"]["command"] == "make build"
    persisted = _persisted_tool_steps(agent, request.action_id)
    assert persisted[0]["tool_output"]["output"]["stdout"] == "compiled 3 of 9\n"


@pytest.mark.asyncio
async def test_stop_watch_hands_the_call_its_own_cancellation() -> None:
    """打ち切られた呼び出しが残した証拠は、そのまま終端を書く境界へ渡る。

    ブローカが部分出力を載せる先は、伝播していく ``CancelledError`` そのもの。Task を
    通したあともその同じ例外が届くことがこの経路の前提なので、ここだけを直接確かめる。
    """

    watch = ActionStopWatch(probe=lambda: True, poll_interval_seconds=0.01)

    async def _killed() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as canceled:
            attach_canceled_command_output(canceled, stdout="half", stderr="")
            raise

    async with watch:
        with pytest.raises(ActionStopRequested) as stopped:
            await watch.run(_killed())

    partial = canceled_command_output(stopped.value.cancellation)
    assert partial is not None
    assert partial.stdout == "half"
