from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog import (
    publication as publication_module,
)
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_draft,
    serialize_memory_draft,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.draft import (
    create_memory_draft,
    delete_memory_document,
    link_memory,
    move_memory_document,
    replace_draft_documents,
    seal_link_command,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    build_memory_context_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryLinkValidationError,
)
from pantaray_agents.local_runtime.memory_catalog.lifecycle import archive_memory
from pantaray_agents.local_runtime.memory_catalog.models import (
    DraftLink,
    MemoryDocument,
)
from pantaray_agents.local_runtime.memory_catalog.publication import (
    MemoryPublicationRequest,
    publish_inline_revision,
)
from pantaray_agents.local_runtime.memory_catalog.reconciler import (
    reconcile_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.repair_queue import RepairJob
from pantaray_agents.local_runtime.memory_catalog.repair_rollback import (
    rollback_or_quarantine,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
    list_revision_fragments,
    load_node_by_source,
    load_revision,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    follow_memory_reference,
)
from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
)
from pantaray_agents.local_runtime.runtime.activity_source_db import (
    with_activity_source_connection,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
)

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000


def test_publication_success_events_run_only_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(tmp_path)
    events: list[str] = []

    def capture_event(event: str, **_fields: object) -> None:
        events.append(event)

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.memory_catalog.observability."
        "emit_memory_catalog_event",
        capture_event,
    )

    with pytest.raises(RuntimeError, match="force rollback"):
        with immediate_transaction(connection):
            register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="rolled-back-log",
                content="This revision must not emit a success event.",
            )
            raise RuntimeError("force rollback")

    assert "memory_revision_activated" not in events

    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="committed-log",
            content="This revision is durably committed.",
        )

    assert events.count("memory_revision_activated") == 1


def test_activity_source_transaction_emits_publication_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(tmp_path)
    connection.close()
    events: list[str] = []

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.memory_catalog.observability."
        "emit_memory_catalog_event",
        lambda event, **_fields: events.append(event),
    )

    def publish(connection: sqlite3.Connection) -> None:
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="activity-source-log",
            content="Activity source publication.",
        )

    with_activity_source_connection(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        operation=publish,
    )

    assert events.count("memory_revision_activated") == 1


def _connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
        VALUES ('user-1', 'ja', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')
        """
    )
    connection.commit()
    return connection


def _target_context(connection: sqlite3.Connection):
    target_revision = register_inline_domain_memory(
        connection=connection,
        user_id="user-1",
        source="activity_log",
        source_record_id="log-1",
        content="A provider boundary appears in the activity.",
    )
    target_node = load_node_by_source(
        connection=connection,
        user_id="user-1",
        source="activity_log",
        source_record_id="log-1",
    )
    assert target_node is not None
    target_fragment = next(
        fragment
        for fragment in list_revision_fragments(
            connection=connection,
            user_id="user-1",
            revision_id=target_revision.revision_id,
        )
        if fragment.block_kind == "paragraph"
    )
    return target_node, target_fragment


def test_link_publication_resolves_exact_observed_fragment(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        target_node, target_fragment = _target_context(connection)
        source_node = ensure_preparing_node(
            connection=connection,
            user_id="user-1",
            source="short_term_insight",
            source_record_id="insight-1",
        )
        epoch = build_memory_context_epoch(
            run_id="run-1",
            user_id="user-1",
            visible=((target_node, target_fragment, "post-action activity"),),
        )
        draft = create_memory_draft(
            user_id="user-1",
            owner_node_id=source_node.node_id,
            base_revision_id=None,
            documents=(MemoryDocument("body.md", "The boundary repeats here."),),
        )
        command = seal_link_command(
            epoch=epoch,
            draft=draft,
            tool_invocation_id="tool-1",
            target_handle=epoch.items[0].item.context_handle,
            source_path="body.md",
            exact_text="The boundary repeats here.",
            occurrence=1,
            note="同じ依存境界が別の場面にも現れている",
            expected_draft_revision=draft.draft_revision,
        )
        linked = link_memory(draft=draft, command=command)
        source_revision = publish_inline_revision(
            connection=connection,
            request=MemoryPublicationRequest(
                source="short_term_insight",
                source_record_id="insight-1",
                draft=linked,
                body_kind="inline",
            ),
        )
        source_node = load_node_by_source(
            connection=connection,
            user_id="user-1",
            source="short_term_insight",
            source_record_id="insight-1",
        )
        assert source_node is not None
        source_fragment = next(
            fragment
            for fragment in list_revision_fragments(
                connection=connection,
                user_id="user-1",
                revision_id=source_revision.revision_id,
            )
            if fragment.block_kind == "paragraph"
        )
        source_epoch = build_memory_context_epoch(
            run_id="reader-1",
            user_id="user-1",
            visible=((source_node, source_fragment, "short-term insight"),),
        )
        _, resolved = follow_memory_reference(
            connection=connection,
            epoch=source_epoch,
            source_handle=source_epoch.items[0].item.context_handle,
            local_ref_id=command.local_ref_id,
            enqueue_repair_on_failure=True,
        )

    assert resolved["reference_note"] == "同じ依存境界が別の場面にも現れている"
    assert resolved["target_fragment_id"] == target_fragment.fragment_id
    assert resolved["target_content"] == target_fragment.content_text
    assert resolved["target_is_current"] is True


def test_target_update_keeps_linked_snapshot_and_archive_blocks_new_link(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        old_node, old_fragment = _target_context(connection)
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-1",
            content="The activity changed later.",
        )
        current_node = load_node_by_source(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-1",
        )
        assert current_node is not None
        assert current_node.current_revision_id != old_fragment.revision_id
        archive_memory(
            connection=connection,
            user_id="user-1",
            node_id=current_node.node_id,
        )
        source_node = ensure_preparing_node(
            connection=connection,
            user_id="user-1",
            source="action",
            source_record_id="action-1",
        )
        draft = create_memory_draft(
            user_id="user-1",
            owner_node_id=source_node.node_id,
            base_revision_id=None,
            documents=(MemoryDocument("body.md", "Use the old observation."),),
        )
        stale_epoch = build_memory_context_epoch(
            run_id="stale",
            user_id="user-1",
            visible=((old_node, old_fragment, "old activity"),),
        )
        command = seal_link_command(
            epoch=stale_epoch,
            draft=draft,
            tool_invocation_id="tool-1",
            target_handle=stale_epoch.items[0].item.context_handle,
            source_path="body.md",
            exact_text="Use the old observation.",
            occurrence=1,
            note="過去の観測を参照する",
            expected_draft_revision=draft.draft_revision,
        )
        linked = link_memory(draft=draft, command=command)
        with pytest.raises(MemoryLinkValidationError, match="active healthy"):
            publish_inline_revision(
                connection=connection,
                request=MemoryPublicationRequest(
                    source="action",
                    source_record_id="action-1",
                    draft=linked,
                    body_kind="inline",
                ),
            )


@pytest.mark.parametrize(
    "path",
    (
        "../outside.md",
        "/absolute.md",
        "facts//item.md",
        "facts\\item.md",
        "facts/item\0.md",
    ),
)
def test_memory_draft_rejects_noncanonical_or_escaping_paths(path: str) -> None:
    with pytest.raises(ValueError, match="canonical relative POSIX"):
        create_memory_draft(
            user_id="user-1",
            owner_node_id="node-1",
            base_revision_id=None,
            documents=(MemoryDocument(path, "content"),),
        )


def test_memory_draft_checkpoint_rejects_body_revision_mismatch() -> None:
    draft = create_memory_draft(
        user_id="user-1",
        owner_node_id="node-1",
        base_revision_id=None,
        documents=(MemoryDocument("body.md", "original"),),
    )
    model = serialize_memory_draft(draft).model_copy(
        update={"draft_revision": "sha256:tampered"}
    )

    with pytest.raises(MemoryLinkValidationError, match="does not match its body"):
        deserialize_memory_draft(model)


def test_move_and_delete_update_link_state_idempotently() -> None:
    draft = create_memory_draft(
        user_id="user-1",
        owner_node_id="node-1",
        base_revision_id=None,
        documents=(
            MemoryDocument("facts/a.md", "A"),
            MemoryDocument("facts/b.md", "B"),
        ),
    )
    moved = move_memory_document(
        draft=draft,
        tool_invocation_id="move-1",
        source_path="facts/a.md",
        destination_path="facts/c.md",
        expected_draft_revision=draft.draft_revision,
    )
    replayed = move_memory_document(
        draft=moved,
        tool_invocation_id="move-1",
        source_path="facts/a.md",
        destination_path="facts/c.md",
        expected_draft_revision=draft.draft_revision,
    )
    deleted = delete_memory_document(
        draft=replayed,
        tool_invocation_id="delete-1",
        source_path="facts/c.md",
        expected_draft_revision=replayed.draft_revision,
    )
    assert replayed == moved
    assert [item.source_path for item in deleted.documents] == ["facts/b.md"]


def test_reconciler_removes_complete_tag_when_mapping_is_lost(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        target_node, target_fragment = _target_context(connection)
        source_node = ensure_preparing_node(
            connection=connection,
            user_id="user-1",
            source="short_term_insight",
            source_record_id="insight-repair",
        )
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES (
                'suggestion-1', 'user-1', 'success', 'suggestion', '1.0',
                '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_insights(
                insight_id, user_id, suggestion_id, status,
                short_term_insight_data, facts, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES (
                'insight-repair', 'user-1', 'suggestion-1', 'success',
                'Repeated boundary.', '', 'insight', '1.0',
                '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )
        epoch = build_memory_context_epoch(
            run_id="repair-source",
            user_id="user-1",
            visible=((target_node, target_fragment, "activity"),),
        )
        draft = create_memory_draft(
            user_id="user-1",
            owner_node_id=source_node.node_id,
            base_revision_id=None,
            documents=(MemoryDocument("body.md", "Repeated boundary."),),
        )
        linked = link_memory(
            draft=draft,
            command=seal_link_command(
                epoch=epoch,
                draft=draft,
                tool_invocation_id="repair-link",
                target_handle=epoch.items[0].item.context_handle,
                source_path="body.md",
                exact_text="Repeated boundary.",
                occurrence=1,
                note="同じ境界問題",
                expected_draft_revision=draft.draft_revision,
            ),
        )
        revision = publish_inline_revision(
            connection=connection,
            request=MemoryPublicationRequest(
                source="short_term_insight",
                source_record_id="insight-repair",
                draft=linked,
                body_kind="inline",
            ),
        )
        connection.execute(
            "DELETE FROM memory_links WHERE user_id = ? AND source_revision_id = ?",
            ("user-1", revision.revision_id),
        )
    connection.close()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    enqueued, completed = reconcile_memory_catalog(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        scan_limit=100,
    )

    assert (enqueued, completed) == (1, 1)
    with sqlite3.connect(tmp_path / "runtime.db") as verification:
        body = verification.execute(
            """
            SELECT revisions.inline_body
            FROM memory_nodes AS nodes
            JOIN memory_revisions AS revisions
              ON revisions.user_id = nodes.user_id
             AND revisions.revision_id = nodes.current_revision_id
            WHERE nodes.user_id = 'user-1'
              AND nodes.source_type = 'short_term_insight'
              AND nodes.source_record_id = 'insight-repair'
            """
        ).fetchone()[0]
        domain_body = verification.execute(
            "SELECT short_term_insight_data FROM agent_insights WHERE insight_id = 'insight-repair'"
        ).fetchone()[0]
    assert body == "Repeated boundary."
    assert domain_body == body


def test_rollback_restores_the_parent_when_revisions_share_a_millisecond(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        publication_module, "now_utc_iso", lambda: "2026-07-18T00:00:00.000Z"
    )
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES (
                'suggestion-1', 'user-1', 'success', 'suggestion', '1.0',
                '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_insights(
                insight_id, user_id, suggestion_id, status,
                short_term_insight_data, facts, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES (
                'insight-rollback', 'user-1', 'suggestion-1', 'success',
                'Third.', '', 'insight', '1.0',
                '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )
        node = ensure_preparing_node(
            connection=connection,
            user_id="user-1",
            source="short_term_insight",
            source_record_id="insight-rollback",
        )
        revision_ids: list[str] = []
        # Ascending ids make the tied created_at scan return the oldest first.
        for number, body in enumerate(("First.", "Second.", "Third."), start=1):
            revision = publish_inline_revision(
                connection=connection,
                request=MemoryPublicationRequest(
                    source="short_term_insight",
                    source_record_id="insight-rollback",
                    revision_id=f"rev_{number}",
                    draft=create_memory_draft(
                        user_id="user-1",
                        owner_node_id=node.node_id,
                        base_revision_id=revision_ids[-1] if revision_ids else None,
                        documents=(MemoryDocument("body.md", body),),
                    ),
                    body_kind="inline",
                ),
            )
            revision_ids.append(revision.revision_id)
    head = load_node_by_source(
        connection=connection,
        user_id="user-1",
        source="short_term_insight",
        source_record_id="insight-rollback",
    )
    current = load_revision(
        connection=connection, user_id="user-1", revision_id=revision_ids[-1]
    )
    assert head is not None and current is not None

    rollback_or_quarantine(
        connection=connection,
        artifact_root=tmp_path / "artifacts",
        node=head,
        current_revision=current,
        job=RepairJob(
            user_id="user-1",
            node_id=head.node_id,
            detected_revision_id=current.revision_id,
            reason="revision_integrity",
            attempt_count=0,
        ),
    )

    restored = load_node_by_source(
        connection=connection,
        user_id="user-1",
        source="short_term_insight",
        source_record_id="insight-rollback",
    )
    assert restored is not None
    assert restored.current_revision_id == revision_ids[1]


def test_second_link_uses_parser_to_ignore_existing_tag_with_bracket_note(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        first_target_node, first_target_fragment = _target_context(connection)
        second_revision = register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-2",
            content="Another relevant observation.",
        )
        second_target_node = load_node_by_source(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-2",
        )
        assert second_target_node is not None
        second_target_fragment = next(
            fragment
            for fragment in list_revision_fragments(
                connection=connection,
                user_id="user-1",
                revision_id=second_revision.revision_id,
            )
            if fragment.block_kind == "paragraph"
        )
        epoch = build_memory_context_epoch(
            run_id="run-bracket-note",
            user_id="user-1",
            visible=(
                (first_target_node, first_target_fragment, "first"),
                (second_target_node, second_target_fragment, "second"),
            ),
        )
        source_node = ensure_preparing_node(
            connection=connection,
            user_id="user-1",
            source="short_term_insight",
            source_record_id="insight-bracket-note",
        )
        draft = create_memory_draft(
            user_id="user-1",
            owner_node_id=source_node.node_id,
            base_revision_id=None,
            documents=(MemoryDocument("body.md", "The pattern repeats."),),
        )
        first = link_memory(
            draft=draft,
            command=seal_link_command(
                epoch=epoch,
                draft=draft,
                tool_invocation_id="first-link",
                target_handle=epoch.items[0].item.context_handle,
                source_path="body.md",
                exact_text="The pattern repeats.",
                occurrence=1,
                note="same pattern [earlier]",
                expected_draft_revision=draft.draft_revision,
            ),
        )
        second = link_memory(
            draft=first,
            command=seal_link_command(
                epoch=epoch,
                draft=first,
                tool_invocation_id="second-link",
                target_handle=epoch.items[1].item.context_handle,
                source_path="body.md",
                exact_text="The pattern repeats.",
                occurrence=1,
                note="same pattern elsewhere",
                expected_draft_revision=first.draft_revision,
            ),
        )

    assert len(extract_markdown_references(second.documents[0].content)) == 2


_EDIT_TAG = '[[ref:ref_1 note:"evidence"]]'
_EDIT_TEXT = f"# Context\n\n- Claim. {_EDIT_TAG}\n- Other.\n"


def _reference_edit_draft():
    return create_memory_draft(
        user_id="user-1",
        owner_node_id="fact-1",
        base_revision_id=None,
        documents=(MemoryDocument("facts/topic.md", _EDIT_TEXT),),
        carried_links=(
            DraftLink(
                local_ref_id="ref_1",
                target_fragment_id="fragment_1",
                source_path="facts/topic.md",
                source_anchor_text="- Claim.",
                source_anchor_occurrence=1,
                reference_note="evidence",
                created_at="2026-09-13T00:00:00Z",
                state="carried",
            ),
        ),
    )


@pytest.mark.parametrize(
    "content",
    [
        _EDIT_TEXT.replace("Claim.", "Revised claim."),
        _EDIT_TEXT.replace("# Context", "## Different context"),
        _EDIT_TEXT.replace("- Claim.", "  - Claim."),
        _EDIT_TEXT.replace("- Claim.", "Claim."),
        f"# Context\n\n- Other.\n\n## Elsewhere\n- Claim. {_EDIT_TAG}\n",
        _EDIT_TEXT + "- New fact.\n",
        _EDIT_TEXT + r'Literal [\[ref:example note:"file text"]] stays text.',
        _EDIT_TEXT + "Read [ref](https://example.test/guide).\n",
        _EDIT_TEXT + "Read [ref:guide](https://example.test/guide).\n",
        _EDIT_TEXT + "Read [[ref]].\n",
        "Read [[ref guide]].\n" + _EDIT_TEXT,
        "Read [ref:guide][guide].\n\n[guide]: https://example.test/guide\n"
        + _EDIT_TEXT,
    ],
)
def test_generic_edit_preserves_links_while_editing_or_moving_text(
    content: str,
) -> None:
    draft = _reference_edit_draft()
    updated = replace_draft_documents(
        draft=draft,
        documents=(MemoryDocument("facts/topic.md", content),),
    )
    assert updated.documents[0].content == content
    assert updated.links == draft.links
    assert deserialize_memory_draft(serialize_memory_draft(updated)) == updated


@pytest.mark.parametrize(
    "content",
    [
        _EDIT_TEXT.replace(" " + _EDIT_TAG, ""),
        _EDIT_TEXT.replace(f"- Claim. {_EDIT_TAG}\n", ""),
        _EDIT_TEXT.replace(_EDIT_TAG, '[[ note:"evidence"]]'),
    ],
)
def test_generic_edit_removes_link_with_its_tag_or_statement(content: str) -> None:
    draft = _reference_edit_draft()
    updated = replace_draft_documents(
        draft=draft,
        documents=(MemoryDocument("facts/topic.md", content),),
    )
    assert updated.documents[0].content == content
    assert updated.links[0].state == "removed"
    assert updated.links[0].target_fragment_id == draft.links[0].target_fragment_id
    assert draft.links[0].state == "carried"
    assert deserialize_memory_draft(serialize_memory_draft(updated)) == updated
    with pytest.raises(MemoryLinkValidationError):
        replace_draft_documents(draft=updated, documents=draft.documents)


@pytest.mark.parametrize(
    "content, path",
    [
        (_EDIT_TEXT.replace("ref_1", "ref_unknown"), "facts/topic.md"),
        (_EDIT_TEXT + f"- Copy. {_EDIT_TAG}\n", "facts/topic.md"),
        (_EDIT_TEXT.replace('note:"evidence"', 'note:"changed"'), "facts/topic.md"),
        (_EDIT_TEXT.replace("]]", "]"), "facts/topic.md"),
        (_EDIT_TEXT.replace("[[ref:", "[ref:"), "facts/topic.md"),
        (_EDIT_TEXT.replace("[[ref:", "[[ref "), "facts/topic.md"),
        (_EDIT_TEXT, "facts/other.md"),
    ],
)
def test_generic_edit_rejects_invalid_refs(content: str, path: str) -> None:
    draft = _reference_edit_draft()
    with pytest.raises(MemoryLinkValidationError):
        replace_draft_documents(draft=draft, documents=(MemoryDocument(path, content),))
    assert draft.documents[0].content == _EDIT_TEXT
    assert draft.links[0].state == "carried"


@pytest.mark.parametrize(
    "literal",
    [
        "The identifier ref_1 is also mentioned as plain text.",
        "Existing token prefixref_1suffix remains literal text.",
        r'Literal [\[ref:ref_1 note:"evidence"]] is not a link.',
    ],
)
def test_deleting_ref_preserves_preexisting_literal_mentions(literal: str) -> None:
    draft = _reference_edit_draft()
    with_literal = replace_draft_documents(
        draft=draft, documents=(MemoryDocument("facts/topic.md", _EDIT_TEXT + literal),)
    )
    updated = replace_draft_documents(
        draft=with_literal,
        documents=(
            MemoryDocument(
                "facts/topic.md", _EDIT_TEXT.replace(" " + _EDIT_TAG, "") + literal
            ),
        ),
    )
    assert updated.links[0].state == "removed"
    assert updated.documents[0].content.endswith(literal)


def test_deleting_ref_allows_moving_and_indenting_an_existing_literal() -> None:
    draft = _reference_edit_draft()
    with_literal = replace_draft_documents(
        draft=draft,
        documents=(MemoryDocument("facts/topic.md", _EDIT_TEXT + "Literal ref_1.\n"),),
    )
    revised = "  Literal ref_1.\n" + _EDIT_TEXT.replace(" " + _EDIT_TAG, "")
    updated = replace_draft_documents(
        draft=with_literal,
        documents=(MemoryDocument("facts/topic.md", revised),),
    )
    assert updated.documents[0].content == revised
    assert updated.links[0].state == "removed"
