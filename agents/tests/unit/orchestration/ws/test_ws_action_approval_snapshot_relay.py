from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pantaray_agents.local_runtime.runtime.action_approval_projection import (
    ACTION_APPROVAL_SNAPSHOT_EVENT,
)
from pantaray_agents.local_runtime.suggestion_state.event_names import (
    EVENT_ACTION_RESUME_REQUESTED,
)
from pantaray_agents.orchestration.ws import (
    action_relay_forwarder_local as local_forwarder,
)
from pantaray_agents.repositories.suggestion_runtime_results import (
    AppendProcessEventResult,
)
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.repositories.repository import RepositoryResult

ROOT_PROCESS_ID = "proc-1"
PAUSED_AT = "2026-09-02T00:00:00Z"


def _blocker(process_id: str, name: str) -> dict[str, object]:
    return {
        "process_id": process_id,
        "action_id": "act-1",
        "approval_session_id": f"session-{name}",
        "tool_request_id": f"request-{name}",
        "tool_id": "apply_patch",
        "intent_class": "write",
        "command_summary": {"summary": name},
    }


ROOT_BLOCKER = _blocker(ROOT_PROCESS_ID, "root")
CHILD_BLOCKER = _blocker("child-1", "child")


@dataclass
class _FakeEvent:
    cursor: int
    event_id: str
    event_type: str
    payload: dict[str, object]


class _FakeHandler:
    def __init__(self) -> None:
        self.session_id = "sess-1"
        self.user_id = "user-1"
        self._is_closed = False
        self._action_processes = {ROOT_PROCESS_ID}
        self._process_metadata: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.synced: list[str] = []

    async def _send(self, event: str, message: Any, **kwargs: Any) -> bool:
        self.sent.append((event, message.model_dump(mode="json"), dict(kwargs)))
        return True

    def _release_action_process(self, process_id: str) -> None:
        self._action_processes.discard(process_id)
        self._process_metadata.pop(process_id, None)

    def _sync_process_metadata_to_current_session(
        self, process_id: str, **_kwargs: Any
    ) -> None:
        self.synced.append(process_id)


def _anchor_payload(blockers: list[dict[str, object]]) -> dict[str, object]:
    return {
        "action_id": "act-1",
        "user_id": "user-1",
        "status": "processing",
        "reason": "approval_pending",
        "completed_at": PAUSED_AT,
        "approval_blockers": blockers,
    }


def _snapshot(event_id: str, blockers: list[dict[str, object]]) -> _FakeEvent:
    return _FakeEvent(
        cursor=len(event_id),
        event_id=event_id,
        event_type=ACTION_APPROVAL_SNAPSHOT_EVENT,
        payload=_anchor_payload(blockers),
    )


async def _forward(
    monkeypatch: pytest.MonkeyPatch,
    handler: _FakeHandler,
    batches: list[list[_FakeEvent]],
    *,
    suggestion_id: str | None = None,
    holding_anchor_cursor: int | None = None,
) -> None:
    remaining = iter(batches)

    def read(**_kwargs: Any) -> list[_FakeEvent]:
        batch = next(remaining, None)
        if batch is None:
            # Stop the poll loop instead of letting a regression hang the suite.
            handler._is_closed = True
            return []
        return batch

    monkeypatch.setattr(
        local_forwarder,
        "read_local_runtime_db_config",
        lambda: ("/tmp/local-runtime.db", 1000),
    )
    monkeypatch.setattr(local_forwarder, "read_local_process_events_after", read)
    monkeypatch.setattr(
        local_forwarder,
        "_pause_anchor_still_holds",
        lambda **kwargs: kwargs["cursor"] == holding_anchor_cursor,
    )
    await local_forwarder._forward_action_events_from_local_runtime(
        handler,
        process_id=ROOT_PROCESS_ID,
        logical_run_id="run-root",
        suggestion_id=suggestion_id,
        action_id="act-1",
        command_id="cmd-1",
    )


def _published(handler: _FakeHandler) -> list[tuple[str, list[Any]]]:
    return [
        (kwargs["event_id"], data["approval_blockers"])
        for _event, data, kwargs in handler.sent
    ]


@pytest.mark.asyncio
async def test_snapshot_relay_publishes_every_aggregate_and_keeps_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _FakeHandler()

    await _forward(
        monkeypatch,
        handler,
        [
            [_snapshot("snap-1", [ROOT_BLOCKER, CHILD_BLOCKER])],
            [_snapshot("snap-2", [CHILD_BLOCKER])],
            [_snapshot("snap-3", [])],
        ],
    )

    assert {event for event, _data, _kwargs in handler.sent} == {
        OutboundEvent.PROCESS_PAUSED.value
    }
    assert _published(handler) == [
        ("snap-1", [ROOT_BLOCKER, CHILD_BLOCKER]),
        ("snap-2", [CHILD_BLOCKER]),
        ("snap-3", []),
    ]
    assert handler.sent[0][1]["process_id"] == ROOT_PROCESS_ID
    assert handler.sent[0][2]["persisted_sequence_override"] is None
    assert handler.sent[0][2]["persist_public_event"] is False
    assert handler.sent[0][2]["store_in_session_store"] is False
    assert ROOT_PROCESS_ID in handler._action_processes
    assert handler.synced == []


@pytest.mark.asyncio
async def test_root_physical_pause_releases_the_forwarder_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _FakeHandler()
    root_anchor = {
        name: value for name, value in ROOT_BLOCKER.items() if name != "process_id"
    }
    pause = _FakeEvent(
        2, "event-paused", "process_paused", _anchor_payload([root_anchor])
    )
    terminal = _FakeEvent(
        3,
        "event-terminal",
        "stream_end",
        {"status": "canceled", "completed_at": PAUSED_AT},
    )

    await _forward(
        monkeypatch,
        handler,
        [[_snapshot("snap-1", [ROOT_BLOCKER]), pause, terminal]],
        holding_anchor_cursor=pause.cursor,
    )

    # The pause ends this forwarder, so a cancel that terminalizes the paused
    # Action converges through the public event store instead of this stream.
    assert _published(handler) == [("snap-1", [ROOT_BLOCKER])]
    assert handler.synced == [ROOT_PROCESS_ID]
    assert ROOT_PROCESS_ID not in handler._action_processes
    assert handler._process_metadata[ROOT_PROCESS_ID]["action_id"] == "act-1"


@pytest.mark.asyncio
async def test_reattached_forwarder_skips_a_superseded_pause_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The root resumed and paused again on a second gated Tool before the client
    # reconnected. Its public cursor is still at the first generation's snapshot,
    # so the forwarder must replay past the old anchor and stop on the new one.
    handler = _FakeHandler()
    root_anchor = {
        name: value for name, value in ROOT_BLOCKER.items() if name != "process_id"
    }
    superseded = _FakeEvent(
        1, "event-paused-1", "process_paused", _anchor_payload([root_anchor])
    )
    resume = _FakeEvent(
        2, "resume-1", EVENT_ACTION_RESUME_REQUESTED, {"process_id": ROOT_PROCESS_ID}
    )
    current = _FakeEvent(
        5, "event-paused-2", "process_paused", _anchor_payload([root_anchor])
    )

    await _forward(
        monkeypatch,
        handler,
        [
            [
                superseded,
                resume,
                _snapshot("snap-2", []),
                _snapshot("snap-3", [ROOT_BLOCKER]),
                current,
            ]
        ],
        holding_anchor_cursor=current.cursor,
    )

    assert _published(handler) == [("snap-2", []), ("snap-3", [ROOT_BLOCKER])]
    assert handler.synced == [ROOT_PROCESS_ID]
    assert ROOT_PROCESS_ID not in handler._action_processes


@pytest.mark.asyncio
async def test_reattached_forwarder_skips_a_pause_anchor_the_resume_already_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The client's public cursor stops at the snapshot that precedes the pause
    # anchor, so the forwarder started by an approved resume reads that anchor
    # first. The root is queued again by then and must keep streaming.
    handler = _FakeHandler()
    root_anchor = {
        name: value for name, value in ROOT_BLOCKER.items() if name != "process_id"
    }
    stale_pause = _FakeEvent(
        1, "event-paused", "process_paused", _anchor_payload([root_anchor])
    )
    resume = _FakeEvent(
        2, "resume-1", EVENT_ACTION_RESUME_REQUESTED, {"process_id": ROOT_PROCESS_ID}
    )
    step = _FakeEvent(
        4,
        "event-step",
        "action_step",
        {
            "action_id": "act-1",
            "process_id": ROOT_PROCESS_ID,
            "step_kind": "tool",
            "step_id": "step-1",
            "step_number": 2,
            "tool_id": "shell",
            "label": "shell",
            "status": "processing",
            "started_at": PAUSED_AT,
            "completed_at": None,
        },
    )

    await _forward(
        monkeypatch,
        handler,
        [[stale_pause, resume, _snapshot("snap-2", []), step]],
    )

    assert [event for event, _data, _kwargs in handler.sent] == [
        OutboundEvent.PROCESS_PAUSED.value,
        OutboundEvent.ACTION_STEP.value,
    ]
    assert handler.sent[0][2]["event_id"] == "snap-2"
    assert handler.sent[0][1]["approval_blockers"] == []
    assert handler.sent[1][2]["event_id"] == "event-step"
    assert handler.synced == []
    assert ROOT_PROCESS_ID in handler._action_processes


@pytest.mark.asyncio
async def test_snapshot_publishes_after_the_durable_resume_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _FakeHandler()
    append = AsyncMock(
        return_value=RepositoryResult(
            data=AppendProcessEventResult(sequence=7, inserted=True)
        )
    )
    monkeypatch.setattr(
        local_forwarder.LocalSuggestionStateRepository,
        "append_action_pause_if_processing",
        append,
    )
    resume = _FakeEvent(
        1,
        "resume-1",
        EVENT_ACTION_RESUME_REQUESTED,
        {"process_id": ROOT_PROCESS_ID},
    )

    await _forward(
        monkeypatch,
        handler,
        [[resume, _snapshot("snap-1", [CHILD_BLOCKER])]],
        suggestion_id="sug-1",
    )

    assert _published(handler) == [("snap-1", [CHILD_BLOCKER])]
    assert handler.sent[0][2]["persisted_sequence_override"] == 7
    assert append.await_count == 1
    assert append.await_args.kwargs["event_id"] == "snap-1"
    assert append.await_args.kwargs["payload"]["data"]["approval_blockers"] == [
        CHILD_BLOCKER
    ]


@pytest.mark.asyncio
async def test_snapshot_for_a_terminal_action_is_skipped_without_ending_the_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _FakeHandler()
    append = AsyncMock(return_value=RepositoryResult())
    monkeypatch.setattr(
        local_forwarder.LocalSuggestionStateRepository,
        "append_action_pause_if_processing",
        append,
    )

    await _forward(
        monkeypatch,
        handler,
        [[_snapshot("snap-1", [])], [_snapshot("snap-2", [])]],
        suggestion_id="sug-1",
    )

    assert handler.sent == []
    assert append.await_count == 2
    assert ROOT_PROCESS_ID in handler._action_processes
