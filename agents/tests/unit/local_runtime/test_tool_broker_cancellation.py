from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    UnprojectedBrokerToolOutcome,
)

from .read_tool_broker_support import (
    bootstrap_read_runtime_db,
    execute_read_tool,
)

_INVOCATION_ID = "tool-request-canceled-read"
_THREAD_RELEASE_TIMEOUT_SECONDS = 10.0


@pytest.mark.asyncio
async def test_canceled_brokered_read_finalizes_its_invocation_as_canceled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The broker owns the audit row for brokered tools, cancellation included."""

    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text("alpha\n", encoding="utf-8")
    executor_started = threading.Event()
    release_executor = threading.Event()

    def blocking_read_executor(**_kwargs: object) -> UnprojectedBrokerToolOutcome:
        executor_started.set()
        release_executor.wait(timeout=_THREAD_RELEASE_TIMEOUT_SECONDS)
        raise AssertionError("the canceled read must not produce an outcome")

    monkeypatch.setattr(broker, "run_read_executor", blocking_read_executor)

    task = asyncio.ensure_future(
        execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "notes.txt"},
            tool_request_id=_INVOCATION_ID,
        )
    )
    assert await asyncio.to_thread(
        executor_started.wait,
        _THREAD_RELEASE_TIMEOUT_SECONDS,
    )
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_executor.set()

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            (_INVOCATION_ID,),
        ).fetchone()
        output_row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (_INVOCATION_ID,),
        ).fetchone()

    assert invocation_row == ("canceled",)
    assert output_row is not None
    output = json.loads(str(output_row[0]))
    assert output["error"]["error_type"] == "ToolCallCanceled"
    assert output["error"]["message"] == "canceled before completion"
