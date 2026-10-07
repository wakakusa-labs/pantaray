from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import zanei
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    FailedToolControl,
    ToolValidationError,
    build_tool_execution_control,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.validation import (
    validate_tool_args,
)
from pantaray_agents.agents.action_agent.tools import TOOL_REGISTRY
from pantaray_agents.agents.action_agent.tools.zanei_tools import (
    RECORDING_UNAVAILABLE_STATUS,
    ZANEI_QUERY_TOOL_ID,
    ZANEI_TIMELINE_TOOL_ID,
)
from pantaray_agents.local_runtime.context.source_control import SourceControl
from pantaray_agents.local_runtime.context.source_gate import ActiveSource, SourceGate
from pantaray_agents.local_runtime.context.source_protocol import (
    SQLITE_INTEGER_MAX,
    EvidenceReadRequest,
    EvidenceResponse,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.context.source_reader import ReadTimeout
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime import identity
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    validate_successful_tool_output,
)
from pantaray_agents.schema.context_source import (
    ActivateSource,
    RecorderBinding,
    SetCapturePaused,
    SourceBinding,
    SuspendSource,
)
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.zanei import (
    FREE_TEXT_CHARACTERS,
    IDENTIFIER_CHARACTERS,
    MAX_TIMELINE_PAGES_PER_RUN,
    PAGE_LIMIT,
    ZaneiTools,
)

USER_ID = "user-1"
STORE_ID = "store-1"


def _binding() -> SourceBinding:
    return SourceBinding(
        user_id=USER_ID,
        epoch=UUID(int=1),
        policy_revision="policy-1",
        store_id=STORE_ID,
        protocol_version=1,
    )


def _observation(sequence: int) -> dict[str, object]:
    absent = {"kind": "absent"}
    return {
        "append_sequence": sequence,
        "id": f"event-{sequence}",
        "ts": "2026-09-07T00:01:00Z",
        "source": absent,
        "event_type": {"kind": "value", "text": "browser_navigation"},
        "bundle_id": absent,
        "app_name": {"kind": "value", "text": "Chrome"},
        "pid": None,
        "window_title": {"kind": "value", "text": "An article"},
        "window_id": None,
    }


def _page(
    *, sequences: tuple[int, ...], next_cursor: str, has_more: bool = False
) -> PageResponse:
    return PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": tuple(_observation(sequence) for sequence in sequences),
            "next_cursor": next_cursor,
            "upper_bound": "upper-1",
            "has_more": has_more,
            "coverage": {
                "after": (sequences[0] - 1) if sequences else 0,
                "through": sequences[-1] if sequences else 0,
            },
        }
    )


def _evidence(text: str) -> dict[str, object]:
    encoded = len(text.encode("utf-8"))
    return {
        "protocol_version": 1,
        "kind": "evidence",
        "origin": None,
        "content": {
            "kind": "text",
            "text": text,
            "start": 0,
            "end": encoded,
            "total_bytes": encoded,
            "remaining": None,
        },
        "metadata": {
            "event_type": "browser_navigation",
            "payload_without_text": {},
            "redaction_applied": False,
            "truncated": False,
        },
    }


@dataclass
class _FakeReader:
    pages: list[object] = field(default_factory=list)
    evidence: list[object] = field(default_factory=list)
    page_requests: list[PageReadRequest] = field(default_factory=list)

    async def read_page(
        self, _source: ActiveSource, request: PageReadRequest
    ) -> object:
        self.page_requests.append(request)
        return self.pages.pop(0)

    async def read_evidence(
        self, _source: ActiveSource, request: EvidenceReadRequest
    ) -> object:
        result = self.evidence.pop(0)
        if isinstance(result, dict):
            return EvidenceResponse.model_validate({**result, "origin": request.origin})
        return result


@dataclass
class _CancellingReader:
    gate: SourceGate

    async def read_page(self, _source: ActiveSource, _request: PageReadRequest) -> None:
        # `SourceGate.revoke` drops the permit before it schedules the cancel.
        self.gate.revoke(USER_ID)
        raise asyncio.CancelledError

    async def read_evidence(
        self, _source: ActiveSource, _request: EvidenceReadRequest
    ) -> None:
        raise AssertionError("evidence must not be read in this test")


@dataclass
class _PermitCheckingReader:
    """Stands in for SourceReader, which guards every read with the permit."""

    gate: SourceGate
    pages: list[object] = field(default_factory=list)

    async def read_page(
        self, source: ActiveSource, _request: PageReadRequest
    ) -> object:
        self.gate.require(source)
        return self.pages.pop(0)

    async def read_evidence(
        self, _source: ActiveSource, _request: EvidenceReadRequest
    ) -> None:
        raise AssertionError("evidence must not be read in this test")


def _runtime(session: ZaneiTools | None = None) -> Any:
    return SimpleNamespace(zanei_session=session)


def _state() -> Any:
    return {"user_id": USER_ID}


def _session(reader: object, source: ActiveSource, cursor: str | None) -> ZaneiTools:
    return ZaneiTools(
        reader=reader,  # type: ignore[arg-type]
        source=source,
        cursor=cursor,
        upper_bound=None,
    )


def _activated_gate() -> tuple[SourceGate, ActiveSource]:
    gate = SourceGate()
    gate.activate(_binding())
    source = gate.current(USER_ID)
    assert source is not None
    return gate, source


def _install_gate(monkeypatch: pytest.MonkeyPatch, gate: SourceGate) -> None:
    monkeypatch.setattr(
        zanei, "context_source_control", SimpleNamespace(gate=gate), raising=True
    )


async def _run_timeline(runtime: Any) -> Any:
    return await zanei.run_zanei_timeline_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
        args={},
        state=_state(),
        runtime=runtime,
    )


def _validate_against_tool_schema(tool_id: str, output: object) -> None:
    validate_successful_tool_output(
        tool_id=tool_id,
        output=output,
        output_schema=TOOL_REGISTRY[tool_id].output_schema,  # type: ignore[arg-type]
    )


async def test_timeline_returns_recent_events_and_matches_the_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    reader = _FakeReader(pages=[_page(sequences=(7,), next_cursor="cursor-7")])
    runtime = _runtime(_session(reader, source, "cursor-6"))

    result = await zanei.run_zanei_timeline_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
        args={},
        state=_state(),
        runtime=runtime,
    )

    assert result.status == "success"
    assert isinstance(result.output, dict)
    assert result.output["has_more"] is False
    events = result.output["events"]
    assert isinstance(events, list)
    assert events[0]["event_id"] == "event-7"  # type: ignore[index,call-overload]
    assert events[0]["app"] == "Chrome"  # type: ignore[index,call-overload]
    assert [request.cursor for request in reader.page_requests] == ["cursor-6"]
    _validate_against_tool_schema(ZANEI_TIMELINE_TOOL_ID, result.output)


async def test_query_reads_one_field_of_an_event_from_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    reader = _FakeReader(
        pages=[_page(sequences=(7,), next_cursor="cursor-7")],
        evidence=[_evidence("https://example.test/article")],
    )
    runtime = _runtime(_session(reader, source, None))
    await zanei.run_zanei_timeline_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
        args={},
        state=_state(),
        runtime=runtime,
    )

    result = await zanei.run_zanei_query_tool(
        step_id="step-2",
        tool_def=TOOL_REGISTRY[ZANEI_QUERY_TOOL_ID],
        args={"event_id": "event-7", "field": "url"},
        state=_state(),
        runtime=runtime,
    )

    assert result.status == "success"
    assert isinstance(result.output, dict)
    assert result.output["text"] == "https://example.test/article"
    assert result.output["next_start"] is None
    _validate_against_tool_schema(ZANEI_QUERY_TOOL_ID, result.output)


async def test_recording_off_returns_an_actionable_result_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gate(monkeypatch, SourceGate())

    def _fail_cursor(_binding_value: SourceBinding) -> str | None:
        raise AssertionError("the stream cursor must not be read without a permit")

    monkeypatch.setattr(zanei, "load_stream_cursor", _fail_cursor)
    runtime = _runtime()

    result = await zanei.run_zanei_timeline_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
        args={},
        state=_state(),
        runtime=runtime,
    )

    assert result.status == "success"
    assert result.output == {
        "status": RECORDING_UNAVAILABLE_STATUS,
        "message": zanei.RECORDING_UNAVAILABLE_MESSAGE,
    }
    assert runtime.zanei_session is None
    _validate_against_tool_schema(ZANEI_TIMELINE_TOOL_ID, result.output)


async def test_a_recorder_paused_at_startup_still_answers_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording left off across a restart must not hide what was already recorded.

    The desktop app brings the recorder back for any stored preference and pauses
    capture at once when that preference is off, so the source reaches `ready`
    with `capture_paused` set. The permit survives that transition, which is what
    keeps the activity still held in the store readable.
    """
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    monkeypatch.setattr(
        identity,
        "current_owner_id",
        lambda: USER_ID,
    )
    control = SourceControl()
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        conn.execute(
            "INSERT INTO users VALUES (?, 'ja', NULL, 'now', 'now')", (USER_ID,)
        )
        conn.commit()
        stopped = await control.transition(
            conn,
            USER_ID,
            SuspendSource(
                kind="suspend",
                request_id=uuid4(),
                expected_epoch=UUID(int=0),
                policy_revision="policy-1",
                reason="policy_change",
            ),
        )
        started = await control.transition(
            conn,
            USER_ID,
            ActivateSource(
                kind="activate",
                request_id=uuid4(),
                issued_epoch=stopped.state.epoch,
                recorder_binding=RecorderBinding(store_id=STORE_ID, protocol_version=1),
            ),
        )
        paused = await control.transition(
            conn,
            USER_ID,
            SetCapturePaused(
                kind="set_capture_paused",
                request_id=uuid4(),
                expected_epoch=started.state.binding.epoch,
                paused=True,
            ),
        )
        assert paused.state.capture_paused is True
        # The permit is still live, so the source keeps reading as ready. Without
        # one the read downgrades to stopped, which is the "recording unavailable"
        # a restart with recording off used to leave behind.
        assert (await control.read(conn, USER_ID)).kind == "ready"

    _install_gate(monkeypatch, control.gate)
    monkeypatch.setattr(zanei, "load_stream_cursor", lambda _binding: "cursor-6")
    reader = _FakeReader(pages=[_page(sequences=(7,), next_cursor="cursor-7")])
    monkeypatch.setattr(zanei, "SourceReader", lambda _gate: reader)
    runtime = _runtime()

    result = await _run_timeline(runtime)

    assert result.status == "success"
    assert isinstance(result.output, dict)
    events = result.output["events"]
    assert isinstance(events, list)
    assert events[0]["event_id"] == "event-7"  # type: ignore[index,call-overload]
    assert [request.cursor for request in reader.page_requests] == ["cursor-6"]


async def test_source_invalidation_becomes_a_recording_unavailable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    runtime = _runtime(_session(_CancellingReader(gate), source, None))

    result = await zanei.run_zanei_timeline_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
        args={},
        state=_state(),
        runtime=runtime,
    )

    assert result.status == "success"
    assert isinstance(result.output, dict)
    assert result.output["status"] == RECORDING_UNAVAILABLE_STATUS
    assert runtime.zanei_session is None
    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0


async def test_cancellation_that_keeps_the_permit_still_cancels_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)

    @dataclass
    class _ExternalCancelReader:
        async def read_page(
            self, _source: ActiveSource, _request: PageReadRequest
        ) -> None:
            raise asyncio.CancelledError

    runtime = _runtime(_session(_ExternalCancelReader(), source, None))

    with pytest.raises(asyncio.CancelledError):
        await zanei.run_zanei_timeline_tool(
            step_id="step-1",
            tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
            args={},
            state=_state(),
            runtime=runtime,
        )


async def test_a_revoked_permit_drops_the_session_and_a_new_permit_reads_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    reader = _PermitCheckingReader(
        gate=gate,
        pages=[
            _page(sequences=(4,), next_cursor="cursor-4", has_more=True),
            _page(sequences=(5,), next_cursor="cursor-5"),
        ],
    )
    monkeypatch.setattr(zanei, "SourceReader", lambda _gate: reader)
    monkeypatch.setattr(zanei, "load_stream_cursor", lambda _binding_value: "cursor-3")
    runtime = _runtime(_session(reader, source, "cursor-3"))

    first = await _run_timeline(runtime)
    assert first.status == "success"
    assert isinstance(first.output, dict)
    assert first.output["events"]

    gate.revoke(USER_ID)
    second = await _run_timeline(runtime)

    assert second.status == "success"
    assert second.output == {
        "status": RECORDING_UNAVAILABLE_STATUS,
        "message": zanei.RECORDING_UNAVAILABLE_MESSAGE,
    }
    assert runtime.zanei_session is None

    gate.activate(_binding())
    third = await _run_timeline(runtime)

    assert third.status == "success"
    assert isinstance(third.output, dict)
    assert third.output["events"][0]["event_id"] == "event-5"  # type: ignore[index,call-overload]
    assert runtime.zanei_session is not None


def test_a_start_offset_above_the_protocol_maximum_is_a_malformed_argument() -> None:
    with pytest.raises(ToolValidationError):
        validate_tool_args(
            TOOL_REGISTRY[ZANEI_QUERY_TOOL_ID],
            {"event_id": "event-7", "field": "url", "start": SQLITE_INTEGER_MAX + 1},
        )


async def test_a_reader_failure_becomes_a_bounded_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    runtime = _runtime(_session(_FakeReader(pages=[ReadTimeout()]), source, None))

    result = await zanei.run_zanei_timeline_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
        args={},
        state=_state(),
        runtime=runtime,
    )

    assert result.status == "error"
    assert isinstance(result.output, dict)
    error = result.output["error"]
    assert isinstance(error, dict)
    assert error["error_type"] == zanei.ZANEI_READ_FAILED_ERROR_TYPE


async def test_the_session_starts_at_the_stream_cursor_and_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, _source = _activated_gate()
    _install_gate(monkeypatch, gate)
    reader = _FakeReader(
        pages=[
            _page(sequences=(4,), next_cursor="cursor-4", has_more=True),
            _page(sequences=(5,), next_cursor="cursor-5"),
        ]
    )
    monkeypatch.setattr(zanei, "SourceReader", lambda _gate: reader)
    cursor_reads: list[SourceBinding] = []

    def _stored_cursor(binding_value: SourceBinding) -> str | None:
        cursor_reads.append(binding_value)
        return "cursor-3"

    monkeypatch.setattr(zanei, "load_stream_cursor", _stored_cursor)
    runtime = _runtime()

    for _ in range(2):
        await zanei.run_zanei_timeline_tool(
            step_id="step-1",
            tool_def=TOOL_REGISTRY[ZANEI_TIMELINE_TOOL_ID],
            args={},
            state=_state(),
            runtime=runtime,
        )

    assert [request.cursor for request in reader.page_requests] == [
        "cursor-3",
        "cursor-4",
    ]
    assert len(cursor_reads) == 1
    assert runtime.zanei_session is not None
    assert runtime.zanei_session.cursor == "cursor-5"


def _dense_observation(sequence: int) -> dict[str, object]:
    """One row at the projection caps: the largest row the recorder can produce."""

    def value(text: str) -> dict[str, str]:
        return {"kind": "value", "text": text}

    return {
        "append_sequence": sequence,
        "id": f"event-{sequence}".ljust(36, "0"),
        "ts": "2026-09-07T00:01:00.123456+00:00",
        "source": value("s" * (IDENTIFIER_CHARACTERS + 1)),
        "event_type": value("t" * (IDENTIFIER_CHARACTERS + 1)),
        "bundle_id": value("b" * (IDENTIFIER_CHARACTERS + 1)),
        "app_name": value("a" * (FREE_TEXT_CHARACTERS + 1)),
        "pid": None,
        "window_title": value("w" * (FREE_TEXT_CHARACTERS + 1)),
        "window_id": 999_999_999,
    }


def _dense_page() -> PageResponse:
    return PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": tuple(
                _dense_observation(sequence) for sequence in range(1, PAGE_LIMIT + 1)
            ),
            "next_cursor": "cursor-dense",
            "upper_bound": "upper-1",
            "has_more": True,
            "coverage": {"after": 0, "through": PAGE_LIMIT},
        }
    )


async def test_a_worst_case_page_still_reaches_the_model_as_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    runtime = _runtime(_session(_FakeReader(pages=[_dense_page()]), source, None))

    result = await _run_timeline(runtime)

    assert result.status == "success"
    assert isinstance(result.output, dict)
    # Above the inline limit the durable store replaces the whole result with an
    # action_file reference, leaving the model no event_id to query.
    assert (
        len(serialize_json_tool_output(result.output))
        < ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    events = result.output["events"]
    assert isinstance(events, list)
    omitted = result.output["older_events_omitted"]
    assert isinstance(omitted, int)
    assert len(events) + omitted == PAGE_LIMIT
    # The newest events are the ones the Action asks about, so they are kept.
    assert events[-1]["event_id"] == f"event-{PAGE_LIMIT}".ljust(36, "0")  # type: ignore[index,call-overload]
    assert result.output["has_more"] is True
    _validate_against_tool_schema(ZANEI_TIMELINE_TOOL_ID, result.output)


async def test_a_page_that_fits_is_returned_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    reader = _FakeReader(pages=[_page(sequences=(7, 8), next_cursor="cursor-8")])
    runtime = _runtime(_session(reader, source, None))

    result = await _run_timeline(runtime)

    assert isinstance(result.output, dict)
    assert "older_events_omitted" not in result.output
    assert len(result.output["events"]) == 2  # type: ignore[arg-type]


async def test_a_rejected_read_keeps_its_zanei_error_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    runtime = _runtime(_session(_FakeReader(), source, None))

    result = await zanei.run_zanei_query_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[ZANEI_QUERY_TOOL_ID],
        # An event id from before a resume: this run's session never saw it.
        args={"event_id": "event-from-a-previous-run", "field": "url"},
        state=_state(),
        runtime=runtime,
    )

    assert result.status == "error"
    control = build_tool_execution_control(status=result.status, output=result.output)
    assert isinstance(control, FailedToolControl)
    assert control.failure.error_type == "ZANEI_EVENT_OUT_OF_RANGE"
    assert "zanei_timeline" in control.failure.message


async def test_a_drained_session_reads_activity_recorded_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    reader = _FakeReader(pages=[_page(sequences=(7,), next_cursor="cursor-7")])
    runtime = _runtime(_session(reader, source, "cursor-6"))

    first = await _run_timeline(runtime)
    assert isinstance(first.output, dict)
    assert first.output["has_more"] is False

    # The user keeps working while the Action runs, so the host has more to give.
    reader.pages.append(_page(sequences=(8,), next_cursor="cursor-8"))
    second = await _run_timeline(runtime)

    assert second.status == "success"
    assert isinstance(second.output, dict)
    assert second.output["events"][0]["event_id"] == "event-8"  # type: ignore[index,call-overload]
    # Read on from the session's own cursor, against a fresh snapshot.
    assert [request.cursor for request in reader.page_requests] == [
        "cursor-6",
        "cursor-7",
    ]
    assert reader.page_requests[-1].upper_bound is None


async def test_a_spent_page_budget_is_a_readable_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source = _activated_gate()
    _install_gate(monkeypatch, gate)
    session = _session(_FakeReader(), source, "cursor-6")
    session.pages_read = MAX_TIMELINE_PAGES_PER_RUN

    result = await _run_timeline(_runtime(session))

    assert result.status == "success"
    assert isinstance(result.output, dict)
    assert result.output["page_budget_spent"] is True
    assert result.output["events"] == []
    _validate_against_tool_schema(ZANEI_TIMELINE_TOOL_ID, result.output)
