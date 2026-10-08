from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.context_search import (
    search_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.repair_projection import (
    write_inline_repair_projection,
)
from pantaray_agents.local_runtime.memory_catalog.repository import load_revision
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
    verify_database_integrity,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.suggestion_research.runtime import (
    LocalSuggestionResearchTools,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.snapshot import (
    SuggestionResearchSnapshot,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

from .migrated_db import prepare_test_database

OBSERVED = "2026-09-07T00:01:00Z"
QUOTE = "見積資料を共有します\n## 別案件ではない\n[[ref:screen_text]]"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        path, 1000, tuple(m for m in load_default_migrations() if m.version < 118)
    )
    with open_memory_catalog_connection(db_path=path, busy_timeout_ms=1000) as conn:
        with immediate_transaction(conn):
            for user in ("user-1", "other"):
                conn.execute(
                    "INSERT INTO users VALUES (?, 'ja', NULL, ?, ?)",
                    (user, OBSERVED, OBSERVED),
                )
                run_id = f"run-{user}"
                conn.execute(
                    """INSERT INTO activity_logs(log_id, user_id, period_start, period_end,
                    description, status, prompt_name, prompt_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '既存の活動', 'success', 'insight', '2.3', ?, ?)""",
                    (run_id, user, OBSERVED, OBSERVED, OBSERVED, OBSERVED),
                )
                register_inline_domain_memory(
                    connection=conn,
                    user_id=user,
                    source="activity_log",
                    source_record_id=run_id,
                    content="既存の活動",
                )
                for number, source, quote in (
                    (1, "資料共有", QUOTE),
                    (2, "設計相談", "画面の色を検討します"),
                ):
                    conn.execute(
                        """INSERT INTO source_records(record_id,user_id,run_id,event_id,
                        observed_at,source,speaker,shown_time,quote,created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"record-{user}-{number}",
                            user,
                            run_id,
                            f"event-{number}",
                            OBSERVED,
                            source,
                            "発言者" if number == 1 else "",
                            "09:01" if number == 1 else "",
                            quote,
                            OBSERVED,
                        ),
                    )
    return path


def _snapshot(path: Path) -> tuple[list[tuple], list[tuple]]:
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("SELECT * FROM source_records ORDER BY record_id").fetchall(),
            conn.execute("SELECT * FROM memory_nodes ORDER BY node_id").fetchall(),
        )


def test_upgrade_preserves_evidence_and_existing_catalog_and_replay(
    db_path: Path,
) -> None:
    records, existing_nodes = _snapshot(db_path)
    prepare_test_database(db_path, 1000, load_default_migrations())
    verify_database_integrity(db_path, 1000)
    after_records, after_nodes = _snapshot(db_path)
    assert after_records == records
    assert all(node in after_nodes for node in existing_nodes)
    assert len(after_nodes) == len(existing_nodes) + 2
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1000) as conn:
        nodes = conn.execute(
            "SELECT * FROM memory_nodes WHERE source_type='source_records'"
        ).fetchall()
        for node in nodes:
            revision = load_revision(
                connection=conn,
                user_id=node["user_id"],
                revision_id=node["current_revision_id"],
            )
            assert revision is not None
            assert node["updated_at"] == OBSERVED
            write_inline_repair_projection(
                connection=conn,
                source="source_records",
                source_record_id=node["source_record_id"],
                revision=revision,
            )
        assert conn.execute("SELECT count(*) FROM memory_links").fetchone()[0] == 0
        # Publication also supplies fragments for the existing embedding worker.
        assert (
            conn.execute(
                "SELECT count(*) FROM memory_fragments WHERE content_text LIKE '%見積資料%'"
            ).fetchone()[0]
            >= 2
        )
    assert _snapshot(db_path)[0] == records
    prepare_test_database(db_path, 1000, load_default_migrations())
    assert _snapshot(db_path) == (after_records, after_nodes)


@pytest.mark.parametrize("focus", ["all", "activity", "stable_knowledge"])
@pytest.mark.parametrize(
    "query, heading, number", [("見積資料", "資料共有", 1), ("画面の色", "設計相談", 2)]
)
def test_search_keeps_source_boundary_and_observation_time(
    db_path: Path, focus: str, query: str, heading: str, number: int
) -> None:
    from pantaray_agents.local_runtime.memory_catalog.search_policy import (
        parse_memory_search_focus,
    )

    prepare_test_database(db_path, 1000, load_default_migrations())
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1000) as conn:
        results, epoch = search_memory_catalog(
            connection=conn,
            user_id="user-1",
            run_id="search",
            query=query,
            query_embedding=None,
            embedding_generation=None,
            focus=parse_memory_search_focus(focus),
            center_time=None,
            radius_hours=None,
            limit=8,
        )
    if focus == "stable_knowledge":
        assert results == []
        return
    assert len(results) == 1
    result = results[0]
    assert result["source"] == "source_records"
    assert result["record_id"] == "run-user-1"
    assert result["heading_path"] == heading
    assert result["observed_at"] == OBSERVED
    assert f"event-{number}" in result["content"]
    assert f"record-user-1-{number}" in result["content"]
    assert "record-other" not in result["content"]
    if number == 2:
        assert "Speaker:" not in result["content"]
        assert "Shown time:" not in result["content"]
        assert "見積資料" not in result["content"]
    assert epoch.items[0].item.source == "source_records"


async def test_suggestion_memory_search_returns_only_the_users_evidence(
    db_path: Path,
) -> None:
    prepare_test_database(db_path, 1000, load_default_migrations())
    definitions = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=1000,
        snapshot=SuggestionResearchSnapshot(
            roots=(),
            stable_memory=SuggestionStableMemoryContext("", False, False),
            commands_allowed=False,
            read_access_scope="workspace",
        ),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="search")
    tools = {definition.name: definition for definition in definitions}
    searched = await tools["memory_search"].execute(
        _call("memory_search", {"query": "見積資料", "focus": "all", "limit": 8}),
        1,
    )
    assert searched.status == "success"
    result = searched.output["results"][0]
    assert "見積資料" in result["content"]
    assert "record-other" not in result["content"]


def _call(name: str, args: dict[str, JSONValue]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=name,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=name, reason=None, args=args),
    )
