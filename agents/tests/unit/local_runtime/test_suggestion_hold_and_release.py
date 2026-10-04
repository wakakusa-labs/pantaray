"""A stored Suggestion is held, then released (shown) or ended unshown."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.suggestion_release import (
    SUGGESTION_HOLD_LIMIT,
    release_held_suggestion,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import parse_utc_iso
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import SuggestionAgentResponse
from pantaray_agents.schema.context_source import SourceBinding, SourceReady

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'en', '2026-09-30T00:00:00Z', '2026-09-30T00:00:00Z')",
            (USER_ID,),
        )
    return path


async def _store(
    db_path: Path, suggestion_id: str, *, has_suggestion: bool = True
) -> None:
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        activity_repository=MagicMock(),
    )
    await repository.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=suggestion_id
    )
    saved = await repository.save_suggestion(
        SuggestionAgentResponse(
            suggestion_id=suggestion_id,
            user_id=USER_ID,
            created_at="2026-09-30T00:00:00Z",
            answer=f"Answer of {suggestion_id}" if has_suggestion else "",
            status=StatusType.SUCCESS,
            has_suggestion=has_suggestion,
            interaction_contract="message_only" if has_suggestion else None,
        ),
        prompt_name="suggestion",
        prompt_version="1",
    )
    assert saved.error is None


def _states(db_path: Path) -> dict[str, str | None]:
    with sqlite3.connect(db_path) as connection:
        return dict(
            connection.execute(
                "SELECT suggestion_id, delivery_state FROM agent_suggestions"
            ).fetchall()
        )


def _held_since(db_path: Path, suggestion_id: str) -> datetime:
    with sqlite3.connect(db_path) as connection:
        (updated_at,) = connection.execute(
            "SELECT updated_at FROM agent_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ).fetchone()
    return parse_utc_iso(updated_at)


def _remembered(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT source_record_id FROM memory_nodes"
                " WHERE source_type = 'suggestion'"
            )
        }


@pytest.mark.asyncio
async def test_a_new_suggestion_is_held_and_replaces_the_one_held_before(
    db_path: Path,
) -> None:
    await _store(db_path, "first")
    await _store(db_path, "second")

    assert _states(db_path) == {"first": "superseded", "second": "held"}
    # Neither was shown, so neither is something Pantaray said.
    assert _remembered(db_path) == set()


@pytest.mark.asyncio
async def test_a_run_without_a_suggestion_keeps_the_held_one(db_path: Path) -> None:
    await _store(db_path, "held")
    await _store(db_path, "nothing", has_suggestion=False)

    assert _states(db_path) == {"held": "held", "nothing": None}


@pytest.mark.asyncio
async def test_release_shows_the_held_suggestion_and_remembers_it(
    db_path: Path,
) -> None:
    await _store(db_path, "superseded")
    await _store(db_path, "held")
    released_at = _held_since(db_path, "held") + timedelta(seconds=30)

    outcome = release_held_suggestion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        now=released_at,
        session_can_show=True,
    )

    assert outcome == "released"
    assert _states(db_path) == {"superseded": "superseded", "held": "released"}
    assert _remembered(db_path) == {"held"}
    # History orders by updated_at, so a released Suggestion leads it.
    assert _held_since(db_path, "held") == released_at
    assert (
        release_held_suggestion(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id=USER_ID,
            now=released_at,
            session_can_show=True,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waited", "outcome"),
    [
        (SUGGESTION_HOLD_LIMIT - timedelta(milliseconds=1), "released"),
        (SUGGESTION_HOLD_LIMIT, "expired"),
    ],
)
async def test_a_suggestion_held_for_the_limit_expires_unremembered(
    db_path: Path, waited: timedelta, outcome: str
) -> None:
    await _store(db_path, "held")

    result = release_held_suggestion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        now=_held_since(db_path, "held") + waited,
        session_can_show=True,
    )

    assert result == outcome
    assert _states(db_path) == {"held": outcome}
    assert _remembered(db_path) == ({"held"} if outcome == "released" else set())


@pytest.mark.asyncio
async def test_turning_recording_off_expires_the_held_suggestion(
    db_path: Path,
) -> None:
    await _store(db_path, "held")
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            store.compare_source(
                connection,
                USER_ID,
                None,
                SourceReady(
                    kind="ready",
                    binding=SourceBinding(
                        user_id=USER_ID,
                        epoch=UUID(int=1),
                        policy_revision="policy-1",
                        store_id="store-1",
                        protocol_version=1,
                    ),
                    capture_paused=True,
                ),
            )

    result = release_held_suggestion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        now=datetime.now(UTC),
        session_can_show=True,
    )

    assert result == "expired"
    assert _remembered(db_path) == set()


@pytest.mark.asyncio
async def test_the_next_run_sees_the_held_suggestion_but_not_unshown_ones(
    db_path: Path,
) -> None:
    from pantaray_agents.agents.suggestion_agent.context_formatters import (
        format_recent_suggestions,
        normalize_recent_suggestion_entry,
    )

    await _store(db_path, "expired")
    release_held_suggestion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        now=_held_since(db_path, "expired") + SUGGESTION_HOLD_LIMIT,
        session_can_show=True,
    )
    await _store(db_path, "shown")
    release_held_suggestion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        now=_held_since(db_path, "shown"),
        session_can_show=True,
    )
    await _store(db_path, "superseded")
    await _store(db_path, "held")
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        activity_repository=MagicMock(),
    )

    recent = await repository.get_recent_suggestions(USER_ID, days=30, limit=5)
    assert recent.data is not None
    entries = [normalize_recent_suggestion_entry(row) for row in recent.data]

    assert [(entry.answer, entry.held) for entry in entries if entry] == [
        ("Answer of held", True),
        ("Answer of shown", False),
    ]
    rendered = format_recent_suggestions([entry for entry in entries if entry])
    assert rendered.count("Delivery: generated, not shown yet") == 1
    assert rendered.index("Answer of held") < rendered.index("not shown yet")
    assert rendered.index("not shown yet") < rendered.index("Answer of shown")
