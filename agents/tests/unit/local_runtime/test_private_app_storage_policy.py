from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.local_runtime.ripgrep_backend_test_support import (
    install_fake_ripgrep_backend,
)

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.tooling.brokering import broker_discovery_ripgrep
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_discovery_ripgrep import (
    RIPGREP_TRUSTED_PATH,
)
from pantaray_agents.local_runtime.tooling.brokering.manifest_paths import (
    load_tool_results_root,
)
from pantaray_agents.local_runtime.tooling.brokering.tool_path_policy import (
    EXEC_CWD_DENIED,
    READ_PATH_DENIED,
    WRITE_PATH_DENIED,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
)
from pantaray_agents.schema.agent.base import JSONValue

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _grant_workspace_full_access,
)
from .path_access_policy_support import (
    ACTION_ID,
    USER_ID,
    bootstrap_path_policy_runtime_db,
)

_TOOL_IDS = ("read", "list", "glob", "grep", "bash", "apply_patch")


async def _run(
    *, db_path: Path, context: object, tool_id: str, args: dict[str, JSONValue]
):
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id=tool_id,
        user_id=USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        tool_request_id=f"request-{uuid4().hex}",
        preflight_only=tool_id == "bash",
        args=args,
    )


def _search_args(tool_id: str, base: str) -> dict[str, JSONValue]:
    if tool_id == "read":
        return {"path": base}
    if tool_id == "list":
        return {"path": base, "max_depth": 6, "limit": 50}
    if tool_id == "glob":
        return {"base_path": base, "pattern": "**/*.txt", "limit": 50}
    return {
        "base_path": base,
        "pattern": "needle",
        "include_glob": "**/*.txt",
        "max_matches": 50,
    }


def _case_alias(path: Path) -> Path:
    alias = path.with_name(path.name.swapcase())
    return alias if alias.exists() else path


@pytest.mark.asyncio
async def test_full_access_read_tools_refuse_private_app_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=_TOOL_IDS,
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )
    storage = db_path.parent
    (storage / "notes.txt").write_text("needle secret\n", encoding="utf-8")
    workspace = context.workspace_path
    (workspace / "storage-link").symlink_to(storage)
    up_to_storage = os.path.relpath(storage, workspace)
    artifact_root = read_local_runtime_artifact_root()
    artifact_root.mkdir(parents=True, exist_ok=True)

    targets = (
        str(storage),
        str(_case_alias(storage.resolve())),
        "storage-link",
        up_to_storage,
        str(workspace.parent),  # another part of the Action's own storage
        str(artifact_root),
    )
    for tool_id in ("read", "list", "glob", "grep"):
        for target in targets:
            with pytest.raises(BrokerPolicyError) as caught:
                await _run(
                    db_path=db_path,
                    context=context,
                    tool_id=tool_id,
                    args=_search_args(tool_id, target),
                )
            assert caught.value.code == READ_PATH_DENIED, (tool_id, target)
            assert "memory_sql" in str(caught.value.fix_hint)

    for path in (
        str(storage / "notes.txt"),
        "storage-link/notes.txt",
        f"{up_to_storage}/notes.txt",
        str(storage / "notes-missing.txt"),
    ):
        with pytest.raises(BrokerPolicyError) as caught:
            await _run(
                db_path=db_path,
                context=context,
                tool_id="read",
                args={"path": path},
            )
        assert caught.value.code == READ_PATH_DENIED, path


@pytest.mark.asyncio
async def test_own_workspace_and_results_stay_readable_inside_app_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=_TOOL_IDS,
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )
    (db_path.parent / "notes.txt").write_text("needle secret\n", encoding="utf-8")
    workspace_file = context.workspace_path / "work.txt"
    workspace_file.write_text("needle work\n", encoding="utf-8")
    results = load_tool_results_root(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER_ID,
        manifest_id=context.manifest_id,
        action_id=ACTION_ID,
    )
    results.mkdir(parents=True, exist_ok=True)
    result_file = results / "result.txt"
    result_file.write_text("needle result\n", encoding="utf-8")

    for path, content in ((workspace_file, "needle work\n"), (result_file, None)):
        read = await _run(
            db_path=db_path,
            context=context,
            tool_id="read",
            args={"path": str(path)},
        )
        assert read.status == "success"
        if content is not None:
            assert read.output["content"] == content

    # Searched from above app storage, only the Action's own roots show, also
    # through a case alias on a case-insensitive volume.
    above_storage = str(_case_alias(tmp_path.resolve()))
    for tool_id in ("list", "glob", "grep"):
        outcome = await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            args=_search_args(tool_id, above_storage),
        )
        items = outcome.output["entries" if tool_id == "list" else "matches"]
        paths = [Path(str(item["path"])) for item in items]
        # list stops at max_depth 6, which reaches the workspace folder itself.
        visible = context.workspace_path if tool_id == "list" else workspace_file
        assert any(path.samefile(visible) for path in paths), tool_id
        for private in (db_path.parent, db_path.parent / "notes.txt"):
            assert not any(path.samefile(private) for path in paths), tool_id
        assert "needle secret" not in (outcome.search_text or ""), tool_id


@pytest.mark.asyncio
async def test_command_cwd_in_app_storage_points_to_memory_sql(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=_TOOL_IDS,
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )

    with pytest.raises(BrokerPolicyError) as caught:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            args={"command": "ls", "cwd": str(db_path.parent)},
        )

    assert caught.value.code == EXEC_CWD_DENIED
    assert "private app storage" in str(caught.value)
    assert "memory_sql" in str(caught.value.fix_hint)


def _add_folder_root(*, db_path: Path, manifest_id: str, folder: Path) -> None:
    # A registered folder that contains app storage, like the home folder.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO workspace_manifest_roots(
                root_id,manifest_id,source_type,source_id,display_name,
                canonical_real_path,real_path,can_read,can_apply_patch,
                can_process_read,can_process_write,created_at
            ) VALUES ('home-root',?,'folder','home','Home',?,?,1,1,1,1,
                      '2026-03-23T00:00:00Z')""",
            (manifest_id, str(folder.resolve()), str(folder.resolve())),
        )


def _patch_args(op: str, path: str) -> dict[str, JSONValue]:
    if op == "add":
        return {
            "changes": [
                {
                    "op": "add",
                    "path": path,
                    "new_lines": ["x"],
                    "trailing_newline": True,
                }
            ]
        }
    if op == "update":
        return {
            "changes": [
                {
                    "op": "update",
                    "path": path,
                    "edits": [{"old_lines": ["kept"], "new_lines": ["changed"]}],
                }
            ]
        }
    return {"changes": [{"op": "delete", "path": path}]}


@pytest.mark.asyncio
async def test_apply_patch_refuses_private_app_storage(tmp_path: Path) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path, allowed_tool_ids=_TOOL_IDS
    )
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    storage = db_path.parent
    notes = storage / "notes.txt"
    notes.write_text("kept\n", encoding="utf-8")
    workspace = context.workspace_path
    (workspace / "storage-link").symlink_to(storage)

    async def refused(op: str, path: str) -> None:
        with pytest.raises(BrokerPolicyError) as caught:
            await _run(
                db_path=db_path,
                context=context,
                tool_id="apply_patch",
                args=_patch_args(op, path),
            )
        assert caught.value.code == WRITE_PATH_DENIED, (op, path)
        assert "private app storage" in str(caught.value), (op, path)
        assert "memory_sql" in str(caught.value.fix_hint), (op, path)

    # No root holds app storage: the path stays outside every approvable folder.
    await refused("add", str(storage / "new.txt"))

    # A registered folder that contains app storage does not open it either.
    _add_folder_root(db_path=db_path, manifest_id=context.manifest_id, folder=tmp_path)
    for op, path in (
        ("add", str(storage / "new.txt")),
        ("add", str(_case_alias(storage.resolve()) / "new.txt")),
        ("add", "storage-link/new.txt"),
        ("add", str(workspace.parent / "new.txt")),
        ("update", str(notes)),
        ("update", "storage-link/notes.txt"),
        ("update", str(db_path)),
        ("delete", str(notes)),
        ("delete", str(db_path)),
    ):
        await refused(op, path)

    assert not (storage / "new.txt").exists()
    assert not (workspace.parent / "new.txt").exists()
    assert notes.read_text(encoding="utf-8") == "kept\n"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    # The Action's own workspace inside app storage stays writable.
    written = await _run(
        db_path=db_path,
        context=context,
        tool_id="apply_patch",
        args=_patch_args("add", str(workspace / "own.txt")),
    )
    assert written.status == "success"
    assert (workspace / "own.txt").read_text(encoding="utf-8") == "x\n"


def _seed_registered_parent(tmp_path: Path) -> tuple[Path, object, Path, Path]:
    """A registered folder that holds app storage next to the user's own files."""

    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path, allowed_tool_ids=_TOOL_IDS
    )
    _add_folder_root(db_path=db_path, manifest_id=context.manifest_id, folder=tmp_path)
    public = tmp_path / "docs" / "public.txt"
    public.parent.mkdir()
    public.write_text("needle public\n", encoding="utf-8")
    own = context.workspace_path / "work.txt"  # type: ignore[attr-defined]
    own.write_text("needle work\n", encoding="utf-8")
    return db_path, context, public, own


def _paths(outcome: object, key: str) -> list[Path]:
    items = outcome.output[key]  # type: ignore[attr-defined]
    return [Path(str(item["path"])) for item in items]


@pytest.mark.asyncio
async def test_list_from_a_parent_does_not_walk_private_app_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker_discovery

    db_path, context, public, _own = _seed_registered_parent(tmp_path)
    # Other Actions' folders sit beside this Action's, on the way to its roots.
    actions = context.workspace_path.parent.parent  # type: ignore[attr-defined]
    for index in range(100):
        other = actions / f"other-{index}"
        other.mkdir()
        (other / "notes.txt").write_text("needle secret\n", encoding="utf-8")
    # Enough for the user's files and the path down to the Action's own roots,
    # not for anything in app storage.
    monkeypatch.setattr(broker_discovery, "DISCOVERY_MAX_SCANNED_PATHS", 30)

    outcome = await _run(
        db_path=db_path,
        context=context,
        tool_id="list",
        args={"path": str(tmp_path), "max_depth": 6, "limit": 50},
    )

    assert outcome.output["truncation_reason"] is None
    paths = _paths(outcome, "entries")
    assert public in paths
    # max_depth 6 reaches the Action's workspace folder inside app storage.
    assert context.workspace_path in paths  # type: ignore[attr-defined]
    assert not any(path.name.startswith("other-") for path in paths)
    assert db_path not in paths


@pytest.mark.skipif(
    shutil.which("rg", path=RIPGREP_TRUSTED_PATH) is None,
    reason="ripgrep is not installed in a trusted location",
)
@pytest.mark.asyncio
async def test_search_from_a_parent_does_not_read_private_app_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context, public, own = _seed_registered_parent(tmp_path)
    results = context.tool_results_path  # type: ignore[attr-defined]
    results.mkdir(parents=True, exist_ok=True)
    result_file = results / "result.txt"
    result_file.write_text("needle result\n", encoding="utf-8")
    bulk = db_path.parent / "bulk"
    bulk.mkdir()
    # Past ripgrep's whole output budget in matching lines alone.
    line = "needle secret\n"
    for index in range(3):
        (bulk / f"big-{index}.txt").write_text(
            line
            * (broker_discovery_ripgrep.RIPGREP_MAX_STDOUT_BYTES // len(line) // 2),
            encoding="utf-8",
        )

    grep = await _run(
        db_path=db_path,
        context=context,
        tool_id="grep",
        args=_search_args("grep", str(tmp_path)),
    )

    assert grep.output["truncation_reason"] is None
    assert sorted(_paths(grep, "matches")) == sorted([public, own, result_file])

    # File names spend the output budget too; lower it so a few hundred do.
    monkeypatch.setattr(broker_discovery_ripgrep, "RIPGREP_MAX_STDOUT_BYTES", 4_096)
    for index in range(300):
        (bulk / f"private-{index:04}.txt").write_text("", encoding="utf-8")

    glob = await _run(
        db_path=db_path,
        context=context,
        tool_id="glob",
        args=_search_args("glob", str(tmp_path)),
    )

    assert glob.output["truncation_reason"] is None
    assert sorted(_paths(glob, "matches")) == sorted([public, own, result_file])
