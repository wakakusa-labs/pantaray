from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime import browser_relay as relay_module
from pantaray_agents.local_runtime.runtime.deliverable_sessions import (
    DesktopRequestMessage,
)
from pantaray_agents.local_runtime.runtime.run_end import end_run_resources
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.schema.websocket.server_messages import (
    BrowserSessionCloseRequestedMessage,
)

from .action_seed import insert_agent_action
from .broker_test_support import BROKER_ACTOR_PROCESS_ID, _seed_broker_actor_process
from .migrated_db import prepare_test_database
from .run_end_probe import install_run_end_probe

OWNER = BROKER_ACTOR_PROCESS_ID


# Catches: a caller cancelled (a Stop, a tool timeout) while the browser close
# is still being sent, leaving the run's processes running and unaudited because
# the close came first.
@pytest.mark.asyncio
async def test_a_cancelled_run_end_still_stops_the_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    probe = install_run_end_probe(monkeypatch)
    await probe.open_browser(db_path, OWNER)

    async def slow_close(user_id: str, message: DesktopRequestMessage) -> bool:
        if isinstance(message, BrowserSessionCloseRequestedMessage):
            await asyncio.sleep(0.3)
        return await probe._deliver(user_id, message)

    monkeypatch.setattr(relay_module, "deliver_desktop_request", slow_close)
    end = asyncio.ensure_future(
        end_run_resources(
            db_path=db_path, busy_timeout_ms=1_000, owner_process_ids=(OWNER,)
        )
    )
    await asyncio.sleep(0.1)
    end.cancel()
    with pytest.raises(asyncio.CancelledError):
        await end

    for _ in range(100):
        if probe.stopped:
            break
        await asyncio.sleep(0.05)
    assert probe.closed == [OWNER]
    assert probe.stopped == [((OWNER,), True)]


# Catches: a close whose send is cancelled or fails on the delivery side
# skipping the process stop, so a child's terminal releases its claims while
# its processes still run.
@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError, ConnectionResetError])
async def test_a_failed_close_still_stops_the_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    probe = install_run_end_probe(monkeypatch)
    await probe.open_browser(db_path, OWNER)

    async def failing_close(user_id: str, message: DesktopRequestMessage) -> bool:
        if isinstance(message, BrowserSessionCloseRequestedMessage):
            raise error
        return await probe._deliver(user_id, message)

    monkeypatch.setattr(relay_module, "deliver_desktop_request", failing_close)
    with pytest.raises(error):
        await end_run_resources(
            db_path=db_path, busy_timeout_ms=1_000, owner_process_ids=(OWNER,)
        )

    assert probe.stopped == [((OWNER,), True)]
