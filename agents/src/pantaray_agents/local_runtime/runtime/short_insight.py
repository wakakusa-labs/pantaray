"""One raw-source run; derived outputs and cursor commit together."""

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pantaray_agents.agents.insight_agent.agent import InsightAgent, ShortInsightOutput
from pantaray_agents.agents.insight_agent.record_verification import (
    VerifiedRecord,
    verify_source_records,
)
from pantaray_agents.local_runtime.activity_summary_schedule import parse_iso_z
from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_gate import (
    SourceGate,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.context.source_protocol import (
    GapResponse,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.context.source_reader import SourceReader
from pantaray_agents.local_runtime.context.source_transport import source_scope
from pantaray_agents.local_runtime.integrations.change_probe import (
    collect_external_changes,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.memory_catalog.source_records import (
    register_source_records_memory,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    SHORT_INSIGHT_WINDOW_SECONDS,
)
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    require_current_route_identity,
)
from pantaray_agents.local_runtime.runtime.memory_agent_triggers import (
    SHORT_INSIGHT_MEMORY_TRIGGER_KIND,
    insert_pending_memory_agent_trigger,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.repository.workspace_context import (
    load_workspace_structure_prompt,
)
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.tools.zanei import PAGE_LIMIT, ZaneiTools

logger = logging.getLogger(__name__)

# uuid5(uuid.NAMESPACE_URL, "pantaray:source_record:v1")
NAMESPACE_SOURCE_RECORD: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:source_record:v1"
)

# How far back the first run after an absence asks connected apps for changes.
# A night or a day away stays covered; past that, each probe returns at most a
# handful of items (several services in no guaranteed order), so a longer reach
# would trade the newest items for stale ones rather than add coverage.
PROBE_MAX_LOOKBACK: Final = timedelta(hours=24)


async def run_short_insight(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    run_id: str,
    period_start: str,
    period_end: str,
    gate: SourceGate,
    reader: SourceReader,
    agent: InsightAgent,
) -> None:
    async with gate.turn():
        source = gate.current(user_id)
    if source is None:
        return
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        existing = connection.execute(
            "SELECT user_id FROM agent_insights WHERE insight_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != user_id:
                raise ValueError("short Insight run belongs to another user")
            return
        cursor = store.get_cursor(connection, source.binding)
        # The activity log shares the Insight's id and commits with it, so its
        # period end is where the previous run's window closed.
        previous = connection.execute(
            "SELECT insights.short_term_insight_data, activity.period_end "
            "FROM agent_insights AS insights "
            "LEFT JOIN activity_logs AS activity "
            "ON activity.log_id = insights.insight_id "
            "WHERE insights.user_id = ? AND insights.status = 'success' "
            "ORDER BY insights.created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    try:
        async with gate.track(source):
            with source_scope(gate, source):
                page = await reader.read_page(
                    source,
                    PageReadRequest(cursor=cursor, upper_bound=None, limit=PAGE_LIMIT),
                )
            if not isinstance(page, (PageResponse, GapResponse)):
                raise RuntimeError(f"Zanei timeline unavailable: {type(page).__name__}")
            # Nothing was recorded since the last committed read: the user is
            # away, so the window gets no Insight and therefore no Memory update
            # or Suggestion, and connected apps are not asked either. The cursor
            # stays put, and the job row for this window keeps it from running
            # again. A gap or a further page is not proof of absence.
            if (
                isinstance(page, PageResponse)
                and not page.observations
                and not page.has_more
            ):
                logger.info(
                    "Short Insight skipped: nothing recorded", extra={"run_id": run_id}
                )
                return
            # Outside `source_scope`: the probe sends no raw activity.
            external_changes = await collect_external_changes(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                run_id=run_id,
                since=_probe_since(
                    period_start=parse_iso_z(period_start),
                    previous_period_end=(
                        None
                        if previous is None or previous[1] is None
                        else parse_iso_z(str(previous[1]))
                    ),
                ),
            )
            with source_scope(gate, source):
                zanei = ZaneiTools(
                    reader=reader,
                    source=source,
                    cursor=cursor,
                    upper_bound=page.upper_bound,
                    first_page=page,
                )
                output = await agent.generate(
                    run_id=run_id,
                    user_id=user_id,
                    zanei=zanei,
                    workspace_context=load_workspace_structure_prompt(
                        db_path=db_path,
                        busy_timeout_ms=busy_timeout_ms,
                        user_id=user_id,
                    ),
                    previous_insight=str(previous[0]) if previous else "(none)",
                    external_changes=external_changes,
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                )
                if zanei.cursor is None:
                    raise ValueError("short Insight completed without a source cursor")
                verification = verify_source_records(
                    claims=output.records, events=zanei.read_events()
                )
                # Counts only: the quotes, headers and speaker names they
                # describe are the user's own screen and stay out of the log.
                logger.info(
                    "Short Insight source records",
                    extra={
                        "run_id": run_id,
                        "records_accepted": len(verification.accepted),
                        "records_rejected": dict(verification.rejected),
                    },
                )
                # The activity log, the Insight and the cursor commit together
                # below and nowhere else, so the route they were produced on
                # has to still be current.
                await require_current_route_identity()
                async with gate.guard(source):
                    with open_memory_catalog_connection(
                        db_path=db_path, busy_timeout_ms=busy_timeout_ms
                    ) as connection:
                        _persist_short_insight(
                            connection=connection,
                            binding=source.binding,
                            expected_cursor=cursor,
                            cursor=zanei.cursor,
                            run_id=run_id,
                            output=output,
                            records=verification.accepted,
                            period_start=period_start,
                            period_end=period_end,
                        )
    except asyncio.CancelledError:
        # `SourceGate.revoke` cancels this task when stored-context access is
        # revoked or the store is replaced. It drops the permit before it
        # schedules that cancel onto this loop, so a permit that is already gone
        # proves the cancel came from invalidation. Fail the job with a normal
        # exception: the worker only finalizes `Exception`, and a job left claimed
        # would block every later short Insight behind its `logical_key`.
        if gate.current(user_id) == source:
            raise
        task = asyncio.current_task()
        assert task is not None  # A coroutine driven by asyncio.run runs in a Task.
        task.uncancel()
        raise SourceInvalidated("context source was invalidated") from None


def _probe_since(
    *, period_start: datetime, previous_period_end: datetime | None
) -> datetime:
    """Start the probe where the last Insight that ran stopped looking.

    The run starts just after its window does, so a probe bounded by the end of
    the previous run's window alone would miss what arrived while that run was
    probing. Overlapping by one window closes that gap; a repeat is harmless
    because the run reads the previous Insight. After idle windows were skipped,
    this reaches back over them, up to `PROBE_MAX_LOOKBACK`.
    """
    last_seen = (
        period_start
        if previous_period_end is None
        else min(previous_period_end, period_start)
    )
    return max(last_seen, period_start - PROBE_MAX_LOOKBACK) - timedelta(
        seconds=SHORT_INSIGHT_WINDOW_SECONDS
    )


def _persist_short_insight(
    *,
    connection: sqlite3.Connection,
    binding: SourceBinding,
    expected_cursor: str | None,
    cursor: str,
    run_id: str,
    output: ShortInsightOutput,
    records: tuple[VerifiedRecord, ...],
    period_start: str,
    period_end: str,
) -> None:
    """Commit the read position and every derived output, or none of them."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with immediate_transaction(connection):
        store.compare_cursor(connection, binding, expected_cursor, cursor)
        connection.execute(
            """INSERT INTO activity_logs(log_id, user_id, period_start, period_end,
               description, status, prompt_name, prompt_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'success', ?, ?, ?, ?)""",
            (
                run_id,
                binding.user_id,
                period_start,
                period_end,
                output.activity,
                InsightAgent.PROMPT_NAME,
                InsightAgent.PROMPT_VERSION,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO agent_insights(insight_id, user_id, status,
               short_term_insight_data, facts, reconsideration_reason, source_cursor,
               prompt_name, prompt_version, created_at, updated_at)
               VALUES (?, ?, 'success', ?, '', ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                binding.user_id,
                output.insight,
                output.reconsideration_reason,
                cursor,
                InsightAgent.PROMPT_NAME,
                InsightAgent.PROMPT_VERSION,
                now,
                now,
            ),
        )
        connection.executemany(
            """INSERT INTO source_records(record_id, user_id, run_id, event_id,
               observed_at, app_name, bundle_id, window_title, source, speaker,
               shown_time, quote, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    _source_record_id(run_id=run_id, record=record),
                    binding.user_id,
                    run_id,
                    record.event_id,
                    record.observed_at,
                    record.app_name,
                    record.bundle_id,
                    record.window_title,
                    record.source,
                    record.speaker,
                    record.shown_time,
                    record.quote,
                    now,
                )
                for record in records
            ),
        )
        register_source_records_memory(
            connection=connection, user_id=binding.user_id, run_id=run_id
        )
        registered: tuple[tuple[MemorySource, str], ...] = (
            ("activity_log", output.activity),
            ("short_term_insight", output.insight),
        )
        for source_type, body in registered:
            register_inline_domain_memory(
                connection=connection,
                user_id=binding.user_id,
                source=source_type,
                source_record_id=run_id,
                content=body,
            )
        insert_pending_memory_agent_trigger(
            connection=connection,
            user_id=binding.user_id,
            trigger_kind=SHORT_INSIGHT_MEMORY_TRIGGER_KIND,
            source_id=run_id,
            created_at=now,
        )


def _source_record_id(*, run_id: str, record: VerifiedRecord) -> str:
    """Derive one id per run and record, so a replayed run appends nothing new.

    JSON, not a joined string: a quote may contain any separator a screen can
    show, and two different records must never share an identity.
    """
    identity = json.dumps(
        [
            run_id,
            record.event_id,
            record.source,
            record.speaker,
            record.shown_time,
            record.quote,
        ],
        ensure_ascii=False,
    )
    return str(uuid.uuid5(NAMESPACE_SOURCE_RECORD, identity))
