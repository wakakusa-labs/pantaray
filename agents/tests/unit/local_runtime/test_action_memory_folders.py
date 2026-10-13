"""An Action reads its memory indexes and the folders they link into at one revision.

A subagent's broker calls resolve to the Action's own manifest and execution
session (``load_action_subagent_broker_authority``) and render the Action's
frozen head, so what these tests read through the Action is what its
subagents read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.initial import (
    ensure_memory_indexes,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.workspace_mount_catalog import (  # noqa: E501
    load_required_local_workspace_manifest_catalog,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    update_read_access_scope,
)
from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_WORKSPACE
from pantaray_agents.tools.contract import BrokerPolicyError

from .action_seed import insert_agent_action
from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _grant_workspace_full_access,
    _seed_broker_actor_process,
)
from .migrated_db import prepare_test_database
from .read_tool_broker_support import execute_read_tool
from .test_memory_indexes import _publish_head

NOW = "2026-10-13T00:00:00Z"
# Longer than one memory_search passage, so a whole read is told apart from one.
LEAF = "# Pantaray\n\n" + "\n\n".join(
    f"- Detail {index}: the paragraph a search would return alone."
    for index in range(40)
)


@pytest.mark.asyncio
async def test_action_reads_current_facts_and_insights_whole_but_cannot_change_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root, context = _bootstrap(tmp_path, monkeypatch)
    # Approval is not what stops a write below: the folders are read-only.
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    _publish_facts(db_path, artifact_root, index_link="wakakusa/pantaray.md")
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id="user-1",
        source="long_term_insight",
        intent_kind="long_term_insight",
        source_record_id="user-1",
        documents=(MemoryDocument("insights/index.md", "# Insights\n"),),
    )
    ensure_memory_indexes(_state(context), user_id="user-1")
    facts = _memory_root(db_path, context, "fact")
    insights = _memory_root(db_path, context, "long_term_insight")

    catalog = load_required_local_workspace_manifest_catalog(
        user_id="user-1",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    ).root_catalog.splitlines()
    read = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": str(facts / "wakakusa" / "pantaray.md")},
    )
    with pytest.raises(BrokerPolicyError) as refused:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="patch-1",
            tool_request_id="request-patch-1",
            args={
                "changes": [
                    {
                        "op": "add",
                        "path": str(facts / "new.md"),
                        "new_lines": ["changed"],
                        "trailing_newline": True,
                    }
                ]
            },
        )

    assert (
        f"- `{facts}`: the user's facts, read-only; `facts/` in Memory Indexes "
        "is this folder"
    ) in catalog
    assert (
        f"- `{insights}`: the user's insights, read-only; `insights/` in Memory "
        "Indexes is this folder"
    ) in catalog
    assert read.output["content"].rstrip("\n").endswith(LEAF.splitlines()[-1])
    assert read.output["next_offset"] is None
    assert refused.value.code == "WRITE_PATH_DENIED"
    assert not (facts / "new.md").exists()


def test_indexes_and_folders_stay_on_one_revision_across_a_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root, context = _bootstrap(
        tmp_path, monkeypatch, facts_link="first.md"
    )
    state = _state(context)

    # Published after the manifest is made and before the run reads its indexes.
    _publish_facts(db_path, artifact_root, index_link="second.md")
    ensure_memory_indexes(state, user_id="user-1")
    first_entry = state["context"]["memory_indexes"]
    # Published while the Action lasts: a later run entry keeps the revision.
    _publish_facts(db_path, artifact_root, index_link="third.md")
    ensure_memory_indexes(state, user_id="user-1")
    facts = _memory_root(db_path, context, "fact")

    assert "second.md" in first_entry
    assert state["context"]["memory_indexes"] == first_entry
    assert (facts / "index.md").read_text(encoding="utf-8").strip() in first_entry
    assert (facts / "second.md").is_file()


@pytest.mark.asyncio
async def test_an_action_from_before_the_folders_gets_them_with_matching_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root, context = _bootstrap(tmp_path, monkeypatch)
    _publish_facts(db_path, artifact_root, index_link="wakakusa/pantaray.md")
    # The manifest and head an Action left before facts had a folder.
    state = _state(context)
    state["context"]["memory_indexes"] = "frozen before the folders"
    state["context"]["executing_head_fields"] = {
        "memory_indexes": "frozen before the folders"
    }

    ensure_memory_indexes(state, user_id="user-1")
    facts = _memory_root(db_path, context, "fact")
    read = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": str(facts / "wakakusa" / "pantaray.md")},
    )

    indexes = state["context"]["memory_indexes"]
    assert (facts / "index.md").read_text(encoding="utf-8").strip() in indexes
    assert state["context"]["executing_head_fields"]["memory_indexes"] == indexes
    assert read.output["content"].rstrip("\n").endswith(LEAF.splitlines()[-1])


def _bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, facts_link: str | None = None
) -> tuple[Path, Path, ActionExecutionContext]:
    artifact_root = tmp_path / "artifacts"
    db_path = tmp_path / "app-data" / "runtime.db"
    db_path.parent.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
        now=NOW,
    )
    if facts_link is not None:
        _publish_facts(db_path, artifact_root, index_link=facts_link)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=NOW,
        allowed_tool_ids=("read", "apply_patch"),
    )
    return db_path, artifact_root, context


def _state(context: ActionExecutionContext) -> ActionAgentState:
    return create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at=NOW,
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        execution_network_policy=context.network_policy,
        action_temp_dir=str(context.action_temp_dir),
        app_runtime_python=str(context.app_runtime_python),
        read_access_scope=context.read_access_scope,
    )


def _publish_facts(db_path: Path, artifact_root: Path, *, index_link: str) -> None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT current_revision_id FROM memory_nodes WHERE source_type = 'fact'"
        ).fetchone()
    _publish_head(
        db_path=db_path,
        artifact_root=artifact_root,
        user_id="user-1",
        source="fact",
        intent_kind="fact",
        source_record_id="fact-1",
        documents=(
            MemoryDocument(
                "facts/index.md",
                f"# Facts\n\n## Overview\nBuilds Pantaray.\n\n- [Detail]({index_link})\n",
            ),
            MemoryDocument(f"facts/{index_link}", LEAF),
        ),
        base_revision_id=None if row is None else row[0],
    )


def _memory_root(db_path: Path, context: ActionExecutionContext, source: str) -> Path:
    with sqlite3.connect(db_path) as connection:
        (path,) = connection.execute(
            """
            SELECT real_path FROM workspace_manifest_roots
            WHERE manifest_id = ? AND source_type = ?
              AND can_read = 1 AND can_apply_patch = 0
              AND can_process_read = 0 AND can_process_write = 0
            """,
            (context.manifest_id, source),
        ).fetchone()
    return Path(path)
