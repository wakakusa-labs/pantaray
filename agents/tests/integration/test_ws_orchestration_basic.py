import time
from datetime import datetime

from .ws_orchestration_test_helpers import (
    _assert_has_event,
    _drain_until,
    _receive_ws_json,
    _send_ws,
    _ws_connect,
    start_relayed_suggestion,
)
from .ws_orchestration_test_helpers import ws_app_harness as ws_app_harness


def test_relayed_suggestion_stream_basic(ws_app_harness) -> None:
    from pantaray_agents.schema.events import OutboundEvent

    user_id = "user_test"
    with _ws_connect(ws_app_harness, user_id=user_id) as ws:
        first = _receive_ws_json(ws, timeout_s=2.0)
        assert first.get("event") == OutboundEvent.SESSION_STARTED.value

        start_relayed_suggestion(ws_app_harness, user_id=user_id)

        started = None
        for _ in range(10):
            msg = _receive_ws_json(ws, timeout_s=2.0)
            if msg.get("event") == OutboundEvent.PROCESS_STARTED.value:
                started = msg
                break
        assert started is not None

        msgs = _drain_until(ws, {OutboundEvent.PROCESS_COMPLETED.value}, timeout_s=5.0)
        _assert_has_event(msgs, OutboundEvent.SUGGESTION_CHUNK.value)
        _assert_has_event(msgs, OutboundEvent.PROCESS_COMPLETED.value)


def test_dismiss_suggestion_flow_basic(ws_app_harness) -> None:
    from pantaray_agents.schema.events import OutboundEvent

    user_id = "user_test"
    with _ws_connect(ws_app_harness, user_id=user_id) as ws:
        _ = _receive_ws_json(ws, timeout_s=2.0)

        start_relayed_suggestion(ws_app_harness, user_id=user_id)
        started = None
        for _ in range(20):
            msg = _receive_ws_json(ws, timeout_s=2.0)
            if msg.get("event") == OutboundEvent.PROCESS_STARTED.value:
                started = msg
                break
        assert started is not None
        suggestion_process_id = started.get("data", {}).get("process_id")
        suggestion_id = started.get("data", {}).get("suggestion_id")
        assert suggestion_process_id
        assert suggestion_id

        deadline = time.time() + 10.0
        while time.time() < deadline:
            msg = _receive_ws_json(ws, timeout_s=max(0.1, deadline - time.time()))
            if (
                msg.get("event") == OutboundEvent.PROCESS_COMPLETED.value
                and msg.get("data", {}).get("process_id") == suggestion_process_id
            ):
                break
        else:
            raise AssertionError("Timed out waiting for suggestion process_completed")

        _send_ws(ws, "dismiss_suggestion", {"suggestion_id": suggestion_id})

        msgs = _drain_until(
            ws, {OutboundEvent.SUGGESTION_REACTION_COMMITTED.value}, timeout_s=3.0
        )
        _assert_has_event(msgs, OutboundEvent.SUGGESTION_REACTION_COMMITTED.value)
        committed = next(
            msg
            for msg in msgs
            if str(msg.get("event"))
            == OutboundEvent.SUGGESTION_REACTION_COMMITTED.value
        )
        rejected_at = (committed.get("data") or {}).get("committed_at")
        assert (committed.get("data") or {}).get("reaction") == "rejected"
        assert isinstance(rejected_at, str) and rejected_at
        assert rejected_at != "1970-01-01T00:00:00Z"
        datetime.fromisoformat(rejected_at.replace("Z", "+00:00"))

        more: list[dict] = []
        try:
            more.append(_receive_ws_json(ws, timeout_s=1.0))
        except TimeoutError:
            pass
        assert all(
            message.get("event") != "insight_processing_started"
            for message in msgs + more
        )


def test_stop_process_cancels_stream(ws_app_harness) -> None:
    from pantaray_agents.schema.events import OutboundEvent

    user_id = "user_test"
    with _ws_connect(ws_app_harness, user_id=user_id) as ws:
        _ = _receive_ws_json(ws, timeout_s=2.0)

        start_relayed_suggestion(ws_app_harness, user_id=user_id, terminal=False)

        started = None
        for _ in range(10):
            msg = _receive_ws_json(ws, timeout_s=2.0)
            if msg.get("event") == OutboundEvent.PROCESS_STARTED.value:
                started = msg
                break
        assert started is not None
        process_id = started.get("data", {}).get("process_id")
        assert process_id

        _send_ws(ws, "stop_process", {"process_id": process_id})

        msgs = _drain_until(ws, {OutboundEvent.PROCESS_COMPLETED.value}, timeout_s=5.0)
        completed = [
            msg
            for msg in msgs
            if msg.get("event") == OutboundEvent.PROCESS_COMPLETED.value
        ]
        assert completed, f"No process_completed in {msgs}"
        assert completed[-1].get("data", {}).get("status") == "canceled"
