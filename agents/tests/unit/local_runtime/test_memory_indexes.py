from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.memory_indexes import (
    load_memory_heads,
    render_memory_indexes,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryIntentKind,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.publication import (
    MemoryPublicationRequest,
    activate_artifact_revision,
    create_artifact_revision_intent,
    materialize_artifact_revision,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .local_action_repository_support import (
    USER_ID,
    bootstrap_action_repository_db,
)


def _publish_head(
    *,
    db_path: Path,
    artifact_root: Path,
    user_id: str = USER_ID,
    source: MemorySource,
    intent_kind: MemoryIntentKind,
    source_record_id: str,
    documents: tuple[MemoryDocument, ...],
    base_revision_id: str | None = None,
) -> None:
    artifact_root.mkdir(exist_ok=True)
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with immediate_transaction(connection):
            node = ensure_preparing_node(
                connection=connection,
                user_id=user_id,
                source=source,
                source_record_id=source_record_id,
            )
            prepared = create_artifact_revision_intent(
                connection=connection,
                request=MemoryPublicationRequest(
                    source=source,
                    source_record_id=source_record_id,
                    draft=create_memory_draft(
                        user_id=user_id,
                        owner_node_id=node.node_id,
                        base_revision_id=base_revision_id,
                        documents=documents,
                    ),
                    body_kind="artifact_tree",
                    intent_kind=intent_kind,
                    domain_payload_json="{}",
                ),
            )
    materialize_artifact_revision(artifact_root=artifact_root, prepared=prepared)
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with immediate_transaction(connection):
            activate_artifact_revision(connection=connection, prepared=prepared)


def _publish_indexes(
    *, db_path: Path, artifact_root: Path, user_id: str = USER_ID
) -> None:
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id=user_id,
        source="fact",
        intent_kind="fact",
        source_record_id="fact-1",
        documents=(
            MemoryDocument(
                "facts/index.md",
                "# Facts\n\n## Overview\nBuilds Pantaray.\n\n"
                "## Wakakusa\n- [Pantaray](pantaray.md)\n",
            ),
            MemoryDocument("facts/pantaray.md", "# Pantaray\nLeaf detail\n"),
        ),
    )
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id=user_id,
        source="long_term_insight",
        intent_kind="long_term_insight",
        source_record_id=user_id,
        documents=(MemoryDocument("insights/index.md", "# Insights\n- Topic\n"),),
    )


def _render(db_path: Path, artifact_root: Path) -> str:
    return render_memory_indexes(
        load_memory_heads(
            db_path=db_path,
            busy_timeout_ms=1_000,
            artifact_root=artifact_root,
            user_id=USER_ID,
        )
    )


def test_memory_indexes_show_each_index_and_say_what_is_missing(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    _publish_indexes(db_path=db_path, artifact_root=tmp_path / "artifacts")

    assert _render(db_path, tmp_path / "artifacts") == (
        '<file path="facts/index.md">\n# Facts\n\n## Overview\nBuilds Pantaray.\n\n'
        "## Wakakusa\n- [Pantaray](pantaray.md)\n</file>\n\n"
        "insights/index.md has no overview yet.\n"
        '<file path="insights/index.md">\n# Insights\n- Topic\n</file>\n\n'
        "Lessons from past work: none yet."
    )


def test_memory_indexes_ignore_other_users_heads(tmp_path: Path) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-2', 'ja', '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z')
            """
        )
    _publish_indexes(
        db_path=db_path, artifact_root=tmp_path / "artifacts", user_id="user-2"
    )

    assert _render(db_path, tmp_path / "artifacts") == (
        "facts/index.md: none yet.\n\ninsights/index.md: none yet.\n\n"
        "Lessons from past work: none yet."
    )


def test_a_missing_memory_file_is_reported_and_queued_for_repair(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    artifact_root = tmp_path / "artifacts"
    _publish_indexes(db_path=db_path, artifact_root=artifact_root)
    (leaf,) = artifact_root.rglob("facts/pantaray.md")
    leaf.unlink()

    memory = load_memory_heads(
        db_path=db_path,
        busy_timeout_ms=1_000,
        artifact_root=artifact_root,
        user_id=USER_ID,
    )

    # The other indexes still reach the run, and the gap is said.
    assert memory.unreadable == ("fact",)
    rendered = render_memory_indexes(memory)
    assert rendered.startswith("facts/index.md: could not be read.\n\n")
    assert '<file path="insights/index.md">' in rendered
    with sqlite3.connect(db_path) as connection:
        queued = connection.execute(
            "SELECT reason FROM memory_repair_queue WHERE user_id = ?", (USER_ID,)
        ).fetchall()
    assert queued == [("revision_integrity",)]
