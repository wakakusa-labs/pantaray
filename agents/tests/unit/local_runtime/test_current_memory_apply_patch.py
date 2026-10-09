from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.descriptor_write import CommittedFileWriteError
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.current_files_index import (
    refresh_current_memory_index,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MemoryFiles,
    create_editable_memory_root,
    locked_memory_files,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    FinalizedBrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)


@pytest.fixture
def memory_runtime(tmp_path, monkeypatch):
    db, raw_context = _bootstrap_runtime_db(tmp_path)
    context = cast(ActionExecutionContext, raw_context)
    _grant_workspace_full_access(db_path=db, capability="scoped_write")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifacts))
    root = create_editable_memory_root(artifact_root=artifacts, user_id="user-1")
    with (
        locked_memory_files(artifact_root=artifacts, user_id="user-1") as files,
        open_memory_catalog_connection(db_path=db, busy_timeout_ms=1000) as connection,
        immediate_transaction(connection),
    ):
        files.write(
            path="agent_experience/project.md", text="old\n", expected_text=None
        )
        files.write(path="facts/profile.md", text="private\n", expected_text=None)
        refresh_current_memory_index(connection=connection, files=files)
        connection.execute(
            """INSERT INTO workspace_manifest_roots(
                root_id,manifest_id,source_type,source_id,display_name,
                canonical_real_path,real_path,can_read,can_apply_patch,
                can_process_read,can_process_write,created_at
            ) VALUES ('memory-root',?,'agent_experience','user-1','Agent Experience',?,?,1,1,0,0,'2026-09-01')""",
            (
                context.manifest_id,
                str(root / "agent_experience"),
                str(root / "agent_experience"),
            ),
        )
        connection.execute(
            """INSERT INTO workspace_manifest_roots(
                root_id,manifest_id,source_type,source_id,display_name,
                canonical_real_path,real_path,can_read,can_apply_patch,
                can_process_read,can_process_write,created_at
            ) VALUES ('broad-root',?,'folder','broad-folder','Broad workspace',?,?,1,1,1,1,'2026-09-01')""",
            (context.manifest_id, str(tmp_path), str(tmp_path)),
        )
    return db, context, root


def _change(path: Path, op="update"):
    result = {"op": op, "path": str(path)}
    if op == "update":
        result["edits"] = [{"old_lines": ["old"], "new_lines": ["new"]}]
    elif op == "add":
        result.update(new_lines=["saved"], trailing_newline=True)
    return result


async def _execute(runtime, change, invocation):
    db, context, _ = runtime
    return await execute_broker_tool(
        db_path=db,
        busy_timeout_ms=1000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id=invocation,
        tool_request_id=f"request-{invocation}",
        args={"changes": [change]},
    )


def _indexed_text(db, path):
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT content_text FROM memory_fragments WHERE source_path=? AND block_kind='document_root'",
            (path,),
        ).fetchone()


@pytest.mark.asyncio
async def test_patch_requires_current_read_and_updates_the_current_index(
    memory_runtime,
):
    db, _, root = memory_runtime
    target = root / "agent_experience/project.md"
    change = _change(target)
    first = await _execute(memory_runtime, change, "read")
    assert first.output["status"] == "needs_read"
    assert first.output["text"] == "old\n"
    assert target.read_text() == "old\n"
    saved = await _execute(memory_runtime, change, "save")
    assert saved.output["status"] == "success"
    assert saved.output["applied_paths"] == [str(target)]
    assert target.read_text() == "new\n"
    assert _indexed_text(db, "agent_experience/project.md") == ("new\n",)


@pytest.mark.asyncio
async def test_add_to_existing_memory_uses_the_apply_patch_target_exists_contract(
    memory_runtime,
):
    db, _, root = memory_runtime
    target = root / "agent_experience/project.md"
    result = await _execute(memory_runtime, _change(target, "add"), "already-exists")
    assert result.output["status"] == "error"
    assert result.output["error"]["code"] == "PATCH_TARGET_EXISTS"
    assert (
        result.output["error"]["llm_feedback"]
        == "PATCH_TARGET_EXISTS: use update for existing files."
    )
    assert result.output["applied_paths"] == []
    assert target.read_text() == "old\n"
    assert _indexed_text(db, "agent_experience/project.md") == ("old\n",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,code",
    [
        ("add", "PATCH_TARGET_EXISTS"),
        ("update", "PATCH_TARGET_NOT_FILE"),
        ("delete", "PATCH_TARGET_NOT_FILE"),
    ],
)
async def test_patch_on_a_markdown_directory_preserves_the_operation_error(
    memory_runtime,
    operation,
    code,
):
    _, _, root = memory_runtime
    target = root / "agent_experience/folder.md"
    child = target / "nested.md"
    created = await _execute(memory_runtime, _change(child, "add"), "nested-file")
    assert created.output["status"] == "success"
    result = await _execute(
        memory_runtime, _change(target, operation), "directory-exists"
    )
    assert result.output["error"]["code"] == code
    assert result.output["applied_paths"] == []
    assert child.read_text() == "saved\n"


@pytest.mark.asyncio
async def test_unreadable_patch_target_reports_read_failure(memory_runtime):
    _, _, root = memory_runtime
    target = root / "agent_experience/project.md"
    target.write_bytes(b"\xff")
    result = await _execute(memory_runtime, _change(target), "unreadable")
    assert result.output["error"]["code"] == "PATCH_READ_FAILED"
    assert result.output["applied_paths"] == []
    assert target.read_bytes() == b"\xff"


@pytest.mark.asyncio
async def test_add_and_delete_save_without_generations(memory_runtime):
    db, _, root = memory_runtime
    target = root / "agent_experience/new.md"
    added = await _execute(memory_runtime, _change(target, "add"), "add")
    assert added.output["status"] == "success"
    assert target.read_text() == "saved\n"
    assert _indexed_text(db, "agent_experience/new.md") == ("saved\n",)
    assert (
        await _execute(memory_runtime, _change(target, "delete"), "read-delete")
    ).output["status"] == "needs_read"
    deleted = await _execute(memory_runtime, _change(target, "delete"), "delete")
    assert deleted.output["status"] == "success"
    assert not target.exists()
    assert _indexed_text(db, "agent_experience/new.md") is None
    assert not (root.parent / "nodes").exists()


@pytest.mark.asyncio
async def test_external_crlf_change_invalidates_the_read_snapshot(memory_runtime):
    _, _, root = memory_runtime
    target = root / "agent_experience/project.md"
    change = _change(target)
    await _execute(memory_runtime, change, "read")
    target.write_bytes(b"external\r\n")
    result = await _execute(memory_runtime, change, "retry")
    assert result.output["status"] == "needs_read"
    assert result.output["text"] == "external\r\n"
    assert target.read_bytes() == b"external\r\n"


@pytest.mark.asyncio
async def test_bom_memory_file_edits_from_the_returned_first_line(memory_runtime):
    _, _, root = memory_runtime
    target = root / "agent_experience/project.md"
    target.write_bytes(b"\xef\xbb\xbfold\r\nkeep\r\n")
    first = await _execute(memory_runtime, _change(target), "bom-read")
    assert first.output["status"] == "needs_read"
    returned_first_line = first.output["text"].split("\r\n")[0]
    change = {
        "op": "update",
        "path": str(target),
        "edits": [{"old_lines": [returned_first_line], "new_lines": ["new"]}],
    }
    saved = await _execute(memory_runtime, change, "bom-save")
    assert saved.output["status"] == "success"
    assert target.read_bytes() == b"\xef\xbb\xbfnew\r\nkeep\r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative",
    ["facts/new.md", "insights/new.md", "../../other/files/agent_experience/new.md"],
)
async def test_broad_workspace_cannot_write_other_memory_categories_or_users(
    memory_runtime, relative
):
    _, _, root = memory_runtime
    target = (root / relative).resolve()
    with pytest.raises(BrokerPolicyError) as caught:
        await _execute(memory_runtime, _change(target, "add"), "forbidden")
    assert caught.value.code == "WRITE_PATH_DENIED"
    assert not target.exists()


@pytest.mark.asyncio
async def test_case_alias_cannot_bypass_the_private_memory_boundary(memory_runtime):
    _, _, root = memory_runtime
    users = root.parent.parent
    alias = users.with_name(users.name.upper())
    if not alias.exists() or not alias.samefile(users):
        pytest.skip("filesystem is case sensitive")
    target = alias / "user-1/files/facts/new.md"
    with pytest.raises(BrokerPolicyError) as caught:
        await _execute(memory_runtime, _change(target, "add"), "alias")
    assert caught.value.code == "WRITE_PATH_DENIED"
    assert not target.exists()


@pytest.mark.asyncio
async def test_current_file_root_must_belong_to_the_authenticated_user(memory_runtime):
    db, _, root = memory_runtime
    other = create_editable_memory_root(artifact_root=root.parents[3], user_id="other")
    (other / "agent_experience").mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE workspace_manifest_roots SET real_path=?,canonical_real_path=? WHERE root_id='memory-root'",
            (str(other / "agent_experience"), str(other / "agent_experience")),
        )
    target = other / "agent_experience/new.md"
    with pytest.raises(BrokerPolicyError):
        await _execute(memory_runtime, _change(target, "add"), "wrong-user")
    assert not target.exists()


@pytest.mark.asyncio
async def test_actor_cancellation_while_waiting_for_memory_lock_blocks_the_save(
    memory_runtime, monkeypatch
):
    from pantaray_agents.local_runtime.tooling.brokering import (
        broker_current_memory_patch as memory_patch,
    )

    db, _, root = memory_runtime
    original = memory_patch.locked_memory_files

    @contextmanager
    def cancel_before_lock(**kwargs):
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE processes SET status='canceled' WHERE process_id=?",
                (BROKER_ACTOR_PROCESS_ID,),
            )
        with original(**kwargs) as files:
            yield files

    monkeypatch.setattr(memory_patch, "locked_memory_files", cancel_before_lock)
    target = root / "agent_experience/new.md"
    with pytest.raises(FinalizedBrokerPolicyError) as caught:
        await _execute(memory_runtime, _change(target, "add"), "canceled")
    assert caught.value.cause.code == "ACTION_SUBAGENT_WRITE_DENIED"
    assert not target.exists()


@pytest.mark.asyncio
async def test_index_failure_reports_the_saved_file_and_rebuilds_from_it(
    memory_runtime, monkeypatch
):
    from pantaray_agents.local_runtime.tooling.brokering import (
        broker_current_memory_patch as memory_patch,
    )

    db, _, root = memory_runtime
    target = root / "agent_experience/new.md"

    def fail_index(**kwargs):
        raise sqlite3.OperationalError("injected index failure")

    with monkeypatch.context() as patch:
        patch.setattr(memory_patch, "refresh_current_memory_index", fail_index)
        result = await _execute(memory_runtime, _change(target, "add"), "index-failed")
    assert result.output["status"] == "error"
    assert result.output["applied_paths"] == [str(target)]
    assert result.output["error"]["code"] == "PATCH_WRITE_FAILED"
    assert target.read_text() == "saved\n"
    with (
        locked_memory_files(artifact_root=root.parents[3], user_id="user-1") as files,
        open_memory_catalog_connection(db_path=db, busy_timeout_ms=1000) as connection,
        immediate_transaction(connection),
    ):
        refresh_current_memory_index(connection=connection, files=files)
    assert _indexed_text(db, "agent_experience/new.md") == ("saved\n",)


@pytest.mark.asyncio
async def test_file_removed_during_post_save_scan_reports_write_failure(
    memory_runtime, monkeypatch
):
    from pantaray_agents.local_runtime.memory_catalog import editable_files
    from pantaray_agents.local_runtime.tooling.brokering import (
        broker_current_memory_patch as memory_patch,
    )

    db, _, root = memory_runtime
    target = root / "agent_experience/new.md"
    read_text = editable_files._read_text

    def remove_before_read(descriptor, relative_path, **kwargs):
        if relative_path == "profile.md":
            (root / "facts/profile.md").unlink()
        return read_text(descriptor, relative_path, **kwargs)

    def scan_after_external_removal(**kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(editable_files, "_read_text", remove_before_read)
            refresh_current_memory_index(**kwargs)

    monkeypatch.setattr(
        memory_patch, "refresh_current_memory_index", scan_after_external_removal
    )
    result = await _execute(memory_runtime, _change(target, "add"), "scan-race")
    assert result.output["error"]["code"] == "PATCH_WRITE_FAILED"
    assert result.output["applied_paths"] == [str(target)]
    assert target.read_text() == "saved\n"
    with (
        locked_memory_files(artifact_root=root.parents[3], user_id="user-1") as files,
        open_memory_catalog_connection(db_path=db, busy_timeout_ms=1000) as connection,
        immediate_transaction(connection),
    ):
        refresh_current_memory_index(connection=connection, files=files)
    assert _indexed_text(db, "agent_experience/new.md") == ("saved\n",)
    assert _indexed_text(db, "facts/profile.md") is None


@pytest.mark.asyncio
async def test_sync_failure_after_atomic_save_reports_the_committed_path(
    memory_runtime, monkeypatch
):
    _, _, root = memory_runtime
    write = MemoryFiles.write

    def save_then_fail(self, **kwargs):
        write(self, **kwargs)
        raise CommittedFileWriteError(5, "directory sync failed after atomic save")

    monkeypatch.setattr(MemoryFiles, "write", save_then_fail)
    target = root / "agent_experience/new.md"
    result = await _execute(memory_runtime, _change(target, "add"), "sync-failed")
    assert result.output["status"] == "error"
    assert result.output["applied_paths"] == [str(target)]
    assert target.read_text() == "saved\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,text",
    [
        ("new.txt", "not Markdown"),
        ("new.md", '[[ref:ref_forged note:"invented"]]'),
        ("large.md", "x" * (1024 * 1024 + 1)),
    ],
)
async def test_action_cannot_bypass_file_or_reference_constraints(
    memory_runtime, path, text
):
    _, _, root = memory_runtime
    target = root / "agent_experience" / path
    change = _change(target, "add")
    change["new_lines"] = [text]
    result = await _execute(memory_runtime, change, "invalid")
    assert result.output["status"] == "error"
    assert result.output["applied_paths"] == []
    assert not target.exists()
