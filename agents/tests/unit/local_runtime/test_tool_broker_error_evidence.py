from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerExecutionError,
)

from .read_tool_broker_support import bootstrap_read_runtime_db, execute_read_tool


@pytest.mark.asyncio
async def test_broker_persists_wrapped_io_cause_and_cleanup_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text("notes\n")

    def fail_read(**_kwargs: object) -> None:
        try:
            raise PermissionError(13, "read permission denied", "notes.txt")
        except PermissionError as cause:
            error = BrokerExecutionError("workspace read failed")
            error.add_note("Closing the file descriptor also failed: bad descriptor")
            raise error from cause

    monkeypatch.setattr(broker, "run_read_executor", fail_read)
    with pytest.raises(BrokerExecutionError) as caught:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "notes.txt"},
            tool_request_id="read-failure-evidence",
        )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id=?",
            ("read-failure-evidence",),
        ).fetchone()
    assert row is not None
    output = json.loads(row[0])
    assert caught.value.finalized_output == output
    trace = output["error"]["traceback"]
    assert "PermissionError: [Errno 13] read permission denied: 'notes.txt'" in trace
    assert "BrokerExecutionError: workspace read failed" in trace
    assert "Closing the file descriptor also failed: bad descriptor" in trace
