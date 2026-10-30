from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from tests.unit.agents.action_agent.fixtures import create_state

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    memory_links,
)
from pantaray_agents.agents.action_agent.tools import GET_MEMORY_REFERENCE_TOOL
from pantaray_agents.local_runtime.memory_catalog import current_file_references as refs
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.current_files_index import (
    refresh_current_memory_index,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_memory_document,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MemoryFileConflictError,
    MemoryFiles,
    create_editable_memory_root,
    locked_memory_files,
    memory_files_revision,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    build_memory_context_epoch,
    merge_memory_context_epochs,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
    MemoryContextExpiredError,
    MemoryLinkValidationError,
    MemoryReferenceInputError,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    MemoryRunBinding,
    memory_run_binding_from_payload,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryBlockKind,
    MemoryContextEpoch,
    MemoryDocument,
    MemorySource,
    ResolvedMemoryLink,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    list_revision_fragments,
    require_node,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    follow_memory_reference,
)
from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
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
from pantaray_agents.tasks.types import MemoryUpdateJobPayload

from .migrated_db import prepare_test_database

PATH = "facts/project.md"
TEXT = "# Facts\n\n- Claim\n"


@dataclass
class State:
    connection: sqlite3.Connection
    files: MemoryFiles
    binding: MemoryRunBinding
    epoch: MemoryContextEpoch
    root: Path


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1000, migrations=load_default_migrations()
    )
    payload: MemoryUpdateJobPayload = {
        "job_id": "memory-job",
        "process_id": "memory-process",
        "user_id": "user-1",
        "enqueued_at": "2026-01-01T00:00:00Z",
        "short_insight_ids": ["insight-1"],
        "summary_ids": [],
        "action_terminals": [],
    }
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1000
    ) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO users(user_id, ui_language, created_at, updated_at) VALUES ('user-1', 'ja', '2026-01-01', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO agent_insights(insight_id, user_id, status, short_term_insight_data, facts, prompt_name, prompt_version, created_at, updated_at) VALUES ('insight-1', 'user-1', 'success', 'Evidence', '', 'insight', '1', '2026-01-01', '2026-01-01')"
            )
        # Older Fact records can still be active during cutover. A reference must
        # bind the fixed-file index, never the first matching historical path.
        with immediate_transaction(connection):
            for source_id in ("legacy-first", "legacy-last"):
                register_inline_memory_document(
                    connection=connection,
                    user_id="user-1",
                    source="fact",
                    source_record_id=source_id,
                    document=MemoryDocument(PATH, "Old fact"),
                )
            target = register_inline_memory_document(
                connection=connection,
                user_id="user-1",
                source="short_term_insight",
                source_record_id="insight-1",
                document=MemoryDocument("body.md", "Observed evidence"),
            )
        node = require_node(
            connection=connection, user_id="user-1", node_id=target.node_id
        )
        fragment = next(
            item
            for item in list_revision_fragments(
                connection=connection, user_id="user-1", revision_id=target.revision_id
            )
            if item.block_kind == "paragraph"
        )
        epoch = build_memory_context_epoch(
            run_id=payload["job_id"],
            user_id="user-1",
            visible=((node, fragment, "Evidence"),),
        )
        enqueue_local_job_with_connection(
            db_path=str(db_path),
            busy_timeout_ms=1000,
            request=build_local_memory_update_enqueue_request(payload),
        )
        claimed = claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1000,
            job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
            owner_user_id="user-1",
            claimed_by="worker",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        assert claimed is not None
        root = create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
        with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
            files.write(path=PATH, text=TEXT, expected_text=None)
            files.write(
                path="agent_experience/project.md",
                text="- Experience",
                expected_text=None,
            )
            with immediate_transaction(connection):
                refresh_current_memory_index(connection=connection, files=files)
            yield State(
                connection, files, memory_run_binding_from_payload(payload), epoch, root
            )


def _link(
    state: State,
    *,
    expected_revision: str | None = None,
    source_path: str = PATH,
    epoch: MemoryContextEpoch | None = None,
    note: str = 'Observed "evidence"',
) -> str:
    return refs.link_current_memory_file(
        connection=state.connection,
        files=state.files,
        binding=state.binding,
        epoch=state.epoch if epoch is None else epoch,
        target_handle=state.epoch.items[0].item.context_handle,
        source_path=source_path,
        exact_text="- Claim",
        occurrence=1,
        note=note,
        expected_revision=memory_files_revision(state.files.documents())
        if expected_revision is None
        else expected_revision,
    )


def _links(state: State) -> list[sqlite3.Row]:
    return state.connection.execute(
        "SELECT links.*, fragments.block_kind, revisions.artifact_root_path FROM memory_links AS links JOIN memory_fragments AS fragments ON fragments.user_id = links.user_id AND fragments.fragment_id = links.source_fragment_id JOIN memory_revisions AS revisions ON revisions.user_id = links.user_id AND revisions.revision_id = links.source_revision_id"
    ).fetchall()


def _refresh(state: State) -> None:
    with immediate_transaction(state.connection):
        refresh_current_memory_index(connection=state.connection, files=state.files)


def _follow(
    state: State,
    local_ref_id: str,
    pinned_revisions: Mapping[MemorySource, str | None] | None = None,
) -> ResolvedMemoryLink:
    # A ref read from a memory file or an index comes with no context handle.
    _, result = follow_memory_reference(
        connection=state.connection,
        epoch=build_memory_context_epoch(
            run_id="next-run", user_id="user-1", visible=()
        ),
        local_ref_id=local_ref_id,
        pinned_revisions=pinned_revisions,
        enqueue_repair_on_failure=False,
    )
    return result


def _assert_resolves(
    state: State, local_ref_id: str, expected_content: str = "Observed evidence"
) -> None:
    result = _follow(state, local_ref_id)
    assert result["target_content"] == expected_content
    assert result["reference_note"] == 'Observed "evidence"'


def test_link_and_unlink_update_current_files_and_the_same_index(state: State) -> None:
    before_count = state.connection.execute(
        "SELECT count(*) FROM memory_revisions"
    ).fetchone()[0]
    original_revision = memory_files_revision(state.files.documents())
    ref_id = _link(state)
    assert f"[[ref:{ref_id}" in state.files.read(PATH)
    assert _links(state)[0]["block_kind"] == "list_item"
    assert _links(state)[0]["artifact_root_path"].endswith("/files")
    _assert_resolves(state, ref_id)
    with pytest.raises(MemoryFileConflictError):
        _link(state, expected_revision=original_revision)
    assert len(extract_markdown_references(state.files.read(PATH))) == 1
    refs.unlink_current_memory_file(
        connection=state.connection,
        files=state.files,
        binding=state.binding,
        local_ref_id=ref_id,
        expected_revision=memory_files_revision(state.files.documents()),
    )
    assert state.files.read(PATH) == TEXT
    assert _links(state) == []
    assert state.files.read("agent_experience/project.md") == "- Experience"
    assert (
        state.connection.execute("SELECT count(*) FROM memory_revisions").fetchone()[0]
        == before_count
    )
    assert (
        state.connection.execute(
            "SELECT count(*) FROM memory_revision_intents"
        ).fetchone()[0]
        == 0
    )


def test_external_crlf_change_invalidates_raw_snapshot_before_linking(
    state: State,
) -> None:
    revision = memory_files_revision(state.files.documents())
    external = TEXT.replace("\n", "\r\n")
    (state.root / PATH).write_bytes(external.encode())
    with pytest.raises(MemoryFileConflictError):
        _link(state, expected_revision=revision)
    assert state.files.read(PATH) == external
    assert _links(state) == []
    ref_id = _link(state)
    assert state.files.read(PATH).startswith("# Facts\n\n- Claim [[ref:")
    _assert_resolves(state, ref_id)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE jobs SET status = 'canceled' WHERE job_id = 'memory-job'",
        "UPDATE processes SET status = 'failed' WHERE process_id = 'memory-process'",
        "UPDATE processes SET current_job_id = NULL WHERE process_id = 'memory-process'",
    ],
)
@pytest.mark.parametrize("operation", ["link", "unlink"])
def test_unclaimed_writer_cannot_change_files(
    state: State, sql: str, operation: str
) -> None:
    ref_id = _link(state) if operation == "unlink" else None
    before = state.files.read(PATH)
    with immediate_transaction(state.connection):
        state.connection.execute(sql)
    with pytest.raises(MemoryCatalogIntegrityError):
        if ref_id is None:
            _link(state)
        else:
            refs.unlink_current_memory_file(
                connection=state.connection,
                files=state.files,
                binding=state.binding,
                local_ref_id=ref_id,
                expected_revision=memory_files_revision(state.files.documents()),
            )
    assert state.files.read(PATH) == before
    assert len(_links(state)) == (0 if ref_id is None else 1)


def test_memory_agent_cannot_link_agent_experience(state: State) -> None:
    with pytest.raises(MemoryLinkValidationError, match="only facts and insights"):
        _link(state, source_path="agent_experience/project.md")
    assert state.files.read("agent_experience/project.md") == "- Experience"
    assert _links(state) == []


def test_multiline_note_is_rejected_before_mapping_or_file_write(state: State) -> None:
    with pytest.raises(MemoryLinkValidationError, match="single-line"):
        _link(state, note="Evidence\n")
    assert state.files.read(PATH) == TEXT
    assert _links(state) == []


def test_another_runs_epoch_cannot_authorize_a_reference(state: State) -> None:
    with pytest.raises(MemoryContextExpiredError):
        _link(state, epoch=replace(state.epoch, run_id="previous-run"))
    assert state.files.read(PATH) == TEXT
    assert _links(state) == []


def _file_epoch(state: State, path: str, kind: MemoryBlockKind) -> MemoryContextEpoch:
    row = state.connection.execute(
        "SELECT node_id, revision_id, fragment_id FROM memory_fragments JOIN memory_revisions USING (user_id, revision_id) WHERE source_path = ? AND block_kind = ? AND artifact_root_path LIKE '%/files'",
        (path, kind),
    ).fetchone()
    node = require_node(
        connection=state.connection, user_id="user-1", node_id=row["node_id"]
    )
    fragment = next(
        item
        for item in list_revision_fragments(
            connection=state.connection,
            user_id="user-1",
            revision_id=row["revision_id"],
        )
        if item.fragment_id == row["fragment_id"]
    )
    return build_memory_context_epoch(
        run_id=state.binding.job_id,
        user_id="user-1",
        visible=((node, fragment, "Claim"),),
    )


def test_deleted_target_handle_is_rejected_after_current_index_refresh(
    state: State,
) -> None:
    epoch = _file_epoch(state, PATH, "list_item")
    (state.root / PATH).write_text("# Facts\n\n- Changed claim\n")
    with pytest.raises(MemoryLinkValidationError, match="search again"):
        refs.link_current_memory_file(
            connection=state.connection,
            files=state.files,
            binding=state.binding,
            epoch=epoch,
            target_handle=epoch.items[0].item.context_handle,
            source_path=PATH,
            exact_text="- Changed claim",
            occurrence=1,
            note="evidence",
            expected_revision=memory_files_revision(state.files.documents()),
        )
    assert _links(state) == []
    assert "[[ref:" not in state.files.read(PATH)


def test_failed_file_write_leaves_no_visible_tag_and_next_refresh_prunes_mapping(
    state: State, monkeypatch
) -> None:
    def fail_write(self, **kwargs):
        raise OSError("file replacement failed")

    with monkeypatch.context() as failure:
        failure.setattr(MemoryFiles, "write", fail_write)
        with pytest.raises(OSError, match="replacement failed"):
            _link(state)
    assert state.files.read(PATH) == TEXT
    assert len(_links(state)) == 1
    _refresh(state)
    assert _links(state) == []


@pytest.mark.parametrize("operation", ["link", "unlink"])
def test_index_failure_after_file_write_recovers_from_current_text(
    state: State, monkeypatch, operation: str
) -> None:
    ref_id = _link(state) if operation == "unlink" else None
    calls = 0
    original = refs.refresh_current_memory_index

    def fail_second_index(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("index write failed")
        return original(**kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(refs, "refresh_current_memory_index", fail_second_index)
        with pytest.raises(sqlite3.OperationalError, match="index write failed"):
            if operation == "link":
                _link(state)
            else:
                assert ref_id is not None
                refs.unlink_current_memory_file(
                    connection=state.connection,
                    files=state.files,
                    binding=state.binding,
                    local_ref_id=ref_id,
                    expected_revision=memory_files_revision(state.files.documents()),
                )
    _refresh(state)
    if operation == "link":
        saved_ref = extract_markdown_references(state.files.read(PATH))[0].local_ref_id
        assert _links(state)[0]["block_kind"] == "list_item"
        _assert_resolves(state, saved_ref)
    else:
        assert state.files.read(PATH) == TEXT
        assert _links(state) == []


def test_cancellation_between_mapping_commit_and_file_write_wins(
    state: State, monkeypatch
) -> None:
    first = True

    @contextmanager
    def cancel_after_commit(connection):
        nonlocal first
        with immediate_transaction(connection):
            yield
        if first:
            first = False
            with immediate_transaction(connection):
                connection.execute(
                    "UPDATE jobs SET status = 'canceled' WHERE job_id = 'memory-job'"
                )

    with monkeypatch.context() as cancellation:
        cancellation.setattr(refs, "immediate_transaction", cancel_after_commit)
        with pytest.raises(MemoryCatalogIntegrityError):
            _link(state)
    assert state.files.read(PATH) == TEXT
    _refresh(state)
    assert _links(state) == []


def test_foreign_user_file_handle_cannot_be_written(
    state: State, tmp_path: Path
) -> None:
    create_editable_memory_root(artifact_root=tmp_path, user_id="user-2")
    with locked_memory_files(artifact_root=tmp_path, user_id="user-2") as foreign_files:
        foreign_files.write(path=PATH, text=TEXT, expected_text=None)
        with pytest.raises(MemoryCatalogIntegrityError, match="another user"):
            _link(replace(state, files=foreign_files))
        assert foreign_files.read(PATH) == TEXT
    assert _links(state) == []


def test_external_change_between_mapping_and_file_write_is_not_overwritten(
    state: State, monkeypatch
) -> None:
    first = True

    @contextmanager
    def edit_after_commit(connection):
        nonlocal first
        with immediate_transaction(connection):
            yield
        if first:
            first = False
            (state.root / PATH).write_text("- External change\n")

    with monkeypatch.context() as external:
        external.setattr(refs, "immediate_transaction", edit_after_commit)
        with pytest.raises(MemoryFileConflictError):
            _link(state)
    assert state.files.read(PATH) == "- External change\n"
    _refresh(state)
    assert _links(state) == []


def test_memory_agent_cannot_unlink_migrated_agent_experience_reference(
    state: State,
) -> None:
    path = "agent_experience/project.md"
    text = '- Experience [[ref:ref_migrated note:"original evidence"]]\n'
    with immediate_transaction(state.connection):
        state.connection.execute(
            """INSERT INTO memory_links(user_id, source_revision_id, local_ref_id, source_fragment_id, target_fragment_id, reference_note, created_at)
            SELECT user_id, revision_id, 'ref_migrated', fragment_id, ?, 'original evidence', '2026-01-01'
            FROM memory_fragments WHERE source_path = ? AND block_kind = 'document_root'""",
            (state.epoch.items[0].fragment_id, path),
        )
    state.files.write(path=path, text=text, expected_text="- Experience")
    _refresh(state)
    with pytest.raises(MemoryLinkValidationError, match="only facts and insights"):
        refs.unlink_current_memory_file(
            connection=state.connection,
            files=state.files,
            binding=state.binding,
            local_ref_id="ref_migrated",
            expected_revision=memory_files_revision(state.files.documents()),
        )
    assert state.files.read(path) == text
    assert len(_links(state)) == 1


def test_changed_document_handle_requires_reobservation_before_new_link(
    state: State,
) -> None:
    path = "agent_experience/project.md"
    prior = _file_epoch(state, path, "document_root")
    (state.root / path).write_text("- Updated experience\n")
    with pytest.raises(MemoryLinkValidationError, match="search again"):
        _link(replace(state, epoch=prior))
    assert _links(state) == []
    assert state.files.read(PATH) == TEXT
    _refresh(state)
    fresh = _file_epoch(state, path, "document_root")
    epoch = merge_memory_context_epochs(base=prior, added=fresh)
    assert epoch.items[0].item.context_handle == prior.items[0].item.context_handle
    ref_id = _link(replace(state, epoch=epoch))
    _assert_resolves(state, ref_id, "- Updated experience\n")


def test_ref_id_duplicated_across_current_categories_is_ambiguous(
    state: State,
) -> None:
    ref_id = _link(state)
    original = _links(state)[0]
    path = "insights/project.md"
    state.files.write(path=path, text=state.files.read(PATH), expected_text=None)
    _refresh(state)
    with immediate_transaction(state.connection):
        state.connection.execute(
            """INSERT INTO memory_links(user_id, source_revision_id, local_ref_id, source_fragment_id, target_fragment_id, reference_note, created_at)
            SELECT user_id, revision_id, ?, fragment_id, ?, ?, '2026-01-01'
            FROM memory_fragments WHERE source_path = ? AND block_kind = 'list_item'""",
            (ref_id, original["target_fragment_id"], original["reference_note"], path),
        )
    before = state.files.documents()
    with pytest.raises(MemoryLinkValidationError, match="ambiguous"):
        refs.unlink_current_memory_file(
            connection=state.connection,
            files=state.files,
            binding=state.binding,
            local_ref_id=ref_id,
            expected_revision=memory_files_revision(before),
        )
    assert state.files.documents() == before
    assert len(_links(state)) == 2
    with pytest.raises(MemoryReferenceInputError, match="ambiguous"):
        _follow(state, ref_id)


def test_ref_resolves_only_in_the_revisions_the_run_sees(state: State) -> None:
    ref_id = _link(state)
    revision_id = _links(state)[0]["source_revision_id"]

    pinned = _follow(state, ref_id, {"fact": revision_id})
    assert pinned["target_content"] == "Observed evidence"
    # A run pinned before facts had a head does not see the ref.
    with pytest.raises(MemoryReferenceInputError, match="not a ref in current memory"):
        _follow(state, ref_id, {"fact": None})
    with pytest.raises(MemoryReferenceInputError, match="not a ref in current memory"):
        _follow(state, "ref_" + "0" * 24)


@pytest.mark.asyncio
async def test_action_follows_a_ref_it_read_without_memory_search(
    state: State, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = _link(state)
    action_state = create_state()
    action_state["user_id"] = "user-1"
    monkeypatch.setattr(
        memory_links,
        "read_local_runtime_db_config",
        lambda: (tmp_path / "runtime.db", 1000),
    )

    result = await memory_links.run_get_memory_reference_tool(
        step_id="step-ref",
        tool_def=GET_MEMORY_REFERENCE_TOOL,
        args={"local_ref_id": ref_id},
        state=action_state,
    )

    assert isinstance(result.output, dict)
    assert result.output["target_content"] == "Observed evidence"
    epoch = deserialize_memory_epoch(action_state["memory_context_epoch"])
    # link_memory can target the resolved fragment by the returned handle.
    assert [item.item.context_handle for item in epoch.items] == [
        result.output["target_context_handle"]
    ]


def test_self_reference_tracks_the_current_file_after_its_target_block_changes(
    state: State,
) -> None:
    epoch = _file_epoch(state, PATH, "list_item")
    ref_id = _link(replace(state, epoch=epoch))
    target = state.connection.execute(
        "SELECT source_path, block_kind FROM memory_fragments WHERE fragment_id = ?",
        (_links(state)[0]["target_fragment_id"],),
    ).fetchone()
    assert tuple(target) == (PATH, "document_root")
    _assert_resolves(state, ref_id, state.files.read(PATH))
