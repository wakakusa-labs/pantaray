from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
)
from pantaray_agents.tools.contract import BrokerPolicyError

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database
from .read_tool_broker_support import (
    bootstrap_read_runtime_db,
    bootstrap_read_runtime_db_with_registered_folder,
    execute_read_tool,
)


@pytest.mark.asyncio
async def test_read_directory_entries(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "src").mkdir()
    (context.workspace_path / "README.md").write_text("# Notes\n", encoding="utf-8")

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "."},
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "directory"
    assert {"name": "README.md", "kind": "file"} in outcome.output["entries"]
    assert {"name": "src", "kind": "directory"} in outcome.output["entries"]
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert "README.md" in (outcome.search_text or "")
    assert "src/" in (outcome.search_text or "")


@pytest.mark.asyncio
async def test_read_skips_symlink_loops_in_directory_and_missing_suggestions(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_read_runtime_db(
        tmp_path,
        read_access_scope="full_access",
    )
    neighbor = context.workspace_path / "visible.md"
    neighbor.write_text("visible\n", encoding="utf-8")
    loop = context.workspace_path / "missing-loop.md"
    loop.symlink_to(loop.name)
    dangling = context.workspace_path / "current"
    dangling.symlink_to("releases/missing")

    directory = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "."},
    )

    assert {entry["name"] for entry in directory.output["entries"]} >= {
        neighbor.name,
        dangling.name,
    }
    assert loop.name not in {entry["name"] for entry in directory.output["entries"]}

    with pytest.raises(BrokerPolicyError) as missing:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "missing-loo.md"},
        )
    assert missing.value.code == "READ_PATH_NOT_FOUND"


@pytest.mark.asyncio
async def test_read_directory_reports_incomplete_page(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    for index in range(3):
        (context.workspace_path / f"file-{index}.txt").write_text(
            "content\n",
            encoding="utf-8",
        )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": ".", "limit": 2},
    )

    assert outcome.status == "success"
    assert len(outcome.output["entries"]) == 2
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "page_limit"
    assert outcome.output["retry_hint"] == "Continue with offset=next_offset."
    rest = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": ".", "offset": outcome.output["next_offset"], "limit": 2},
    )
    assert {
        entry["name"] for entry in outcome.output["entries"] + rest.output["entries"]
    } == {f"file-{index}.txt" for index in range(3)}
    assert rest.output["next_offset"] is None


@pytest.mark.asyncio
async def test_read_directory_pages_reach_entries_past_twenty_thousand(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    large_dir = context.workspace_path / "large-dir"
    large_dir.mkdir()
    # One past the entries a directory read once scanned before stopping.
    for index in range(20_001):
        (large_dir / f"{index:05}").touch()

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "large-dir", "offset": 20_000, "limit": 10},
    )

    assert len(outcome.output["entries"]) == 2
    assert outcome.output["next_offset"] is None
    assert outcome.output["truncated"] is False


@pytest.mark.asyncio
async def test_read_directory_says_what_it_skipped(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    folder = context.workspace_path / "project"
    (folder / ".venv").mkdir(parents=True)
    (folder / ".runtime-temp").mkdir()
    (folder / "link.txt").symlink_to(folder / ".venv")

    outcome = await execute_read_tool(
        db_path=db_path, context=context, args={"path": "project"}
    )
    scratch = await execute_read_tool(
        db_path=db_path, context=context, args={"path": "."}
    )

    names = {entry["name"] for entry in outcome.output["entries"]}
    assert names == {".venv", ".runtime-temp"}
    assert "skipped 1 symlink(s)" in str(outcome.output["warning"])
    # Pantaray's own session temp folder in the scratch workspace stays out.
    assert ".runtime-temp" not in {entry["name"] for entry in scratch.output["entries"]}
    assert scratch.output["warning"] is None


@pytest.mark.asyncio
async def test_read_dot_returns_current_workspace_directory(tmp_path: Path) -> None:
    db_path, context, folder = bootstrap_read_runtime_db_with_registered_folder(
        tmp_path
    )
    (context.workspace_path / "scratch-note.txt").write_text("note\n", encoding="utf-8")

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "."},
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "directory"
    assert outcome.output["path"] == str(context.workspace_path)
    assert {"name": "scratch-note.txt", "kind": "file"} in outcome.output["entries"]
    assert folder.real_path != str(context.workspace_path)


@pytest.mark.asyncio
async def test_read_suggests_similar_missing_paths(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "README.md").write_text("# Notes\n", encoding="utf-8")
    (context.workspace_path / "READ_THIS.md").write_text("# Notes\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "READM.md"},
        )

    assert exc_info.value.code == "READ_PATH_NOT_FOUND"
    assert "Did you mean one of these?" in str(exc_info.value)
    assert str(context.workspace_path / "README.md") in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_reports_missing_workspace_absolute_paths(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "README.md").write_text("# Notes\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": str(context.workspace_path / "READM.md")},
        )

    assert exc_info.value.code == "READ_PATH_NOT_FOUND"


@pytest.mark.asyncio
async def test_read_registered_folder_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    db_path = tmp_path / "app-data" / "runtime.db"
    db_path.parent.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_root,
        display_name="Repo",
        organization_ids=(),
        project_ids=(),
        now="2026-03-23T00:00:00Z",
    )
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    context = bootstrap_module.ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={
            "path": str(repo_root / "notes.txt"),
            "offset": 2,
            "limit": 1,
        },
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "file"
    assert outcome.output["path"] == str(repo_root / "notes.txt")
    assert outcome.output["content"] == "beta\n"
    assert outcome.file_paths == (str(repo_root / "notes.txt"),)


@pytest.mark.asyncio
async def test_read_accepts_workspace_absolute_file_path(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    notes_path = context.workspace_path / "notes.txt"
    notes_path.write_text("inside\n", encoding="utf-8")

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": str(notes_path)},
    )

    assert outcome.output["kind"] == "file"
    assert outcome.output["path"] == str(notes_path)


@pytest.mark.asyncio
async def test_read_rejects_absolute_path_outside_manifest(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": str(outside_file)},
        )

    assert exc_info.value.code == "READ_SCOPE_DENIED"


@pytest.mark.asyncio
async def test_read_full_access_allows_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_read_runtime_db(
        tmp_path,
        read_access_scope="full_access",
    )
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": str(outside_file)},
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "file"
    assert outcome.output["path"] == str(outside_file)
    assert outcome.output["content"] == "outside\n"


@pytest.mark.asyncio
async def test_read_rejects_absolute_symlink_escape(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret\n", encoding="utf-8")
    symlink_path = context.workspace_path / "secret-link.txt"
    symlink_path.symlink_to(outside_file)

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": str(symlink_path)},
        )

    assert exc_info.value.code == "READ_PATH_DENIED"


@pytest.mark.asyncio
async def test_read_rejects_reused_invocation_id(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text(
        "alpha\nbeta\ngamma\n",
        encoding="utf-8",
    )

    first_outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        invocation_id="invocation-read-reused",
        tool_request_id="request-read-first",
        args={"path": "notes.txt", "offset": 1, "limit": 1},
    )

    assert first_outcome.status == "success"

    with pytest.raises(BrokerPolicyError, match="already been claimed"):
        await execute_read_tool(
            db_path=db_path,
            context=context,
            invocation_id="invocation-read-reused",
            tool_request_id="request-read-second",
            args={"path": "notes.txt", "offset": 2, "limit": 1},
        )


@pytest.mark.asyncio
async def test_read_rejects_workspace_escape(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(BrokerPolicyError):
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "../outside.txt"},
        )
