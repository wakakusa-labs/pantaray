from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from pantaray_agents.agents.insight_agent.agent import ShortInsightOutput
from pantaray_agents.agents.insight_agent.record_verification import (
    SourceRecordClaim,
    VerifiedRecord,
)
from pantaray_agents.agents.insight_agent.zanei_tools import (
    EVENT_TOOL,
    PAGE_LIMIT,
    PAGE_TOOL,
    ZaneiTools,
)
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z
from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_control import SourceControl
from pantaray_agents.local_runtime.context.source_gate import (
    ActiveSource,
    SourceGate,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.context.source_protocol import (
    EvidenceReadRequest,
    EvidenceResponse,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime import identity, short_insight
from pantaray_agents.local_runtime.runtime.activity_local_repository import (
    SQLiteActivityRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.activity_summary_scheduler import (
    calculate_summary_window,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    build_local_insight_enqueue_request,
    build_short_insight_job_payload,
    short_insight_window_start,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_control import DeferredLocalJob
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    bind_job_route_identity,
)
from pantaray_agents.local_runtime.runtime.job_types import LOCAL_INSIGHT_JOB_TYPE
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
)
from pantaray_agents.local_runtime.runtime.short_insight import (
    _source_record_id,
    run_short_insight,
)
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    enqueue_suggestion_for_insight,
    suggestion_id_for_insight,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.context_source import (
    ActivateSource,
    RecorderBinding,
    SourceBinding,
    SuspendSource,
)
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

from .migrated_db import prepare_test_database

USER = "user-1"
RUN = "run-1"
PERIOD_START = "2026-09-07T00:00:00Z"
PERIOD_END = "2026-09-07T00:04:00Z"
STORE_ID = "store-1"
# One invented chat screen, and the record a run claims after reading it.
SCREEN = "設計の相談\nおおたに りん  09:40\n配色の候補を 3 つ用意しました\n"
QUOTE = "配色の候補を 3 つ用意しました"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(path, 1_000, load_default_migrations())
    with open_memory_catalog_connection(db_path=path, busy_timeout_ms=1_000) as conn:
        conn.execute("INSERT INTO users VALUES (?, 'ja', NULL, 'now', 'now')", (USER,))
        conn.execute(
            "INSERT INTO users VALUES ('other', 'ja', NULL, 'now', 'now')",
        )
        conn.commit()
    return path


def _observation(sequence: int) -> dict[str, object]:
    absent = {"kind": "absent"}
    return {
        "append_sequence": sequence,
        "id": f"event-{sequence}",
        "ts": "2026-09-07T00:01:00Z",
        "source": absent,
        "event_type": absent,
        "bundle_id": absent,
        "app_name": {"kind": "value", "text": "Editor"},
        "pid": None,
        "window_title": absent,
        "window_id": None,
    }


def _page(*, observations: int, next_cursor: str) -> PageResponse:
    return PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": tuple(
                _observation(index + 1) for index in range(observations)
            ),
            "next_cursor": next_cursor,
            "upper_bound": "upper-1",
            "has_more": False,
            "coverage": {"after": 0, "through": observations},
        }
    )


@dataclass
class _FakeReader:
    pages: list[PageResponse]
    requests: list[object] = field(default_factory=list)

    async def read_page(self, _source: ActiveSource, request: object) -> PageResponse:
        self.requests.append(request)
        return self.pages.pop(0)

    async def read_evidence(
        self, _source: ActiveSource, request: EvidenceReadRequest
    ) -> EvidenceResponse:
        return EvidenceResponse.model_validate(
            {
                "protocol_version": 1,
                "kind": "evidence",
                "origin": request.origin,
                "content": {
                    "kind": "text",
                    "text": SCREEN,
                    "start": 0,
                    "end": len(SCREEN.encode("utf-8")),
                    "total_bytes": len(SCREEN.encode("utf-8")),
                    "remaining": None,
                },
                "metadata": {
                    "event_type": "content.snapshot",
                    "payload_without_text": {},
                    "redaction_applied": False,
                    "truncated": False,
                },
            }
        )


def _timeline_call() -> ReactToolCall:
    return ReactToolCall(
        tool_name=PAGE_TOOL,
        tool_args={},
        tool_call_envelope=ToolCallEnvelope(tool_id=PAGE_TOOL, reason=None, args={}),
    )


def _query_call(event_id: str) -> ReactToolCall:
    args: dict[str, object] = {"event_id": event_id, "field": "text"}
    return ReactToolCall(
        tool_name=EVENT_TOOL,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=EVENT_TOOL, reason=None, args=args),
    )


def _claim(**overrides: str) -> SourceRecordClaim:
    return SourceRecordClaim.model_validate(
        {
            "event_id": "event-1",
            "source": "設計の相談",
            "speaker": "おおたに りん",
            "shown_time": "09:40",
            "quote": QUOTE,
            **overrides,
        }
    )


@dataclass
class _FakeAgent:
    """Stand in for the LLM loop: read one timeline page, then complete."""

    output: ShortInsightOutput
    calls: int = 0
    before_output: object = None
    read_event: str | None = None

    async def generate(
        self, *, zanei: ZaneiTools, **_kwargs: object
    ) -> ShortInsightOutput:
        self.calls += 1
        await zanei.timeline(_timeline_call(), 1)
        if self.read_event is not None:
            await zanei.query(_query_call(self.read_event), 2)
        if self.before_output is not None:
            await self.before_output()
        return self.output


def _output(
    reason: str | None = "The user switched goals.",
    records: list[SourceRecordClaim] | None = None,
) -> ShortInsightOutput:
    return ShortInsightOutput(
        activity="# Activity\nThe user edited the parser.",
        insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason=reason,
        records=records or [],
    )


async def _active_source(gate: SourceGate) -> ActiveSource:
    binding = SourceBinding(
        user_id=USER,
        epoch=UUID(int=1),
        policy_revision="policy-1",
        store_id=STORE_ID,
        protocol_version=1,
    )
    async with gate.turn():
        gate.activate(binding)
        source = gate.current(USER)
    assert source is not None
    return source


async def _run(
    *,
    db_path: Path,
    gate: SourceGate,
    reader: object,
    agent: object,
    run_id: str = RUN,
    user_id: str = USER,
) -> None:
    await run_short_insight(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=user_id,
        run_id=run_id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        gate=gate,
        reader=reader,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
    )


def _stored(db_path: Path, *, run_id: str = RUN) -> dict[str, object]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        insight = connection.execute(
            "SELECT status, short_term_insight_data, reconsideration_reason, "
            "source_cursor FROM agent_insights WHERE insight_id = ?",
            (run_id,),
        ).fetchone()
        activity = connection.execute(
            "SELECT status, description, period_start, period_end "
            "FROM activity_logs WHERE log_id = ?",
            (run_id,),
        ).fetchone()
        catalog = connection.execute(
            "SELECT source_type FROM memory_nodes WHERE source_record_id = ? "
            "ORDER BY source_type",
            (run_id,),
        ).fetchall()
        cursor = connection.execute("SELECT cursor FROM context_streams").fetchone()
        suggestions = connection.execute(
            "SELECT suggestion_id, status FROM agent_suggestions"
        ).fetchall()
        jobs = connection.execute("SELECT job_type, logical_key FROM jobs").fetchall()
        triggers = connection.execute(
            "SELECT trigger_kind, source_id, status FROM memory_agent_triggers"
        ).fetchall()
        records = connection.execute(
            "SELECT record_id, run_id, event_id, observed_at, app_name, bundle_id, "
            "window_title, source, speaker, shown_time, quote FROM source_records "
            "ORDER BY quote"
        ).fetchall()
    return {
        "insight": tuple(insight) if insight else None,
        "activity": tuple(activity) if activity else None,
        "catalog": [str(row[0]) for row in catalog],
        "cursor": None if cursor is None else str(cursor[0]),
        "suggestions": [tuple(row) for row in suggestions],
        "jobs": [tuple(row) for row in jobs],
        "triggers": [tuple(row) for row in triggers],
        "records": [tuple(row) for row in records],
    }


async def test_empty_period_still_updates_context_and_requests_memory_review(
    db_path: Path,
) -> None:
    gate = SourceGate()
    source = await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=0, next_cursor="cursor-1")])
    agent = _FakeAgent(output=_output())

    await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)

    assert agent.calls == 1
    stored = _stored(db_path)
    assert stored["cursor"] == "cursor-1"
    assert stored["insight"] is not None
    assert stored["activity"] is not None
    assert stored["triggers"] == [("memory_from_short_insight", RUN, "pending")]
    assert stored["suggestions"] == []
    assert source.binding.user_id == USER


async def test_successful_run_commits_outputs_catalog_and_cursor(
    db_path: Path,
) -> None:
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=2, next_cursor="cursor-2")])

    await _run(
        db_path=db_path, gate=gate, reader=reader, agent=_FakeAgent(output=_output())
    )

    stored = _stored(db_path)
    assert stored["insight"] == (
        "success",
        "# Insight\nThe user is fixing a parser bug.",
        "The user switched goals.",
        "cursor-2",
    )
    assert stored["activity"] == (
        "success",
        "# Activity\nThe user edited the parser.",
        PERIOD_START,
        PERIOD_END,
    )
    assert stored["catalog"] == ["activity_log", "short_term_insight"]
    assert stored["cursor"] == "cursor-2"
    assert stored["suggestions"] == []
    assert stored["jobs"] == []
    assert stored["triggers"] == [("memory_from_short_insight", RUN, "pending")]


async def test_catalog_failure_rolls_back_the_cursor_and_every_output(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])
    original = short_insight.register_inline_domain_memory
    calls = {"count": 0}

    def _fail_second(**kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("catalog publication failed")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(short_insight, "register_inline_domain_memory", _fail_second)

    with pytest.raises(RuntimeError, match="catalog publication failed"):
        await _run(
            db_path=db_path,
            gate=gate,
            reader=reader,
            agent=_FakeAgent(output=_output(records=[_claim()]), read_event="event-1"),
        )

    assert _stored(db_path) == {
        "insight": None,
        "activity": None,
        "catalog": [],
        "cursor": None,
        "suggestions": [],
        "jobs": [],
        "triggers": [],
        "records": [],
    }


async def test_a_concurrent_cursor_move_rejects_the_run(db_path: Path) -> None:
    gate = SourceGate()
    source = await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])

    async def _move_cursor() -> None:
        with open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=1_000
        ) as connection:
            with immediate_transaction(connection):
                store.compare_cursor(connection, source.binding, None, "cursor-other")

    agent = _FakeAgent(output=_output(), before_output=_move_cursor)

    with pytest.raises(store.ContextStoreConflict):
        await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)

    stored = _stored(db_path)
    assert stored["cursor"] == "cursor-other"
    assert stored["insight"] is None
    assert stored["activity"] is None


@pytest.mark.parametrize(
    ("reason", "reactivate"),
    [
        ("policy_change", False),
        ("policy_change", True),
        ("shutdown", True),
        ("disabled", False),
    ],
)
async def test_recorder_changes_during_inference_preserve_range_and_publication(
    db_path: Path, monkeypatch, reason, reactivate
) -> None:
    monkeypatch.setattr(
        identity,
        "current_owner_id",
        lambda: USER,
    )
    control = SourceControl()
    entered, release = asyncio.Event(), asyncio.Event()
    reader = _FakeReader(
        pages=[
            _page(observations=1, next_cursor="cursor-1").model_copy(
                update={"has_more": True}
            ),
            PageResponse.model_validate(
                {
                    **_page(observations=2, next_cursor="cursor-2").model_dump(),
                    "observations": (_observation(2),),
                    "coverage": {"after": 1, "through": 2},
                }
            ),
        ]
    )

    class Agent:
        async def generate(
            self, *, zanei: ZaneiTools, **_kwargs: object
        ) -> ShortInsightOutput:
            await zanei.timeline(_timeline_call(), 1)
            entered.set()
            await release.wait()
            await zanei.timeline(_timeline_call(), 2)
            return _output()

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        stopped = await control.transition(
            connection,
            USER,
            SuspendSource(
                kind="suspend",
                request_id=uuid4(),
                expected_epoch=UUID(int=0),
                policy_revision="policy-1",
                reason="policy_change",
            ),
        )
        ready = await control.transition(
            connection,
            USER,
            ActivateSource(
                kind="activate",
                request_id=uuid4(),
                issued_epoch=stopped.state.epoch,
                recorder_binding=RecorderBinding(store_id=STORE_ID, protocol_version=1),
                capture_paused=False,
            ),
        )
        task = asyncio.create_task(
            _run(db_path=db_path, gate=control.gate, reader=reader, agent=Agent())
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stopped = await control.transition(
                connection,
                USER,
                SuspendSource(
                    kind="suspend",
                    request_id=uuid4(),
                    expected_epoch=ready.state.binding.epoch,
                    policy_revision="policy-2",
                    reason=reason,
                ),
            )
            if reactivate:
                ready = await control.transition(
                    connection,
                    USER,
                    ActivateSource(
                        kind="activate",
                        request_id=uuid4(),
                        issued_epoch=stopped.state.epoch,
                        recorder_binding=RecorderBinding(
                            store_id=STORE_ID, protocol_version=1
                        ),
                        capture_paused=False,
                    ),
                )
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert store.get_cursor(connection, ready.state.binding) == "cursor-2"
    assert reader.requests[1] == PageReadRequest(
        cursor="cursor-1", upper_bound="upper-1", limit=PAGE_LIMIT
    )
    stored = _stored(db_path)
    assert stored["insight"] == (
        "success",
        _output().insight,
        _output().reconsideration_reason,
        "cursor-2",
    )
    assert stored["activity"][0] == "success"
    assert stored["catalog"] == ["activity_log", "short_term_insight"]
    assert stored["triggers"] == [("memory_from_short_insight", RUN, "pending")]


async def test_a_revoked_source_fails_the_run_without_publishing(db_path: Path) -> None:
    """Revocation cancels the run; the job must still end with a normal error."""
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])

    async def _revoke() -> None:
        async with gate.turn():
            gate.revoke(USER)
        # Let the loop deliver the cancel inside the model call.
        await asyncio.sleep(0)

    agent = _FakeAgent(output=_output(), before_output=_revoke)

    with pytest.raises(SourceInvalidated):
        await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)

    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0
    assert _stored(db_path) == {
        "insight": None,
        "activity": None,
        "catalog": [],
        "cursor": None,
        "suggestions": [],
        "jobs": [],
        "triggers": [],
        "records": [],
    }


async def test_a_replayed_run_is_a_no_op_and_another_users_run_is_rejected(
    db_path: Path,
) -> None:
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(
        pages=[
            _page(observations=1, next_cursor="cursor-2"),
            _page(observations=1, next_cursor="cursor-3"),
        ]
    )
    agent = _FakeAgent(output=_output())

    await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)
    await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)

    assert agent.calls == 1
    assert _stored(db_path)["cursor"] == "cursor-2"

    other_gate = SourceGate()
    other_binding = SourceBinding(
        user_id="other",
        epoch=uuid4(),
        policy_revision="policy-1",
        store_id=STORE_ID,
        protocol_version=1,
    )
    async with other_gate.turn():
        other_gate.activate(other_binding)

    with pytest.raises(ValueError, match="belongs to another user"):
        await _run(
            db_path=db_path,
            gate=other_gate,
            reader=reader,
            agent=agent,
            user_id="other",
        )


async def test_a_stored_run_is_selected_by_the_hourly_activity_summary(
    db_path: Path,
) -> None:
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])
    payload = build_short_insight_job_payload(
        user_id=USER,
        window_start=short_insight_window_start(
            datetime(2026, 9, 7, 10, 58, 30, tzinfo=UTC)
        ),
    )

    await run_short_insight(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        run_id=payload["insight_id"],
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        gate=gate,
        reader=reader,  # type: ignore[arg-type]
        agent=_FakeAgent(output=_output()),
    )

    window = calculate_summary_window(
        summary_type="1h", now=datetime(2026, 9, 7, 11, 5, tzinfo=UTC)
    )
    repository = SQLiteActivityRuntimeRepository(db_path=db_path, busy_timeout_ms=1_000)
    selected = await repository.get_source_data_for_summary(
        USER,
        "1h",
        iso_z(window.period_start),
        iso_z(window.period_end),
    )

    assert selected.data is not None
    assert [row["log_id"] for row in selected.data] == [payload["insight_id"]]


@pytest.mark.parametrize("reason", [None, ""])
async def test_an_unchanged_insight_still_requests_memory_review(
    db_path: Path, reason: str | None
) -> None:
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])

    await _run(
        db_path=db_path,
        gate=gate,
        reader=reader,
        agent=_FakeAgent(output=_output(reason=reason)),
    )

    stored = _stored(db_path)
    assert stored["cursor"] == "cursor-2"
    assert stored["catalog"] == ["activity_log", "short_term_insight"]
    assert stored["triggers"] == [("memory_from_short_insight", RUN, "pending")]
    assert stored["suggestions"] == []
    assert stored["jobs"] == []


async def test_a_repeated_enqueue_for_one_insight_stays_a_single_suggestion(
    db_path: Path,
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with immediate_transaction(connection):
            for enqueued_at in (PERIOD_END, "2026-09-07T12:00:00Z"):
                enqueue_suggestion_for_insight(
                    connection=connection,
                    user_id=USER,
                    insight_id=RUN,
                    now=enqueued_at,
                )

    suggestion_id = suggestion_id_for_insight(RUN)
    stored = _stored(db_path)
    assert stored["suggestions"] == [(suggestion_id, "processing")]
    assert stored["jobs"] == [("generate_suggestion", suggestion_id)]


async def test_an_insight_produced_on_a_replaced_route_is_never_published(
    db_path: Path,
) -> None:
    """The account changed while the agent ran, so nothing commits.

    The activity log, the Insight, the catalog entries, the memory trigger and
    the read cursor commit together, so the run that produced them for the
    previous owner must reach none of them and start over instead.
    """

    def _sign_in(session_version: str) -> None:
        import_desktop_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id=USER,
            desktop_access_token="header.payload.signature",
            expires_at="2099-09-07T00:00:00Z",
            session_version=session_version,
        )

    payload = build_short_insight_job_payload(
        user_id=USER, window_start=datetime(2026, 9, 7, tzinfo=UTC)
    )
    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_insight_enqueue_request(payload),
    )
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type=LOCAL_INSIGHT_JOB_TYPE,
            owner_user_id=USER,
            claimed_by="worker-1",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )
    identity.register_logged_out_owner("local-owner")
    mark_configured()
    _sign_in("1")

    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=2, next_cursor="cursor-2")])

    async def _replace_the_route() -> None:
        # A second sign-in of the same account is a new cloud identity, applied
        # after the agent produced its output and before it is committed.
        _sign_in("2")

    agent = _FakeAgent(output=_output(), before_output=_replace_the_route)

    try:
        with bind_job_route_identity(
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            process_pending_status="enqueued",
            db_path=db_path,
            busy_timeout_ms=1_000,
            requeue_on_change=True,
        ):
            with pytest.raises(DeferredLocalJob):
                await _run(
                    db_path=db_path,
                    gate=gate,
                    reader=reader,
                    agent=agent,
                    run_id=payload["insight_id"],
                )
    finally:
        identity.reset_logged_out_owner()

    assert _stored(db_path, run_id=payload["insight_id"]) == {
        "insight": None,
        "activity": None,
        "catalog": [],
        "cursor": None,
        "suggestions": [],
        "jobs": [(LOCAL_INSIGHT_JOB_TYPE, USER)],
        "triggers": [],
        "records": [],
    }
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status, claimed_by FROM jobs WHERE job_id = ?",
            (payload["job_id"],),
        ).fetchone() == ("queued", None)


async def test_verified_records_are_appended_beside_the_activity_log(
    db_path: Path,
) -> None:
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])
    agent = _FakeAgent(
        output=_output(records=[_claim(), _claim()]), read_event="event-1"
    )

    await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)

    stored = _stored(db_path)
    assert stored["cursor"] == "cursor-2"
    assert stored["catalog"] == ["activity_log", "short_term_insight", "source_records"]
    (record,) = stored["records"]
    _record_id, *fields = record
    assert fields == [
        RUN,
        "event-1",
        "2026-09-07T00:01:00Z",
        "Editor",
        None,
        None,
        "設計の相談",
        "おおたに りん",
        "09:40",
        QUOTE,
    ]
    await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)
    assert _stored(db_path)["records"] == stored["records"]


@pytest.mark.parametrize("malformed", [False, True])
async def test_records_the_fetched_text_cannot_prove_never_reach_the_store(
    db_path: Path,
    malformed: bool,
) -> None:
    """A run whose records all fail must still commit the outputs and cursor."""
    gate = SourceGate()
    await _active_source(gate)
    reader = _FakeReader(pages=[_page(observations=1, next_cursor="cursor-2")])
    agent = _FakeAgent(
        output=_output(
            records=[
                _claim(quote="配色の案を三つ出した"),
                _claim(source="別のスレッド"),
                _claim(event_id="event-2"),
            ]
        ),
        read_event="event-1",
    )

    if malformed:
        payload = agent.output.model_dump()
        payload["records"] = [{"event_id": "event-1", "quote": QUOTE}]
        agent.output = ShortInsightOutput.model_validate(payload)
    await _run(db_path=db_path, gate=gate, reader=reader, agent=agent)

    stored = _stored(db_path)
    assert stored["records"] == []
    assert stored["cursor"] == "cursor-2"
    assert stored["activity"] is not None
    assert stored["insight"] is not None
    assert stored["triggers"] == [("memory_from_short_insight", RUN, "pending")]


def test_a_record_id_separates_fields_a_joined_string_would_blur() -> None:
    """Two different records must never share an id, whatever a screen shows."""

    def _record(source: str, speaker: str) -> VerifiedRecord:
        return VerifiedRecord(
            event_id="event-1",
            observed_at="2026-09-07T00:01:00Z",
            app_name=None,
            bundle_id=None,
            window_title=None,
            source=source,
            speaker=speaker,
            shown_time="09:40",
            quote=QUOTE,
        )

    left = _record("案件A", "さとう:りく")
    right = _record("案件A:さとう", "りく")

    assert _source_record_id(run_id=RUN, record=left) != _source_record_id(
        run_id=RUN, record=right
    )
    assert _source_record_id(run_id=RUN, record=left) != _source_record_id(
        run_id="run-2", record=left
    )
    assert _source_record_id(run_id=RUN, record=left) == _source_record_id(
        run_id=RUN, record=_record("案件A", "さとう:りく")
    )
