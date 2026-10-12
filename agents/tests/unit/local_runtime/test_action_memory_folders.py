from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.workspace_mount_catalog import (  # noqa: E501
    load_required_local_workspace_manifest_catalog,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
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
    # Approval is not what stops a write below: the folders are read-only.
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    facts = _publish(
        db_path=db_path,
        artifact_root=artifact_root,
        source="fact",
        folder="facts",
        files={
            "index.md": "# Facts\n- [Pantaray](wakakusa/pantaray.md)\n",
            "wakakusa/pantaray.md": LEAF,
        },
    )
    insights = _publish(
        db_path=db_path,
        artifact_root=artifact_root,
        source="long_term_insight",
        folder="insights",
        files={"index.md": "# Insights\n"},
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=NOW,
        allowed_tool_ids=("read", "apply_patch"),
    )

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
    leaf = facts / "wakakusa" / "pantaray.md"
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
    assert leaf.read_text(encoding="utf-8") == LEAF


def _publish(
    *,
    db_path: Path,
    artifact_root: Path,
    source: str,
    folder: str,
    files: dict[str, str],
) -> Path:
    revision_path = f"memory/{source}-revision-1"
    root = artifact_root / revision_path / folder
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO memory_nodes(
                    user_id, node_id, source_type, source_record_id, lifecycle,
                    integrity, current_revision_id, created_at, updated_at
                ) VALUES ('user-1', ?, ?, 'record-1', 'preparing', 'healthy',
                          NULL, ?, ?)
                """,
                (f"{source}-node", source, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO memory_revisions(
                    user_id, revision_id, node_id, body_kind, inline_body,
                    artifact_root_path, fragment_schema_version,
                    content_sha256, created_at
                ) VALUES ('user-1', ?, ?, 'artifact_tree', NULL, ?, 1, ?, ?)
                """,
                (f"{source}-revision-1", f"{source}-node", revision_path, source, NOW),
            )
            connection.execute(
                """
                UPDATE memory_nodes SET lifecycle = 'active', current_revision_id = ?
                WHERE user_id = 'user-1' AND node_id = ?
                """,
                (f"{source}-revision-1", f"{source}-node"),
            )
    return root.resolve()
