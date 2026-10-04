from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
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
    build_action_repository,
)


def _register_suggestion(db_path: Path) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with immediate_transaction(connection):
            register_inline_domain_memory(
                connection=connection,
                user_id=USER_ID,
                source="suggestion",
                source_record_id="suggestion-1",
                content="Accepted suggestion",
            )


def _publish_head(
    *,
    db_path: Path,
    artifact_root: Path,
    user_id: str = USER_ID,
    source: MemorySource,
    intent_kind: MemoryIntentKind,
    source_record_id: str,
    entry_path: str,
    profile_brief: str | None,
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
                        base_revision_id=None,
                        documents=(MemoryDocument(entry_path, f"# {source}\n"),),
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
            revision = activate_artifact_revision(
                connection=connection, prepared=prepared
            )
            if profile_brief is not None:
                # The unified Memory agent writes the brief onto the revision; the
                # legacy run tables stay empty here on purpose.
                connection.execute(
                    """
                    UPDATE memory_revisions SET profile_brief = ?
                    WHERE user_id = ? AND revision_id = ?
                    """,
                    (profile_brief, user_id, revision.revision_id),
                )


def _publish_all_heads(
    *, db_path: Path, artifact_root: Path, user_id: str = USER_ID
) -> None:
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id=user_id,
        source="long_term_insight",
        intent_kind="long_term_insight",
        source_record_id=user_id,
        entry_path="insights/index.md",
        profile_brief="INSIGHT-BRIEF",
    )
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id=user_id,
        source="fact",
        intent_kind="fact",
        source_record_id="fact-1",
        entry_path="facts/index.md",
        profile_brief="FACTS-BRIEF",
    )
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id=user_id,
        source="agent_experience",
        intent_kind="agent_experience",
        source_record_id=user_id,
        entry_path="agent_experience/index.md",
        profile_brief=None,
    )


def _revision_root_path(db_path: Path, revision_id: str) -> str:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT artifact_root_path FROM memory_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_legacy_tables_empty(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        for table in (
            "agent_insight_update_runs",
            "agent_fact_structuring_runs",
            "agent_facts",
        ):
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )


@pytest.mark.asyncio
async def test_initial_memory_context_resolves_three_catalog_heads(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    _register_suggestion(db_path)
    _publish_all_heads(db_path=db_path, artifact_root=tmp_path / "artifacts")
    _assert_legacy_tables_empty(db_path)

    result = await build_action_repository(db_path).get_initial_memory_context(
        USER_ID,
        action_id="action-1",
        suggestion_id="suggestion-1",
    )

    assert result.data is not None
    assert result.data.insight is not None
    assert result.data.insight.insight_id == USER_ID
    assert result.data.insight.insight_profile_brief == "INSIGHT-BRIEF"
    assert result.data.facts is not None
    assert result.data.facts.fact_id == "fact-1"
    assert result.data.facts.facts_profile_brief == "FACTS-BRIEF"
    assert [artifact.source_type for artifact in result.data.artifacts] == [
        "long_term_insight",
        "facts",
        "agent_experience",
    ]
    for artifact, entry in zip(
        result.data.artifacts,
        ("insights/index.md", "facts/index.md", "agent_experience/index.md"),
        strict=True,
    ):
        assert [file.storage_path for file in artifact.files] == [
            f"{_revision_root_path(db_path, artifact.artifact_id)}/{entry}"
        ]
    assert result.data.context_epoch is not None
    assert {item.source for item in result.data.context_epoch.items} == {
        "suggestion",
        "long_term_insight",
        "fact",
        "agent_experience",
    }


@pytest.mark.asyncio
async def test_initial_memory_context_skips_categories_without_a_head(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    _register_suggestion(db_path)
    _publish_head(
        db_path=db_path,
        artifact_root=tmp_path / "artifacts",
        source="fact",
        intent_kind="fact",
        source_record_id="fact-1",
        entry_path="facts/index.md",
        profile_brief="FACTS-BRIEF",
    )

    result = await build_action_repository(db_path).get_initial_memory_context(
        USER_ID,
        action_id="action-1",
        suggestion_id="suggestion-1",
    )

    assert result.data is not None
    assert result.data.insight is None
    assert result.data.facts is not None
    assert [artifact.source_type for artifact in result.data.artifacts] == ["facts"]


@pytest.mark.asyncio
async def test_initial_memory_context_ignores_other_users_heads(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    _register_suggestion(db_path)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-2', 'ja', '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z')
            """
        )
    _publish_all_heads(
        db_path=db_path, artifact_root=tmp_path / "artifacts", user_id="user-2"
    )

    result = await build_action_repository(db_path).get_initial_memory_context(
        USER_ID,
        action_id="action-1",
        suggestion_id="suggestion-1",
    )

    assert result.data is not None
    assert result.data.insight is None
    assert result.data.facts is None
    assert result.data.artifacts == ()
