from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemoryDocument,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    EditablePathPolicy,
    MemoryPatchError,
    MemoryPatchErrorCode,
    PatchChunk,
    PatchLine,
    SandboxPathError,
    apply_memory_patch,
    read_text_file_raw,
    resolve_sandbox_path,
    search_text_files,
    write_text_file_raw,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    MemoryDraftToolSession,
    ReadableFileRoot,
    build_local_memory_file_tools,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.logical_draft_io import (
    validate_memory_document_path,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolRegistry,
    ToolCallEnvelope,
)
from pantaray_agents.tools.memory.retrieval import (
    MemoryContextSession,
)


def test_read_file_returns_raw_text_without_strip(tmp_path: Path) -> None:
    target = tmp_path / "facts.md"
    target.write_text("  leading\n\ntrailing  \n", encoding="utf-8")

    text = read_text_file_raw(sandbox_root=tmp_path, path="facts.md")

    assert text == "  leading\n\ntrailing  \n"


def test_resolve_sandbox_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-memory-file-editor"
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SandboxPathError):
        resolve_sandbox_path(
            sandbox_root=tmp_path,
            relative_path="link/secret.md",
            must_exist=True,
            must_be_file=True,
        )


def test_search_text_files_skips_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("private-token", encoding="utf-8")
    (workspace / "secret-link.md").symlink_to(outside / "secret.md")

    matches = search_text_files(
        sandbox_root=workspace,
        query="private-token",
        limit=20,
    )

    assert matches == ()


def test_resolve_sandbox_path_allows_missing_parent_inside_sandbox(
    tmp_path: Path,
) -> None:
    resolved = resolve_sandbox_path(
        sandbox_root=tmp_path,
        relative_path="notes/new/facts.md",
        must_exist=False,
    )

    assert resolved.path == tmp_path / "notes" / "new" / "facts.md"


def test_write_text_file_rejects_final_symlink_into_protected_path(
    tmp_path: Path,
) -> None:
    facts = tmp_path / "facts"
    facts.mkdir()
    protected = tmp_path / "protected.md"
    protected.write_text("protected\n", encoding="utf-8")
    (facts / "link.md").symlink_to(protected)

    with pytest.raises(SandboxPathError):
        write_text_file_raw(
            sandbox_root=tmp_path,
            editable_policy=EditablePathPolicy(("facts/**",)),
            path="facts/link.md",
            text="overwritten\n",
        )

    assert protected.read_text(encoding="utf-8") == "protected\n"


def test_write_text_file_applies_policy_after_parent_symlink_resolution(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    (tmp_path / "facts").symlink_to(protected, target_is_directory=True)

    with pytest.raises(SandboxPathError):
        write_text_file_raw(
            sandbox_root=tmp_path,
            editable_policy=EditablePathPolicy(("facts/**",)),
            path="facts/new.md",
            text="denied\n",
        )

    assert not (protected / "new.md").exists()


def test_apply_memory_patch_requires_unique_context() -> None:
    chunk = PatchChunk(
        lines=(
            PatchLine(op="context", text="- same"),
            PatchLine(op="add", text="- added"),
        )
    )

    with pytest.raises(MemoryPatchError) as exc_info:
        apply_memory_patch("- same\n- same\n", (chunk,))

    assert exc_info.value.code == MemoryPatchErrorCode.CONTEXT_AMBIGUOUS


@pytest.mark.parametrize(
    "path",
    (
        "",
        "/facts.md",
        "facts//a.md",
        "facts/./a.md",
        "../a.md",
        "a\\b.md",
    ),
)
def test_memory_document_path_rejects_noncanonical_values(path: str) -> None:
    with pytest.raises(ValueError):
        validate_memory_document_path(path)


def test_shell_and_memory_domain_tools_are_available() -> None:
    _, tools = _build_tools((MemoryDocument("facts/index.md", "# Facts\n"),))

    assert {
        "read_file",
        "search_files",
        "write_file",
        "apply_patch",
        "shell",
        "link_memory",
        "unlink_memory",
        "move_memory_file",
        "delete_memory_file",
    }.issubset({tool.name for tool in tools})


@pytest.mark.asyncio
async def test_draft_read_search_and_edits_never_touch_disk(tmp_path: Path) -> None:
    outside = tmp_path / "facts"
    outside.mkdir()
    disk_file = outside / "index.md"
    disk_file.write_text("outside\n", encoding="utf-8")
    session, tools = _build_tools(
        (MemoryDocument("facts/index.md", "# Facts\n\n- Current.\n"),)
    )
    registry = ReactToolRegistry(tools)

    read_result = await registry.execute(
        _tool_call(
            "read_file",
            {"root": "memory_draft", "path": "facts/index.md"},
        ),
        1,
    )
    search_result = await registry.execute(
        _tool_call("search_files", {"root": "memory_draft", "query": "Current"}),
        2,
    )
    patch_result = await registry.execute(
        _tool_call(
            "apply_patch",
            {
                "path": "facts/index.md",
                "chunks": [
                    {
                        "lines": [
                            {"op": "remove", "text": "- Current."},
                            {"op": "add", "text": "- Updated."},
                        ]
                    }
                ],
            },
        ),
        3,
    )
    write_result = await registry.execute(
        _tool_call("write_file", {"path": "facts/new.md", "text": "# New\n"}),
        4,
    )

    assert read_result.output["text"] == "# Facts\n\n- Current.\n"
    assert search_result.output["matches"][0]["path"] == "facts/index.md"
    assert patch_result.status == "success"
    assert write_result.status == "success"
    assert _document_text(session, "facts/index.md") == "# Facts\n\n- Updated.\n"
    assert _document_text(session, "facts/new.md") == "# New\n"
    assert disk_file.read_text(encoding="utf-8") == "outside\n"
    assert not (outside / "new.md").exists()


@pytest.mark.asyncio
async def test_readable_workspace_root_remains_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "facts.md").write_text("workspace\n", encoding="utf-8")
    session, tools = _build_tools(
        (MemoryDocument("facts.md", "draft\n"),),
        policy=EditablePathPolicy(("facts.md",)),
        readable_roots=(
            ReadableFileRoot(
                "workspace_1", "Product repo", workspace, workspace.resolve()
            ),
        ),
    )
    read_tool = next(tool for tool in tools if tool.name == "read_file")
    search_tool = next(tool for tool in tools if tool.name == "search_files")

    draft_read = await read_tool.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    workspace_read = await read_tool.execute(
        _tool_call("read_file", {"root": "workspace_1", "path": "facts.md"}), 2
    )
    path_search = await search_tool.execute(
        _tool_call("search_files", {"root": "workspace_1", "query": "facts.md"}),
        3,
    )

    assert read_tool.request_schema["required"] == ["root", "path"]
    assert read_tool.request_schema["properties"]["root"]["enum"] == [
        "memory_draft",
        "workspace_1",
    ]
    assert draft_read.output["text"] == "draft\n"
    assert workspace_read.output["text"] == "workspace\n"
    assert path_search.output["matches"][0]["line_number"] == 0
    assert _document_text(session, "facts.md") == "draft\n"


@pytest.mark.asyncio
async def test_readable_root_rejects_registered_path_retargeting(
    tmp_path: Path,
) -> None:
    registered = tmp_path / "workspace"
    moved = tmp_path / "workspace-original"
    outside = tmp_path / "outside"
    registered.mkdir()
    outside.mkdir()
    approved = registered.resolve()
    registered.rename(moved)
    (outside / "secret.md").write_text("secret\n", encoding="utf-8")
    registered.symlink_to(outside, target_is_directory=True)
    _, tools = _build_tools(
        (MemoryDocument("facts.md", "draft\n"),),
        policy=EditablePathPolicy(("facts.md",)),
        readable_roots=(
            ReadableFileRoot("workspace_1", "Product repo", registered, approved),
        ),
    )
    read_tool = next(tool for tool in tools if tool.name == "read_file")

    result = await read_tool.execute(
        _tool_call("read_file", {"root": "workspace_1", "path": "secret.md"}), 1
    )

    assert result.status == "error"
    assert result.output["error_code"] == "PATH_DENIED"


@pytest.mark.asyncio
async def test_link_move_and_delete_update_only_checkpoint() -> None:
    epoch, target = append_memory_context_item(
        epoch=MemoryContextEpoch("epoch-1", "run-1", "user-1", ()),
        source="activity_log",
        label="source activity",
        source_path="body.md",
        heading_path=None,
        content="Persisted evidence.",
        fragment_id="fragment-1",
        revision_id="revision-1",
        node_id="activity-1",
        reference_depth=0,
    )
    session, tools = _build_tools(
        (
            MemoryDocument("facts/index.md", "# Facts\n\n- Updated.\n"),
            MemoryDocument("facts/old.md", "# Old\n"),
        ),
        epoch=epoch,
    )
    registry = ReactToolRegistry(tools)
    link = await registry.execute(
        _tool_call(
            "link_memory",
            {
                "target_handle": target.item.context_handle,
                "source_path": "facts/index.md",
                "exact_text": "- Updated.",
                "occurrence": 1,
                "note": "The activity supports this update.",
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        1,
    )
    move = await registry.execute(
        _tool_call(
            "move_memory_file",
            {
                "source_path": "facts/old.md",
                "destination_path": "facts/archive.md",
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        2,
    )
    delete = await registry.execute(
        _tool_call(
            "delete_memory_file",
            {
                "path": "facts/archive.md",
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        3,
    )

    assert link.status == move.status == delete.status == "success"
    assert "[[ref:" in _document_text(session, "facts/index.md")
    assert {item.source_path for item in session.draft.documents} == {"facts/index.md"}


@pytest.mark.asyncio
async def test_link_replay_returns_the_persisted_reference_id() -> None:
    epoch, target = append_memory_context_item(
        epoch=MemoryContextEpoch("epoch-1", "run-1", "user-1", ()),
        source="activity_log",
        label="source activity",
        source_path="body.md",
        heading_path=None,
        content="Persisted evidence.",
        fragment_id="fragment-1",
        revision_id="revision-1",
        node_id="activity-1",
        reference_depth=0,
    )
    session, tools = _build_tools(
        (MemoryDocument("facts/index.md", "# Facts\n\n- Updated.\n"),),
        epoch=epoch,
    )
    link_tool = next(tool for tool in tools if tool.name == "link_memory")
    call = _tool_call(
        "link_memory",
        {
            "target_handle": target.item.context_handle,
            "source_path": "facts/index.md",
            "exact_text": "- Updated.",
            "occurrence": 1,
            "note": "The activity supports this update.",
            "expected_draft_revision": session.draft.draft_revision,
        },
    )

    first = await link_tool.execute(call, 1)
    replay = await link_tool.execute(call, 1)

    assert first.status == replay.status == "success"
    assert replay.output["local_ref_id"] == first.output["local_ref_id"]
    assert len(session.draft.links) == 1


@pytest.mark.asyncio
async def test_move_replay_is_idempotent() -> None:
    session, tools = _build_tools(
        (
            MemoryDocument("facts/index.md", "# Facts\n"),
            MemoryDocument("facts/old.md", "# Old\n"),
        )
    )
    move_tool = next(tool for tool in tools if tool.name == "move_memory_file")
    call = _tool_call(
        "move_memory_file",
        {
            "source_path": "facts/old.md",
            "destination_path": "facts/archive.md",
            "expected_draft_revision": session.draft.draft_revision,
        },
    )

    first = await move_tool.execute(call, 1)
    first_revision = session.draft.draft_revision
    replay = await move_tool.execute(call, 1)

    assert first.status == replay.status == "success"
    assert replay.output["draft_revision"] == first_revision
    assert {item.source_path for item in session.draft.documents} == {
        "facts/index.md",
        "facts/archive.md",
    }


@pytest.mark.asyncio
async def test_write_rejects_existing_file_directory_and_invalid_paths() -> None:
    _, tools = _build_tools((MemoryDocument("facts/index.md", "# Facts\n"),))
    write_tool = next(tool for tool in tools if tool.name == "write_file")

    existing = await write_tool.execute(
        _tool_call("write_file", {"path": "facts/index.md", "text": "replace\n"}), 1
    )
    directory = await write_tool.execute(
        _tool_call("write_file", {"path": "facts", "text": "file\n"}), 2
    )
    traversal = await write_tool.execute(
        _tool_call("write_file", {"path": "../facts.md", "text": "escape\n"}), 3
    )

    assert existing.output["error_code"] == "TARGET_EXISTS"
    assert directory.output["error_code"] == "TARGET_NOT_FILE"
    assert traversal.output["error_code"] == "PATH_DENIED"


@pytest.mark.asyncio
async def test_virtual_shell_inspects_checkpoint_and_rejects_mutation() -> None:
    _, tools = _build_tools(
        (
            MemoryDocument("facts/index.md", "# Facts\n"),
            MemoryDocument("facts/archive/old.md", "old\n"),
        )
    )
    shell = next(tool for tool in tools if tool.name == "shell")

    find = await shell.execute(_tool_call("shell", {"cmd": "find facts -type f"}), 1)
    listing = await shell.execute(_tool_call("shell", {"cmd": "ls facts"}), 2)
    pwd = await shell.execute(_tool_call("shell", {"cmd": "pwd"}), 3)
    exists = await shell.execute(
        _tool_call("shell", {"cmd": "test -f facts/index.md"}), 4
    )
    mutation = await shell.execute(
        _tool_call("shell", {"cmd": "touch facts/new.md"}), 5
    )
    skipped_missing = await shell.execute(
        _tool_call(
            "shell",
            {"cmd": "test -f facts/missing.md && ls missing-directory"},
        ),
        6,
    )

    assert find.output["stdout"] == "facts/archive/old.md\nfacts/index.md\n"
    assert listing.output["stdout"] == "facts/archive\nfacts/index.md\n"
    assert pwd.output["stdout"] == "/memory_draft\n"
    assert exists.output["exit_code"] == 0
    assert mutation.status == "error"
    assert mutation.output["error_code"] == "COMMAND_DENIED"
    assert skipped_missing.status == "success"
    assert skipped_missing.output["exit_code"] == 1


def _build_tools(
    documents: tuple[MemoryDocument, ...],
    *,
    policy: EditablePathPolicy = EditablePathPolicy(("facts/**",)),
    epoch: MemoryContextEpoch | None = None,
    readable_roots: tuple[ReadableFileRoot, ...] = (),
) -> tuple[MemoryDraftToolSession, tuple[ReactToolDefinition, ...]]:
    session = MemoryDraftToolSession(
        editable_policy=policy,
        draft=create_memory_draft(
            user_id="user-1",
            owner_node_id="fact-1",
            base_revision_id=None,
            documents=documents,
        ),
        memory_context=MemoryContextSession(
            user_id="user-1",
            run_id="run-1",
            epoch=epoch,
        ),
    )
    return session, build_local_memory_file_tools(
        editable_policy=policy,
        memory_session=session,
        readable_roots=readable_roots,
    )


def _document_text(session: MemoryDraftToolSession, path: str) -> str:
    return next(
        item.content for item in session.draft.documents if item.source_path == path
    )


def _tool_call(tool_name: str, args: dict[str, object]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id=tool_name,
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )
