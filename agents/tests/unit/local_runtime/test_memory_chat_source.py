"""The single chat as a source of the unified Memory run."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.hooks.events import (
    ChatMessageRecorded,
    InsightRecorded,
)
from pantaray_agents.local_runtime.hooks.inbox import record_event
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_memory_update_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.memory_chat_range import (
    MEMORY_CHAT_MAX_CHARS,
)
from pantaray_agents.local_runtime.runtime.session_store import import_desktop_session
from pantaray_agents.local_runtime.storage.chat_messages import (
    CHAT_MESSAGE_KINDS,
    read_chat_messages,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.tasks.memory_update_evidence import render_chat_messages

from .memory_update_hook import Tick, finish_memory_jobs, memory_payloads, record, tick
from .migrated_db import prepare_test_database

__all__ = ["tick"]

NOW = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )
    return db_path


def _append(db_path: Path, content: dict[str, object], *, minutes_ago: int) -> int:
    created_at = (NOW - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO chat_items(
                item_id, user_id, message_id, kind, quote_item_id, payload, created_at
            ) VALUES (?, 'user-1', ?, ?, ?, ?, ?)
            """,
            (
                f"item-{uuid.uuid4().hex[:8]}",
                uuid.uuid4().hex,
                content["kind"],
                content.get("quote_item_id"),
                json.dumps(content),
                created_at,
            ),
        )
        sequence = int(cursor.lastrowid or 0)
        if content["kind"] in CHAT_MESSAGE_KINDS:
            record_event(
                connection,
                user_id="user-1",
                event=ChatMessageRecorded(sequence=sequence),
                occurred_at=created_at,
            )
    return sequence


def _user(text: str, *, minutes_ago: int, db_path: Path) -> int:
    return _append(
        db_path,
        {
            "kind": "user_message",
            "text": text,
            "quote_item_id": None,
            "images": [],
            "files": [],
        },
        minutes_ago=minutes_ago,
    )


def _reply(text: str, *, minutes_ago: int, db_path: Path) -> int:
    return _append(
        db_path,
        {"kind": "assistant_message", "text": text, "quote_item_id": None, "cards": []},
        minutes_ago=minutes_ago,
    )


def _failure(*, minutes_ago: int, db_path: Path) -> int:
    return _append(
        db_path, {"kind": "turn_failure", "reason": "internal"}, minutes_ago=minutes_ago
    )


def _chat_ranges(db_path: Path) -> list[dict[str, int] | None]:
    return [dict(p["chat"]) if "chat" in p else None for p in memory_payloads(db_path)]


def test_recent_chat_alone_waits_for_the_insight_window(
    tmp_path: Path, tick: Tick
) -> None:
    db_path = _db(tmp_path)
    _user("I prefer short replies in Japanese.", minutes_ago=5, db_path=db_path)

    tick(db_path, NOW)

    assert _chat_ranges(db_path) == []


def test_chat_older_than_the_window_starts_a_run_of_its_own(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches a turn failure, which is no message, starting or holding a run.
    db_path = _db(tmp_path)
    _user("I prefer short replies.", minutes_ago=20, db_path=db_path)
    _failure(minutes_ago=19, db_path=db_path)
    last = _reply("Understood.", minutes_ago=19, db_path=db_path)

    tick(db_path, NOW)
    finish_memory_jobs(db_path)
    tick(db_path, NOW)

    assert _chat_ranges(db_path) == [{"after_sequence": 0, "through_sequence": last}]


def test_chat_rides_along_with_an_insight_and_the_next_range_starts_after_it(
    tmp_path: Path, tick: Tick
) -> None:
    db_path = _db(tmp_path)
    first = _user("We decided to ship 0.4.0 on Friday.", minutes_ago=1, db_path=db_path)
    record(
        db_path,
        InsightRecorded(insight_id="insight-1"),
        at=NOW.isoformat().replace("+00:00", "Z"),
    )

    tick(db_path, NOW)
    finish_memory_jobs(db_path)
    later = _user("And the notes go out with it.", minutes_ago=-30, db_path=db_path)
    tick(db_path, NOW + timedelta(hours=1))

    assert _chat_ranges(db_path) == [
        {"after_sequence": 0, "through_sequence": first},
        {"after_sequence": first, "through_sequence": later},
    ]


def test_one_run_takes_the_chat_up_to_its_character_bound(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches the messages past the bound consumed with the range, which would
    # lose them, or handed again in the next range.
    db_path = _db(tmp_path)
    half = "x" * (MEMORY_CHAT_MAX_CHARS // 2)
    _user(half, minutes_ago=30, db_path=db_path)
    second = _reply(half, minutes_ago=29, db_path=db_path)
    third = _user(half, minutes_ago=28, db_path=db_path)

    tick(db_path, NOW)
    finish_memory_jobs(db_path)
    tick(db_path, NOW)

    assert _chat_ranges(db_path) == [
        {"after_sequence": 0, "through_sequence": second},
        {"after_sequence": second, "through_sequence": third},
    ]


def test_a_run_reads_and_names_only_the_messages_of_its_range(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    before = _user("earlier", minutes_ago=30, db_path=db_path)
    asked = _user("Call me Kento, not Shimizu-san.", minutes_ago=20, db_path=db_path)
    _failure(minutes_ago=19, db_path=db_path)
    answered = _reply("Got it, Kento.", minutes_ago=18, db_path=db_path)
    _user("later", minutes_ago=10, db_path=db_path)

    with sqlite3.connect(db_path) as connection:
        messages = read_chat_messages(
            connection, user_id="user-1", after=before, through=answered
        )
    rendered = render_chat_messages(messages)

    assert [message.sequence for message in messages] == [asked, answered]
    assert f"### chat item {messages[0].item_id}, user (" in rendered
    assert "Call me Kento, not Shimizu-san." in rendered
    assert f"### chat item {messages[1].item_id}, Pantaray (" in rendered
    assert "earlier" not in rendered and "later" not in rendered


def test_a_payload_may_carry_the_chat_as_its_only_source() -> None:
    payload = {
        "job_id": "job-1",
        "process_id": "process-1",
        "user_id": "user-1",
        "enqueued_at": "2026-10-09T03:00:00Z",
        "short_insight_ids": [],
        "summary_ids": [],
        "action_terminals": [],
        "chat": {"after_sequence": 4, "through_sequence": 9},
    }

    parsed = parse_memory_update_job_payload_json(json.dumps(payload))

    assert parsed["chat"] == {"after_sequence": 4, "through_sequence": 9}
    with pytest.raises(MigrationError):
        parse_memory_update_job_payload_json(
            json.dumps(
                {**payload, "chat": {"after_sequence": 9, "through_sequence": 9}}
            )
        )
