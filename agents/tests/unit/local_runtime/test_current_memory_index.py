from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from time import process_time

import pytest

from pantaray_agents.local_runtime.memory_catalog.current_files_index import (
    refresh_current_memory_index,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    EDITABLE_MEMORY_ROOTS,
    create_editable_memory_root,
    locked_memory_files,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    ensure_user_embedding_generation,
    generation_entry_table,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_work import (
    claim_due_embeddings,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
    build_memory_context_epoch,
    merge_memory_context_epochs,
    resolve_context_handle,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
    MemoryReferenceNotFoundError,
)
from pantaray_agents.local_runtime.memory_catalog.fragments import (
    locate_memory_references,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryFragment,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
    insert_revision,
    list_revision_fragments,
    load_latest_active_node_by_source,
    load_revision,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    follow_memory_reference,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    commit_embedding_projection_batch,
    encode_embedding,
    store_embedding_success,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.memory_embeddings import MEMORY_EMBEDDING_DIMENSIONS

from .embedding_test_support import TEST_EMBEDDING_SPECIFICATION
from .migrated_db import prepare_test_database


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1000, migrations=load_default_migrations()
    )
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, 1000)
        connection.row_factory = sqlite3.Row
        connection.executemany(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) VALUES (?, 'ja', '2026-01-01', '2026-01-01')",
            ((user,) for user in ("user-1", "user-2")),
        )
        connection.commit()
        yield connection


def _fragments(
    connection: sqlite3.Connection, path: str, user: str = "user-1"
) -> tuple[MemoryFragment, ...]:
    row = connection.execute(
        "SELECT revision_id FROM memory_fragments WHERE user_id = ? AND source_path = ? LIMIT 1",
        (user, path),
    ).fetchone()
    assert row is not None
    return tuple(
        item
        for item in list_revision_fragments(
            connection=connection, user_id=user, revision_id=str(row[0])
        )
        if item.source_path == path
    )


def test_external_edits_update_existing_index_and_keep_unchanged_embeddings(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    root = create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
    generation = ensure_user_embedding_generation(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1000,
        user_id="user-1",
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    path = "facts/project.md"
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.write(
            path=path, text="# Old\n\noldterm\n\nunchangedterm", expected_text=None
        )
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
    before = _fragments(connection, path)
    old = next(item for item in before if item.content_text == "oldterm")
    kept = next(item for item in before if item.content_text == "unchangedterm")
    document = next(item for item in before if item.block_kind == "document_root")
    with immediate_transaction(connection):
        for item in (old, kept):
            store_embedding_success(
                connection,
                user_id="user-1",
                generation_id=generation.generation_id,
                fragment_id=item.fragment_id,
                chunk_index=0,
                fragment_content_sha256=item.content_sha256,
                vector=(1.0, *((0.0,) * (MEMORY_EMBEDDING_DIMENSIONS - 1))),
                created_at="2026-01-01",
            )
        connection.execute(
            "UPDATE memory_revisions SET profile_brief = 'oldterm' WHERE revision_id = ?",
            (document.revision_id,),
        )
    (root / path).write_text("# New\n\nnewterm\n\nunchangedterm")
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
    after = _fragments(connection, path)
    current = next(item for item in after if item.block_kind == "document_root")
    assert current.fragment_id == document.fragment_id
    assert current.revision_id == document.revision_id
    assert replace(kept, heading_path="New") in after
    for query, expected in (("oldterm", False), ("newterm", True)):
        hits = connection.execute(
            "SELECT 1 FROM memory_fragments_fts WHERE memory_fragments_fts MATCH ?",
            (query,),
        ).fetchall()
        assert bool(hits) is expected
    entries = generation_entry_table(generation.generation_id)
    assert [
        row[0] for row in connection.execute(f"SELECT fragment_id FROM {entries}")
    ] == [kept.fragment_id]
    assert (
        connection.execute(
            "SELECT 1 FROM memory_embedding_work WHERE fragment_id = ?",
            (old.fragment_id,),
        ).fetchone()
        is None
    )
    new = next(item for item in after if item.content_text == "newterm")
    assert (
        connection.execute(
            "SELECT state FROM memory_embedding_work WHERE fragment_id = ?",
            (new.fragment_id,),
        ).fetchone()[0]
        == "pending"
    )
    for table, expected in (
        ("memory_revisions", 3),
        ("memory_revision_parents", 0),
        ("memory_revision_intents", 0),
    ):
        assert (
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            == expected
        )
    assert (
        connection.execute(
            "SELECT profile_brief FROM memory_revisions WHERE revision_id = ?",
            (document.revision_id,),
        ).fetchone()[0]
        is None
    )
    assert [
        item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()
    ] == [path]


def test_a_block_edited_while_it_is_embedded_is_not_indexed(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    # Search no longer re-checks the index, so the writer is the one place a
    # vector computed from text that has since changed must be turned away.
    db_path = tmp_path / "runtime.db"
    root = create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
    generation = ensure_user_embedding_generation(
        db_path=db_path,
        busy_timeout_ms=1000,
        user_id="user-1",
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    path = "facts/project.md"
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.write(path=path, text="oldterm", expected_text=None)
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
    claimed = claim_due_embeddings(
        db_path=db_path,
        busy_timeout_ms=1000,
        user_id="user-1",
        generation_id=generation.generation_id,
        now="2026-01-01T00:00:00Z",
    )
    assert [item.content_text for item in claimed] == ["oldterm"]
    (root / path).write_text("newterm")
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)

    indexed = commit_embedding_projection_batch(
        db_path=db_path,
        busy_timeout_ms=1000,
        documents=claimed,
        encoded_vectors=(
            encode_embedding(
                (1.0, *((0.0,) * (MEMORY_EMBEDDING_DIMENSIONS - 1))),
                dimensions=MEMORY_EMBEDDING_DIMENSIONS,
            ),
        ),
        created_at="2026-01-01T00:00:01Z",
    )

    entries = generation_entry_table(generation.generation_id)
    assert indexed == 0
    assert connection.execute(f"SELECT COUNT(*) FROM {entries}").fetchone()[0] == 0
    new = next(
        item
        for item in _fragments(connection, path)
        if item.block_kind != "document_root"
    )
    assert new.content_text == "newterm"
    assert (
        connection.execute(
            "SELECT state FROM memory_embedding_work WHERE fragment_id = ?",
            (new.fragment_id,),
        ).fetchone()[0]
        == "pending"
    )


@pytest.mark.parametrize("target_changes", [True, False])
@pytest.mark.parametrize("broken", ["deleted", "duplicate", "note"])
@pytest.mark.parametrize(
    ("source_root", "target_root"),
    [("insights", "facts"), ("facts", "agent_experience")],
)
def test_references_follow_current_files_and_drop_invalid_mappings(
    tmp_path: Path,
    connection: sqlite3.Connection,
    broken: str,
    target_changes: bool,
    source_root: str,
    target_root: str,
) -> None:
    create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
    source_path, target_path = f"{source_root}/source.md", f"{target_root}/target.md"
    tag = '[[ref:r1 note:"evidence"]]'
    original_target = "# Original\n\noldtarget"
    updated_target = "# Renamed\n\n" + ("newtarget" if target_changes else "oldtarget")
    source_text = f"# Observation\r\n\r\nKnown {tag}\r\n"
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.write(path=source_path, text=source_text, expected_text=None)
        files.write(path=target_path, text=original_target, expected_text=None)
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        source = next(
            item
            for item in _fragments(connection, source_path)
            if item.block_kind == "paragraph"
        )
        target = next(
            item
            for item in _fragments(connection, target_path)
            if item.block_kind == "paragraph"
        )
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO memory_links VALUES (?, ?, 'r1', ?, ?, 'evidence', '2026-01-01')",
                ("user-1", source.revision_id, source.fragment_id, target.fragment_id),
            )
        updated_source = source_text.replace(
            "Observation", "Updated\r\n\r\nIntroduction"
        )
        files.write(path=source_path, text=updated_source, expected_text=source_text)
        files.write(
            path=target_path, text=updated_target, expected_text=original_target
        )
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        source = next(
            item
            for item in _fragments(connection, source_path)
            if item.block_kind == "paragraph" and tag in item.content_text
        )
        target = next(
            item
            for item in _fragments(connection, target_path)
            if item.block_kind == ("document_root" if target_changes else "paragraph")
        )
        link = connection.execute(
            "SELECT source_fragment_id, target_fragment_id FROM memory_links"
        ).fetchone()
        assert tuple(link) == (source.fragment_id, target.fragment_id)
        node = load_latest_active_node_by_source(
            connection=connection,
            user_id="user-1",
            source=EDITABLE_MEMORY_ROOTS[source_root],
        )
        assert node is not None
        epoch = build_memory_context_epoch(
            user_id="user-1", run_id="run", visible=((node, source, "source"),)
        )
        _, resolved = follow_memory_reference(
            connection=connection,
            epoch=epoch,
            source_handle=epoch.items[0].item.context_handle,
            local_ref_id="r1",
            enqueue_repair_on_failure=False,
        )
        assert resolved["target_content"] == (
            updated_target if target_changes else "oldtarget"
        )
        if broken == "deleted":
            files.delete(path=target_path, expected_text=updated_target)
        else:
            changed = (
                updated_source + f"\n\nCopied {tag}"
                if broken == "duplicate"
                else updated_source.replace('note:"evidence"', 'note:"changed"')
            )
            files.write(path=source_path, text=changed, expected_text=updated_source)
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        assert (
            connection.execute("SELECT count(*) FROM memory_links").fetchone()[0] == 0
        )
        with pytest.raises(MemoryReferenceNotFoundError):
            follow_memory_reference(
                connection=connection,
                epoch=epoch,
                source_handle=epoch.items[0].item.context_handle,
                local_ref_id="r1",
                enqueue_repair_on_failure=False,
            )
        assert (
            tag in files.read(source_path)
            if broken != "note"
            else 'note:"changed"' in files.read(source_path)
        )


def test_failed_index_transaction_recovers_from_saved_file(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
    path = "facts/current.md"
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.write(path=path, text="oldword", expected_text=None)
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        before = _fragments(connection, path)
        connection.execute(
            "CREATE TRIGGER fail_index BEFORE INSERT ON memory_fragments WHEN new.content_text = 'newword' BEGIN SELECT RAISE(ABORT, 'index unavailable'); END"
        )
        files.write(path=path, text="newword", expected_text="oldword")
        with pytest.raises(sqlite3.IntegrityError, match="index unavailable"):
            with immediate_transaction(connection):
                refresh_current_memory_index(connection=connection, files=files)
        assert files.read(path) == "newword"
        assert _fragments(connection, path) == before
        connection.execute("DROP TRIGGER fail_index")
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        assert {item.content_text for item in _fragments(connection, path)} == {
            "newword"
        }
        assert (
            connection.execute("SELECT count(*) FROM memory_revisions").fetchone()[0]
            == 3
        )


def test_reobserved_current_content_updates_existing_context_handles(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.write(path="facts/current.md", text="old", expected_text=None)
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        root = next(
            item
            for item in _fragments(connection, "facts/current.md")
            if item.block_kind == "document_root"
        )
        node = load_latest_active_node_by_source(
            connection=connection, user_id="user-1", source="fact"
        )
        assert node is not None
        original = build_memory_context_epoch(
            user_id="user-1", run_id="run", visible=((node, root, "fact"),)
        )
        handle = original.items[0].item.context_handle
        files.write(path="facts/current.md", text="new", expected_text="old")
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        current = next(
            item
            for item in _fragments(connection, "facts/current.md")
            if item.block_kind == "document_root"
        )
        added = build_memory_context_epoch(
            user_id="user-1", run_id="run", visible=((node, current, "fact"),)
        )
        appended, _ = append_memory_context_item(
            epoch=original,
            source="fact",
            label="current",
            source_path=current.source_path,
            heading_path=None,
            content=current.content_text,
            fragment_id=current.fragment_id,
            revision_id=current.revision_id,
            node_id=node.node_id,
            reference_depth=1,
        )
        for epoch in (
            merge_memory_context_epochs(base=original, added=added),
            appended,
        ):
            refreshed = resolve_context_handle(
                epoch=epoch, user_id="user-1", context_handle=handle
            )
            assert refreshed.item.content == "new"
            assert refreshed.reference_depth == 0


def test_deleting_last_file_keeps_other_users_index_intact(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    path = "agent_experience/current.md"
    for user in ("user-1", "user-2"):
        create_editable_memory_root(artifact_root=tmp_path, user_id=user)
        with locked_memory_files(artifact_root=tmp_path, user_id=user) as files:
            files.write(path=path, text=user, expected_text=None)
            with immediate_transaction(connection):
                refresh_current_memory_index(connection=connection, files=files)
                action = register_inline_domain_memory(
                    connection=connection,
                    user_id=user,
                    source="action",
                    source_record_id="action-1",
                    content="source action",
                )
                experience = _fragments(connection, path, user)[0]
                connection.execute(
                    "INSERT INTO memory_evidence_edges VALUES (?, ?, ?, 'source_action')",
                    (user, experience.revision_id, action.revision_id),
                )
    other = _fragments(connection, path, "user-2")
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.delete(path=path, expected_text="user-1")
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        assert files.documents() == ()
    assert not connection.execute(
        "SELECT 1 FROM memory_fragments WHERE user_id = 'user-1' AND source_path = ?",
        (path,),
    ).fetchall()
    assert _fragments(connection, path, "user-2") == other

    edges = connection.execute(
        "SELECT user_id FROM memory_evidence_edges ORDER BY user_id"
    ).fetchall()
    assert [row[0] for row in edges] == ["user-2"]
    actions = connection.execute(
        "SELECT 1 FROM memory_nodes WHERE source_type = 'action'"
    ).fetchall()
    assert len(actions) == 2


def test_corrupt_newest_record_does_not_fall_back_to_an_older_index(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    create_editable_memory_root(artifact_root=tmp_path, user_id="user-1")
    with locked_memory_files(artifact_root=tmp_path, user_id="user-1") as files:
        files.write(path="facts/current.md", text="current", expected_text=None)
        with immediate_transaction(connection):
            refresh_current_memory_index(connection=connection, files=files)
        old = _fragments(connection, "facts/current.md")[0]
        revision = load_revision(
            connection=connection, user_id="user-1", revision_id=old.revision_id
        )
        assert revision is not None
        with immediate_transaction(connection):
            connection.execute("UPDATE memory_nodes SET updated_at = '2020-01-01'")
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="fact",
                source_record_id="newer-fact",
            )
            insert_revision(
                connection=connection,
                revision=replace(
                    revision, node_id=node.node_id, revision_id="rev-newer"
                ),
            )
            connection.execute(
                "UPDATE memory_nodes SET lifecycle='active', integrity='corrupt', current_revision_id='rev-newer', updated_at='2021-01-01' WHERE node_id=?",
                (node.node_id,),
            )
        with pytest.raises(MemoryCatalogIntegrityError, match="not editable"):
            with immediate_transaction(connection):
                refresh_current_memory_index(connection=connection, files=files)


def test_many_reference_blocks_have_bounded_cpu_cost() -> None:
    document = MemoryDocument(
        "facts/many.md",
        "\n".join(f'- fact [[ref:r{i} note:"evidence"]]' for i in range(20_000)),
    )
    started = process_time()
    references = locate_memory_references((document,))
    elapsed = process_time() - started
    assert [item.block_index for item in references] == list(range(1, 20_001))
    # A valid sub-1 MiB file must not monopolize the shared DB writer for seconds.
    assert elapsed < 2.0
