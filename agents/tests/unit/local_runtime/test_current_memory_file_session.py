from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.current_files_index import (
    refresh_current_memory_index,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MemoryFileConflictError,
    create_editable_memory_root,
    locked_memory_files,
    memory_files_revision,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
    MemoryLinkValidationError,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    memory_run_binding_from_payload,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_types import LOCAL_MEMORY_UPDATE_JOB_TYPE
from pantaray_agents.local_runtime.runtime.memory_update_queue import (
    build_local_memory_update_enqueue_request,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    current_file_session as current,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.registry import (
    LocalMemoryFileEditorTools,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.types import MemoryUpdateJobPayload
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope
from pantaray_agents.tools.memory.retrieval import MemoryContextSession

from .migrated_db import prepare_test_database

PATH = "facts/project.md"
TEXT = "- Original\n"


@pytest.fixture
def session(tmp_path: Path) -> current.CurrentMemoryFileSession:
    db = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db, busy_timeout_ms=1000, migrations=load_default_migrations()
    )
    payload: MemoryUpdateJobPayload = {
        "job_id": "job",
        "process_id": "process",
        "user_id": "user",
        "enqueued_at": "2026-01-01T00:00:00Z",
        "short_insight_ids": ["insight"],
        "summary_ids": [],
        "action_terminals": [],
    }
    with (
        open_memory_catalog_connection(db_path=db, busy_timeout_ms=1000) as connection,
        immediate_transaction(connection),
    ):
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) VALUES ('user', 'ja', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO agent_insights(insight_id, user_id, status, short_term_insight_data, facts, prompt_name, prompt_version, created_at, updated_at) VALUES ('insight', 'user', 'success', 'Evidence', '', 'insight', '1', '2026-01-01', '2026-01-01')"
        )
    enqueue_local_job_with_connection(
        db_path=str(db),
        busy_timeout_ms=1000,
        request=build_local_memory_update_enqueue_request(payload),
    )
    assert claim_next_pending_job(
        db_path=str(db),
        busy_timeout_ms=1000,
        job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
        owner_user_id="user",
        claimed_by="worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    create_editable_memory_root(artifact_root=tmp_path, user_id="user")
    with locked_memory_files(artifact_root=tmp_path, user_id="user") as files:
        files.write(path=PATH, text=TEXT, expected_text=None)
        files.write(
            path="agent_experience/project.md", text="- Lesson", expected_text=None
        )
        with (
            open_memory_catalog_connection(
                db_path=db, busy_timeout_ms=1000
            ) as connection,
            immediate_transaction(connection),
        ):
            refresh_current_memory_index(connection=connection, files=files)
    return current.CurrentMemoryFileSession(
        db,
        tmp_path,
        1000,
        memory_run_binding_from_payload(payload),
        MemoryContextSession("user", "job"),
    )


def _call(name: str, args: dict[str, JSONValue]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=name,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=name, reason=None, args=args),
    )


def _tools(session: current.CurrentMemoryFileSession) -> LocalMemoryFileEditorTools:
    return LocalMemoryFileEditorTools(session.editable_policy, session)


def _text(session: current.CurrentMemoryFileSession, path: str = PATH) -> str:
    return next(
        document.content
        for document in session.draft.documents
        if document.source_path == path
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["apply_patch", "write_file"])
async def test_other_file_change_before_lock_rejects_the_unobserved_tree(
    session, monkeypatch, operation
):
    tools = _tools(session)
    mutation = current.CurrentMemoryFileSession._mutation

    @contextmanager
    def edit_before_lock(self, expected_revision):
        with locked_memory_files(
            artifact_root=self.artifact_root, user_id=self.binding.user_id
        ) as files:
            files.write(
                path="agent_experience/project.md",
                text="- Updated by Action",
                expected_text="- Lesson",
            )
        with mutation(self, expected_revision) as files:
            yield files

    if operation == "apply_patch":
        await tools.read_file(
            _call("read_file", {"root": "memory_draft", "path": PATH}), 1
        )
        args = {
            "path": PATH,
            "chunks": [
                {
                    "lines": [
                        {"op": "remove", "text": "- Original"},
                        {"op": "add", "text": "- Saved"},
                    ]
                }
            ],
        }
    else:
        args = {"path": "facts/new.md", "text": "- Saved\n"}
    with monkeypatch.context() as patch:
        patch.setattr(current.CurrentMemoryFileSession, "_mutation", edit_before_lock)
        result = await getattr(tools, operation)(_call(operation, args), 2)
    assert result.output["error_code"] == "MEMORY_DRAFT_REVISION_STALE"
    assert _text(session) == TEXT
    assert _text(session, "agent_experience/project.md") == "- Updated by Action"
    assert "facts/new.md" not in {
        document.source_path for document in session.draft.documents
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["apply_patch", "write_file"])
async def test_saved_hash_does_not_authorize_deleting_an_unseen_external_edit(
    session, monkeypatch, operation
):
    tools = _tools(session)
    path = PATH if operation == "apply_patch" else "facts/new.md"
    mutation = current.CurrentMemoryFileSession._mutation
    saved_revision = ""

    @contextmanager
    def edit_after_unlock(self, expected_revision):
        nonlocal saved_revision
        with mutation(self, expected_revision) as files:
            yield files
            saved_revision = memory_files_revision(files.documents())
        with locked_memory_files(
            artifact_root=self.artifact_root, user_id=self.binding.user_id
        ) as files:
            files.write(path=path, text="- External", expected_text="- Saved\n")

    if operation == "apply_patch":
        await tools.read_file(
            _call("read_file", {"root": "memory_draft", "path": path}), 1
        )
        args = {
            "path": path,
            "chunks": [
                {
                    "lines": [
                        {"op": "remove", "text": "- Original"},
                        {"op": "add", "text": "- Saved"},
                    ]
                }
            ],
        }
    else:
        args = {"path": path, "text": "- Saved\n"}
    with monkeypatch.context() as patch:
        patch.setattr(current.CurrentMemoryFileSession, "_mutation", edit_after_unlock)
        result = await getattr(tools, operation)(_call(operation, args), 2)
    assert result.output["status"] == "success"
    assert result.output["draft_revision"] == saved_revision
    assert saved_revision != session.draft.draft_revision
    deleted = await session.delete_file(
        _call(
            "delete_memory_file",
            {"path": path, "expected_draft_revision": result.output["draft_revision"]},
        ),
        3,
    )
    assert deleted.output["error_code"] == "MEMORY_DRAFT_REVISION_STALE"
    assert _text(session, path) == "- External"


@pytest.mark.asyncio
async def test_existing_tools_save_patch_and_reject_unread_external_change(session):
    tools = _tools(session)
    patch = _call(
        "apply_patch",
        {
            "path": PATH,
            "chunks": [
                {
                    "lines": [
                        {"op": "remove", "text": "- Original"},
                        {"op": "add", "text": "- Updated"},
                    ]
                }
            ],
        },
    )
    assert (await tools.apply_patch(patch, 1)).output["status"] == "needs_read"
    result = await tools.apply_patch(patch, 2)
    assert result.output["status"] == "success"
    assert _text(session) == "- Updated\n"
    stale = _call(
        "apply_patch",
        {
            "path": PATH,
            "chunks": [
                {
                    "lines": [
                        {"op": "remove", "text": "- Updated"},
                        {"op": "add", "text": "- Overwritten"},
                    ]
                }
            ],
        },
    )
    await tools.read_file(_call("read_file", {"root": "memory_draft", "path": PATH}), 3)
    with locked_memory_files(
        artifact_root=session.artifact_root, user_id="user"
    ) as files:
        files.write(path=PATH, text="- External\r\n", expected_text="- Updated\n")
    assert (await tools.apply_patch(stale, 4)).output["status"] == "needs_read"
    assert _text(session) == "- External\r\n"
    with pytest.raises(MemoryFileConflictError):
        session.replace_document(
            expected_revision=session.draft.draft_revision,
            path=PATH,
            content="- Stale",
            expected_text="- Updated\n",
        )


@pytest.mark.asyncio
async def test_create_move_delete_refresh_current_index_and_allow_empty_category(
    session,
):
    tools = _tools(session)
    assert (
        await tools.write_file(
            _call("write_file", {"path": "insights/lesson.MD", "text": "- Learned"}), 1
        )
    ).output["status"] == "success"
    result = await session.move_file(
        _call(
            "move_memory_file",
            {
                "source_path": PATH,
                "destination_path": "facts/renamed.md",
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        2,
    )
    assert result.output["status"] == "success"
    result = await session.delete_file(
        _call(
            "delete_memory_file",
            {
                "path": "facts/renamed.md",
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        3,
    )
    assert result.output["status"] == "success"
    with open_memory_catalog_connection(
        db_path=session.db_path, busy_timeout_ms=1000
    ) as connection:
        paths = {
            row[0]
            for row in connection.execute("SELECT source_path FROM memory_fragments")
        }
    assert paths == {"insights/lesson.MD", "agent_experience/project.md"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("move_file", {"source_path": PATH, "destination_path": "facts/moved.md"}),
        ("delete_file", {"path": PATH}),
    ],
)
async def test_move_delete_reject_stale_tree(session, method, args):
    revision = session.draft.draft_revision
    session.append_document(
        expected_revision=session.draft.draft_revision,
        path="insights/new.md",
        content="- New",
    )
    result = await getattr(session, method)(
        _call(method, {**args, "expected_draft_revision": revision}), 1
    )
    assert result.output["error_code"] == "MEMORY_DRAFT_REVISION_STALE"
    assert _text(session) == TEXT


@pytest.mark.asyncio
async def test_canceled_job_cannot_save_and_previous_save_survives(session):
    session.replace_document(
        expected_revision=session.draft.draft_revision,
        path=PATH,
        content="- Saved",
        expected_text=TEXT,
    )
    with (
        open_memory_catalog_connection(
            db_path=session.db_path, busy_timeout_ms=1000
        ) as connection,
        immediate_transaction(connection),
    ):
        connection.execute("UPDATE jobs SET status = 'canceled' WHERE job_id = 'job'")
    with pytest.raises(MemoryCatalogIntegrityError):
        session.replace_document(
            expected_revision=session.draft.draft_revision,
            path=PATH,
            content="- Unauthorized",
            expected_text="- Saved",
        )
    result = await _tools(session).write_file(
        _call("write_file", {"path": "insights/new.md", "text": "- Unauthorized"}), 2
    )
    assert result.output["error_code"] == "MEMORY_CATALOG_INTEGRITY"
    assert _text(session) == "- Saved"


@pytest.mark.asyncio
async def test_agent_experience_read_only_and_cross_category_move_rejected(session):
    tools = _tools(session)
    read = await tools.read_file(
        _call(
            "read_file", {"root": "memory_draft", "path": "agent_experience/project.md"}
        ),
        1,
    )
    assert read.output["text"] == "- Lesson"
    write = await tools.write_file(
        _call("write_file", {"path": "agent_experience/new.md", "text": "- Forbidden"}),
        2,
    )
    assert write.output["error_code"] == "PATH_DENIED"
    move = await session.move_file(
        _call(
            "move_memory_file",
            {
                "source_path": PATH,
                "destination_path": "insights/moved.md",
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        3,
    )
    assert move.output["status"] == "error"
    assert _text(session) == TEXT


@pytest.mark.parametrize(
    "replacement",
    [
        '- Original [[ref:ref_new note:"invented"]]',
        '- Original [[ref:ref_old note:"changed"]]',
        '- Original [[ref:ref_old note:"note"]] [[ref:ref_old note:"note"]]',
        '- Original [[ref:ref_old note:"note"]',
    ],
)
def test_patch_cannot_forge_duplicate_or_damage_tags(session, replacement):
    original = '- Original [[ref:ref_old note:"note"]]'
    with locked_memory_files(
        artifact_root=session.artifact_root, user_id="user"
    ) as files:
        files.write(path=PATH, text=original, expected_text=TEXT)
    with pytest.raises(MemoryLinkValidationError):
        session.replace_document(
            expected_revision=session.draft.draft_revision,
            path=PATH,
            content=replacement,
            expected_text=original,
        )
    assert _text(session) == original


def test_patch_preserves_external_inert_tags_and_can_remove_complete_tag(session):
    original = (
        '- Original [[ref:ref_external note:"note"]] [[ref:ref_external note:"note"]]'
    )
    with locked_memory_files(
        artifact_root=session.artifact_root, user_id="user"
    ) as files:
        files.write(path=PATH, text=original, expected_text=TEXT)
    updated = original.replace("Original", "Edited")
    session.replace_document(
        expected_revision=session.draft.draft_revision,
        path=PATH,
        content=updated,
        expected_text=original,
    )
    session.replace_document(
        expected_revision=session.draft.draft_revision,
        path=PATH,
        content="- Edited",
        expected_text=updated,
    )
    assert _text(session) == "- Edited"


@pytest.mark.asyncio
async def test_index_failure_reports_error_but_saved_file_can_rebuild_index(
    session, monkeypatch
):
    def fail_index(**kwargs):
        raise sqlite3.OperationalError("injected index failure")

    with monkeypatch.context() as patch:
        patch.setattr(current, "refresh_current_memory_index", fail_index)
        result = await _tools(session).write_file(
            _call("write_file", {"path": "insights/recover.md", "text": "- Durable"}), 1
        )
    assert result.output["error_code"] == "DATABASE_FAILED"
    assert _text(session, "insights/recover.md") == "- Durable"
    with (
        locked_memory_files(
            artifact_root=session.artifact_root, user_id="user"
        ) as files,
        open_memory_catalog_connection(
            db_path=session.db_path, busy_timeout_ms=1000
        ) as connection,
        immediate_transaction(connection),
    ):
        refresh_current_memory_index(connection=connection, files=files)
        assert connection.execute(
            "SELECT 1 FROM memory_fragments WHERE source_path = 'insights/recover.md' AND content_text = '- Durable'"
        ).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("read_file", {"root": "memory_draft", "path": PATH}),
        ("search_files", {"root": "memory_draft", "query": "Original"}),
        ("shell", {"cmd": "ls"}),
        ("write_file", {"path": "insights/new.md", "text": "- New"}),
    ],
)
async def test_missing_root_returns_io_error_without_recreating_files(
    session, method, args
):
    root = session.artifact_root / "memory_catalog/users/user/files"
    root.rename(root.with_name("moved-away"))
    result = await getattr(_tools(session), method)(_call(method, args), 1)
    assert result.output["error_code"] == "IO_FAILED"
    assert not root.exists()


@pytest.mark.asyncio
async def test_registered_reference_tools_save_and_remove_the_tag(session):
    from pantaray_agents.local_runtime.memory_catalog.epoch import (
        build_memory_context_epoch,
    )
    from pantaray_agents.local_runtime.memory_catalog.repository import (
        list_revision_fragments,
        require_node,
    )

    with open_memory_catalog_connection(
        db_path=session.db_path, busy_timeout_ms=1000
    ) as connection:
        row = connection.execute(
            "SELECT node_id, current_revision_id FROM memory_nodes WHERE source_type = 'fact'"
        ).fetchone()
        node = require_node(
            connection=connection, user_id="user", node_id=row["node_id"]
        )
        fragments = list_revision_fragments(
            connection=connection,
            user_id="user",
            revision_id=row["current_revision_id"],
        )
        fragment = next(item for item in fragments if item.block_kind == "list_item")
    session.memory_context.epoch = build_memory_context_epoch(
        run_id="job", user_id="user", visible=((node, fragment, "observed"),)
    )
    definitions = {tool.name: tool for tool in _tools(session).definitions()}
    linked = await definitions["link_memory"].execute(
        _call(
            "link_memory",
            {
                "source_path": PATH,
                "exact_text": "- Original",
                "occurrence": 1,
                "note": "observed",
                "target_handle": session.memory_context.epoch.items[
                    0
                ].item.context_handle,
                "expected_draft_revision": session.draft.draft_revision,
            },
        ),
        1,
    )
    assert linked.output["status"] == "success"
    ref_id = linked.output["local_ref_id"]
    assert f"[[ref:{ref_id}" in _text(session)
    removed = await definitions["unlink_memory"].execute(
        _call(
            "unlink_memory",
            {
                "local_ref_id": ref_id,
                "expected_draft_revision": linked.output["draft_revision"],
            },
        ),
        2,
    )
    assert removed.output["status"] == "success"
    assert "[[ref:" not in _text(session)
