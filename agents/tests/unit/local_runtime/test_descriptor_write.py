from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.descriptor_write import (
    CommittedFileWriteError,
    open_writable_parent_descriptor,
    write_text_at_descriptor,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchAddChange,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_structured_patch import (
    StructuredPatchError,
    apply_structured_workspace_patch,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
)


def test_created_parent_entries_are_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced_directories: dict[int, list[str]] = {}
    sync = os.fsync

    def capture_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_directories[os.fstat(fd).st_ino] = os.listdir(fd)
        sync(fd)

    monkeypatch.setattr(os, "fsync", capture_sync)
    descriptor, _ = open_writable_parent_descriptor(
        root_path=tmp_path, relative_path="project/deep/notes.md", create_missing=True
    )
    os.close(descriptor)
    assert synced_directories[tmp_path.stat().st_ino] == ["project"]
    assert synced_directories[(tmp_path / "project").stat().st_ino] == ["deep"]


def test_exclusive_add_syncs_directory_without_temporary_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced_entries: list[list[str]] = []
    sync = os.fsync

    def capture_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_entries.append(os.listdir(fd))
        sync(fd)

    monkeypatch.setattr(os, "fsync", capture_sync)
    descriptor, name = open_writable_parent_descriptor(
        root_path=tmp_path, relative_path="notes.md", create_missing=False
    )
    try:
        write_text_at_descriptor(
            parent_descriptor=descriptor,
            destination_name=name,
            text="knowledge",
            replacement_mode=0o600,
            exclusive=True,
        )
    finally:
        os.close(descriptor)
    assert synced_entries[-1] == ["notes.md"]


@pytest.mark.asyncio
async def test_broker_returns_absolute_changed_path_after_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    sync = os.fsync

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory sync failed")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-sync-failure",
        tool_request_id="request-sync-failure",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": "notes.md",
                    "new_lines": ["knowledge"],
                    "trailing_newline": True,
                }
            ]
        },
    )
    target = context.workspace_path / "notes.md"
    assert target.read_text() == "knowledge\n"
    assert outcome.status == "error"
    assert outcome.output["applied_paths"] == [str(target)]


def test_writes_keep_one_current_file_and_private_permissions(tmp_path: Path) -> None:
    descriptor, name = open_writable_parent_descriptor(
        root_path=tmp_path, relative_path="project/notes.md", create_missing=True
    )
    try:
        for i in range(4):
            write_text_at_descriptor(
                parent_descriptor=descriptor,
                destination_name=name,
                text=f"# Notes\n\nCurrent value: {i}\n",
                replacement_mode=0o600,
                exclusive=i == 0,
            )
        assert (tmp_path / "project/notes.md").read_text().endswith("value: 3\n")
        assert sorted(
            p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
        ) == [
            "project",
            "project/notes.md",
        ]
        assert stat.S_IMODE((tmp_path / "project/notes.md").stat().st_mode) == 0o600
        with pytest.raises(FileExistsError):
            write_text_at_descriptor(
                parent_descriptor=descriptor,
                destination_name=name,
                text="should not replace",
                replacement_mode=0o600,
                exclusive=True,
            )
        assert (tmp_path / "project/notes.md").read_text().endswith("value: 3\n")
    finally:
        os.close(descriptor)


def test_failed_file_sync_preserves_previous_content_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.md"
    target.write_text("existing knowledge\n")
    descriptor, name = open_writable_parent_descriptor(
        root_path=tmp_path, relative_path="notes.md", create_missing=False
    )

    def fail_sync(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "fsync", fail_sync)
    try:
        with pytest.raises(OSError, match="no space left on device") as caught:
            write_text_at_descriptor(
                parent_descriptor=descriptor,
                destination_name=name,
                text="replacement",
                replacement_mode=0o600,
                exclusive=False,
            )
        assert not isinstance(caught.value, CommittedFileWriteError)
    finally:
        os.close(descriptor)
    assert target.read_text() == "existing knowledge\n"
    assert list(tmp_path.iterdir()) == [target]


def test_patch_reports_file_changed_when_directory_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = os.fsync

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory sync failed")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(StructuredPatchError) as caught:
        apply_structured_workspace_patch(
            patch_root=tmp_path,
            path_rewrites={"notes.md": "notes.md"},
            changes=[
                ApplyPatchAddChange(
                    op="add",
                    path="notes.md",
                    new_lines=["# Notes"],
                    trailing_newline=True,
                )
            ],
        )
    assert caught.value.applied_paths == ("notes.md",)
    assert "The file changed" in str(caught.value)
    assert (tmp_path / "notes.md").read_text() == "# Notes\n"
    assert [p.name for p in tmp_path.iterdir()] == ["notes.md"]


def test_write_stays_on_open_parent_when_path_is_replaced(tmp_path: Path) -> None:
    parent = tmp_path / "project"
    parent.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    descriptor, name = open_writable_parent_descriptor(
        root_path=tmp_path, relative_path="project/notes.md", create_missing=False
    )
    parent.rename(tmp_path / "original")
    parent.symlink_to(external, target_is_directory=True)
    try:
        write_text_at_descriptor(
            parent_descriptor=descriptor,
            destination_name=name,
            text="knowledge",
            replacement_mode=0o600,
            exclusive=True,
        )
    finally:
        os.close(descriptor)
    assert not list(external.iterdir())
    assert (tmp_path / "original/notes.md").read_text() == "knowledge"
