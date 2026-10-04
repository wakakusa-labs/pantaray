from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    verify_database_integrity,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)

from .support import _insert_user, _migrations_before

USER_ID = "user-delivery"
STORED_AT = "2026-09-30T00:00:00.000Z"


def _insert(
    connection: sqlite3.Connection,
    suggestion_id: str,
    has_suggestion: int | None,
    delivery_state: str,
) -> None:
    connection.execute(
        "INSERT INTO agent_suggestions(suggestion_id, user_id, status, has_suggestion,"
        " delivery_state, created_at, updated_at) VALUES (?, ?, 'success', ?, ?, ?, ?)",
        (suggestion_id, USER_ID, has_suggestion, delivery_state, STORED_AT, STORED_AT),
    )


@pytest.fixture
def upgraded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path,
        1_000,
        _migrations_before(migrations, "0122_suggestion_delivery_state.sql"),
    )
    with sqlite3.connect(db_path) as connection:
        _insert_user(connection, USER_ID)
        connection.executemany(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status,"
            " has_suggestion, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (suggestion_id, USER_ID, status, has_suggestion, STORED_AT, STORED_AT)
                for suggestion_id, status, has_suggestion in (
                    ("shown", "success", 1),
                    ("nothing", "success", 0),
                    ("running", "processing", None),
                )
            ],
        )
    apply_migrations(db_path, 1_000, migrations)
    verify_database_integrity(db_path, 1_000)
    return db_path


def test_upgrade_marks_every_stored_suggestion_as_shown(upgraded_db: Path) -> None:
    with sqlite3.connect(upgraded_db) as connection:
        rows = connection.execute(
            "SELECT suggestion_id, delivery_state, updated_at FROM agent_suggestions"
            " ORDER BY suggestion_id"
        ).fetchall()

    # History orders by updated_at, so the backfill must not move any row.
    assert rows == [
        ("nothing", None, STORED_AT),
        ("running", None, STORED_AT),
        ("shown", "released", STORED_AT),
    ]


@pytest.mark.parametrize(
    ("has_suggestion", "delivery_state"),
    [(0, "held"), (None, "released"), (1, "pending")],
)
def test_only_a_stored_suggestion_carries_a_known_delivery_state(
    upgraded_db: Path, has_suggestion: int | None, delivery_state: str
) -> None:
    with sqlite3.connect(upgraded_db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert(connection, "bad", has_suggestion, delivery_state)


def test_an_owner_holds_at_most_one_suggestion(upgraded_db: Path) -> None:
    with sqlite3.connect(upgraded_db) as connection:
        _insert(connection, "held-1", 1, "held")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            _insert(connection, "held-2", 1, "held")
