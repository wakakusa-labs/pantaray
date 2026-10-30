from __future__ import annotations

import errno
import json
import sqlite3
from pathlib import Path

import pytest

import pantaray_agents.local_runtime.memory_catalog.artifact_recovery as recovery_module
import pantaray_agents.local_runtime.memory_catalog.artifact_tree_durability as durability_module
import pantaray_agents.local_runtime.memory_catalog.publication as publication_module
from pantaray_agents.local_runtime.memory_catalog.artifact_domain_publication import (
    FactArtifactPublication,
    publish_fact_artifact,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_recovery import (
    recover_artifact_revision_intents,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_workspace import (
    prepare_artifact_draft,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.draft import (
    create_memory_draft,
    link_memory,
    replace_draft_documents,
    seal_link_command,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    build_memory_context_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.erasure import (
    erase_user_memory_offline,
    resume_pending_user_erasures,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
    MemoryPublicationConflictError,
    MemoryPublicationPendingIntentError,
)
from pantaray_agents.local_runtime.memory_catalog.fragments import (
    artifact_content_sha256,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    MemoryRunBinding,
    memory_run_binding_from_payload,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryDraftCheckpoint,
)
from pantaray_agents.local_runtime.memory_catalog.publication import (
    MemoryPublicationRequest,
    create_artifact_revision_intent,
    materialize_artifact_revision,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
    list_revision_fragments,
    list_revision_links,
    load_node_by_source,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    follow_memory_reference,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
)
from pantaray_agents.local_runtime.runtime.memory_update_queue import (
    build_local_memory_update_enqueue_request,
)
from pantaray_agents.local_runtime.storage.memory_update_lock import (
    MemoryUpdateLockLease,
    memory_update_lock_path,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_suggestion_artifacts_root,
)
from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)
from pantaray_agents.tasks.types import MemoryUpdateJobPayload

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
MEMORY_JOB_PAYLOAD: MemoryUpdateJobPayload = {
    "job_id": "job-memory-1",
    "process_id": "process-memory-1",
    "user_id": "user-1",
    "enqueued_at": "2026-07-18T00:00:00Z",
    "short_insight_ids": ["insight-1"],
    "summary_ids": [],
    "action_terminals": [],
}


def test_fact_artifact_activation_updates_domain_and_catalog_atomically(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    draft = _fact_draft(db_path)

    revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(draft),
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT facts.status, facts.structured_fact_sha256,
                   nodes.current_revision_id
            FROM agent_facts AS facts
            JOIN memory_nodes AS nodes
              ON nodes.user_id = facts.user_id
             AND nodes.source_type = 'fact'
             AND nodes.source_record_id = facts.fact_id
            JOIN memory_revisions AS revisions
              ON revisions.user_id = nodes.user_id
             AND revisions.revision_id = nodes.current_revision_id
            WHERE facts.fact_id = 'fact-1'
            """
        ).fetchone()
        intent_count = connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone()[0]
        retired_run_status = connection.execute(
            "SELECT status FROM agent_fact_structuring_runs WHERE fact_run_id = 'run-1'"
        ).fetchone()
    assert row == (
        "success",
        revision.content_sha256,
        revision.revision_id,
    )
    # The retired per-Fact run ledger is history: publishing never writes it.
    assert retired_run_status == ("processing",)
    assert intent_count == 0
    assert (artifact_root / revision.artifact_root_path / "facts/index.md").read_text(
        encoding="utf-8"
    ) == "# Facts\n- Durable\n"


@pytest.mark.parametrize(
    "block_kind, source_template",
    [
        ("list_item", "- Rewritten claim. {tag}\n"),
        (
            "list_item",
            '- Rewritten claim. {tag}\n- Remove. [[ note:"observed evidence"]]\n',
        ),
        ("heading", "### Rewritten claim. {tag}\n"),
        ("code_block", "```text\nRewritten claim. {tag}\n```\n"),
    ],
)
def test_edited_artifact_reloads_only_surviving_refs_on_the_new_source_text(
    tmp_path: Path,
    block_kind: str,
    source_template: str,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    draft = _fact_draft(db_path)
    draft = replace_draft_documents(
        draft=draft,
        documents=(MemoryDocument("facts/index.md", "# Facts\n- Keep.\n- Remove.\n"),),
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            target_revision = register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="log-1",
                content="Observed evidence.",
            )
            target_node = load_node_by_source(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="log-1",
            )
            assert target_node is not None
            target = next(
                fragment
                for fragment in list_revision_fragments(
                    connection=connection,
                    user_id="user-1",
                    revision_id=target_revision.revision_id,
                )
                if fragment.block_kind == "paragraph"
            )
    epoch = build_memory_context_epoch(
        run_id="run-1",
        user_id="user-1",
        visible=((target_node, target, "evidence"),),
    )
    for index, anchor in enumerate(("Keep.", "Remove.")):
        draft = link_memory(
            draft=draft,
            command=seal_link_command(
                epoch=epoch,
                draft=draft,
                tool_invocation_id=f"link-{index}",
                target_handle=epoch.items[0].item.context_handle,
                source_path="facts/index.md",
                exact_text=anchor,
                occurrence=1,
                note="observed evidence",
                expected_draft_revision=draft.draft_revision,
            ),
        )
    surviving, removed = draft.links
    tag = f'[[ref:{surviving.local_ref_id} note:"observed evidence"]]'
    revised = "## Revised context\n" + source_template.format(tag=tag)
    draft = replace_draft_documents(
        draft=draft, documents=(MemoryDocument("facts/index.md", revised),)
    )
    revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(draft),
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        loaded = prepare_artifact_draft(
            connection=connection,
            artifact_root=artifact_root,
            user_id="user-1",
            source="fact",
            source_record_id="fact-1",
            initial_documents=draft.documents,
        )
        links = list_revision_links(
            connection=connection, user_id="user-1", revision_id=revision.revision_id
        )
        assert len(links) == 1
        assert links[0].local_ref_id == surviving.local_ref_id
        assert links[0].target_fragment_id == target.fragment_id
        assert all(link.local_ref_id != removed.local_ref_id for link in loaded.links)
        assert loaded.documents[0].content == revised
        source_node = load_node_by_source(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-1",
        )
        assert source_node is not None
        source = next(
            fragment
            for fragment in list_revision_fragments(
                connection=connection,
                user_id="user-1",
                revision_id=revision.revision_id,
            )
            if fragment.fragment_id == links[0].source_fragment_id
        )
        assert source.block_kind == block_kind
        assert loaded.links[0].source_anchor_text == source.content_text
        assert source.heading_path.split(" / ")[0] == "Revised context"
        reader_epoch = build_memory_context_epoch(
            run_id="reader-1",
            user_id="user-1",
            visible=((source_node, source, "updated fact"),),
        )
        _, resolved = follow_memory_reference(
            connection=connection,
            epoch=reader_epoch,
            local_ref_id=surviving.local_ref_id,
            pinned_revisions=None,
            enqueue_repair_on_failure=True,
        )
        assert resolved["target_fragment_id"] == target.fragment_id
        assert resolved["reference_note"] == "observed evidence"
        assert resolved["target_content"] == "Observed evidence."


def test_artifact_intent_conflict_is_typed_and_retryable(tmp_path: Path) -> None:
    db_path, _ = _runtime(tmp_path)
    request = MemoryPublicationRequest(
        source="fact",
        source_record_id="fact-1",
        draft=_fact_draft(db_path),
        body_kind="artifact_tree",
        intent_kind="fact",
        domain_payload_json="{}",
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            create_artifact_revision_intent(connection=connection, request=request)

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with pytest.raises(MemoryPublicationPendingIntentError) as caught:
            with immediate_transaction(connection):
                create_artifact_revision_intent(connection=connection, request=request)
        intent_count = connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone()[0]

    assert not isinstance(caught.value, sqlite3.IntegrityError)
    assert is_retryable_repository_exception(caught.value)
    assert intent_count == 1
    assert not is_retryable_repository_exception(
        MemoryPublicationConflictError("memory head changed")
    )


def test_fact_publication_rejects_existing_fact_owned_by_another_user(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-2', 'ja', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_facts(
                fact_id, user_id, status, prompt_name, prompt_version,
                source_insight_ids, created_at, updated_at
            ) VALUES (
                'fact-1', 'user-2', 'success', 'fact_structuring', '1.0', '[]',
                '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )

    with pytest.raises(MemoryCatalogIntegrityError, match="owner does not match"):
        publish_fact_artifact(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
            publication=_publication(_fact_draft(db_path)),
        )

    with sqlite3.connect(db_path) as connection:
        owner = connection.execute(
            "SELECT user_id FROM agent_facts WHERE fact_id = 'fact-1'"
        ).fetchone()
        run_status = connection.execute(
            "SELECT status FROM agent_fact_structuring_runs WHERE fact_run_id = 'run-1'"
        ).fetchone()
    assert owner == ("user-2",)
    assert run_status == ("processing",)


def test_recovery_activates_exact_materialized_intent(tmp_path: Path) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _materialize_fact_intent(db_path=db_path, artifact_root=artifact_root)

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 1
    with sqlite3.connect(db_path) as connection:
        # A retired per-Fact run left this intent pending: recovery writes the
        # Fact projection and leaves that run's closed ledger alone.
        assert connection.execute(
            "SELECT status FROM agent_facts WHERE fact_id = 'fact-1'"
        ).fetchone() == ("success",)
        assert (
            connection.execute(
                "SELECT status FROM agent_fact_structuring_runs WHERE fact_run_id = 'run-1'"
            ).fetchone()[0]
            == "processing"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_revision_intents"
            ).fetchone()[0]
            == 0
        )


def test_recovery_activates_a_retired_insight_update_intent(tmp_path: Path) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_insight_update_runs(
                insight_update_id, user_id, status, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES (
                'insight-update-1', 'user-1', 'processing', 'insight_update',
                '1.0', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="long_term_insight",
                source_record_id="user-1",
            )
            prepared = create_artifact_revision_intent(
                connection=connection,
                request=MemoryPublicationRequest(
                    source="long_term_insight",
                    source_record_id="user-1",
                    draft=create_memory_draft(
                        user_id="user-1",
                        owner_node_id=node.node_id,
                        base_revision_id=None,
                        documents=(
                            MemoryDocument(
                                "insights/index.md", "# Insights\n- Durable\n"
                            ),
                        ),
                    ),
                    body_kind="artifact_tree",
                    intent_kind="long_term_insight",
                    domain_payload_json=(
                        '{"base_sha256":null,"base_storage_path":null,'
                        '"insight_update_id":"insight-update-1",'
                        '"profile_brief":"legacy brief",'
                        '"prompt_name":"insight_update","prompt_version":"1.0",'
                        '"source_insight_id":"insight-1","user_id":"user-1"}'
                    ),
                ),
            )
    materialize_artifact_revision(artifact_root=artifact_root, prepared=prepared)

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 1
    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            """
            SELECT source_insight_id, source_run_id
            FROM agent_long_term_insight_state WHERE user_id = 'user-1'
            """
        ).fetchone()
        retired_run_status = connection.execute(
            """
            SELECT status FROM agent_insight_update_runs
            WHERE insight_update_id = 'insight-update-1'
            """
        ).fetchone()
    assert state == (None, "insight-update-1")
    assert retired_run_status == ("processing",)


def test_recovery_rematerializes_absent_artifact_from_intent_draft(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    prepared = _create_fact_intent(db_path=db_path)

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 1
    revision_root = artifact_root / prepared.final_relative_path
    assert (revision_root / "facts/index.md").read_text(encoding="utf-8") == (
        "# Facts\n- Durable\n"
    )
    with sqlite3.connect(db_path) as connection:
        revision = connection.execute(
            "SELECT revision_id FROM memory_revisions WHERE user_id = 'user-1'"
        ).fetchone()
        assert revision == (prepared.revision_id,)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone() == (0,)


def test_recovery_rematerializes_incomplete_nested_artifact_from_intent(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    prepared = _create_fact_intent(db_path=db_path)
    materialize_artifact_revision(artifact_root=artifact_root, prepared=prepared)
    revision_root = artifact_root / prepared.final_relative_path
    (revision_root / "facts/index.md").unlink()

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 1
    assert (revision_root / "facts/index.md").read_text(encoding="utf-8") == (
        "# Facts\n- Durable\n"
    )
    assert not tuple(revision_root.parent.glob("memory-recovery-*"))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_artifact_deletions"
        ).fetchone() == (0,)


def test_artifact_tree_fsyncs_nested_directories_before_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    tree_root = artifact_root / "users/user-1/revision-1"
    nested = tree_root / "facts/nested"
    nested.mkdir(parents=True)
    synced: list[Path] = []
    monkeypatch.setattr(durability_module, "_fsync_directory", synced.append)

    durability_module.fsync_artifact_tree(
        tree_root=tree_root,
        ancestor_root=artifact_root,
    )

    assert synced.index(nested) < synced.index(nested.parent) < synced.index(tree_root)
    assert synced.index(tree_root) < synced.index(tree_root.parent)
    assert synced[-1] == artifact_root.resolve()


def test_enospc_materialization_preserves_draft_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    prepared = _create_fact_intent(db_path=db_path)

    def fail_write(_path: Path, _content: str) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    with monkeypatch.context() as materialize_failure:
        materialize_failure.setattr(
            publication_module, "_write_durable_text", fail_write
        )
        with pytest.raises(OSError) as caught:
            materialize_artifact_revision(
                artifact_root=artifact_root,
                prepared=prepared,
            )

    assert caught.value.errno == errno.ENOSPC
    assert is_retryable_repository_exception(caught.value)
    _assert_fact_intent_pending(db_path)
    assert not (artifact_root / prepared.final_relative_path).exists()

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 1
    assert (artifact_root / prepared.final_relative_path / "facts/index.md").read_text(
        encoding="utf-8"
    ) == "# Facts\n- Durable\n"


@pytest.mark.parametrize("error_number", (errno.ENOSPC, errno.EIO))
def test_recovery_materialization_os_error_preserves_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _create_fact_intent(db_path=db_path)

    def fail_materialization(**_kwargs: object) -> None:
        raise OSError(error_number, "storage unavailable")

    monkeypatch.setattr(
        recovery_module,
        "materialize_artifact_revision",
        fail_materialization,
    )

    with pytest.raises(OSError) as caught:
        recover_artifact_revision_intents(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
        )

    assert caught.value.errno == error_number
    assert is_retryable_repository_exception(caught.value)
    _assert_fact_intent_pending(db_path)


@pytest.mark.parametrize("tampered_field", ("domain_payload_json", "draft_checkpoint"))
def test_recovery_rejects_tampered_intent_envelope(
    tmp_path: Path, tampered_field: str
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _create_fact_intent(db_path=db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT domain_payload_json FROM memory_revision_intents"
        ).fetchone()
        assert row is not None
        envelope = json.loads(str(row[0]))
        if tampered_field == "domain_payload_json":
            domain_payload = json.loads(envelope["domain_payload_json"])
            domain_payload["fact_id"] = "fact-other"
            envelope["domain_payload_json"] = json.dumps(domain_payload)
        else:
            tampered_content = "tampered"
            envelope["draft_checkpoint"]["documents"][0]["content"] = tampered_content
            envelope["draft_checkpoint"]["draft_revision"] = (
                f"sha256:{artifact_content_sha256((MemoryDocument('facts/index.md', tampered_content),))}"
            )
        connection.execute(
            "UPDATE memory_revision_intents SET domain_payload_json = ?",
            (json.dumps(envelope),),
        )

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM agent_fact_structuring_runs WHERE fact_run_id = 'run-1'"
        ).fetchone() == ("processing",)


def test_recovery_rejects_tampered_manifest_hash(tmp_path: Path) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _create_fact_intent(db_path=db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE memory_revision_intents SET manifest_sha256 = ?",
            ("0" * 64,),
        )

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM agent_fact_structuring_runs WHERE fact_run_id = 'run-1'"
        ).fetchone() == ("processing",)


def test_recovery_os_error_preserves_materialized_intent_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _materialize_fact_intent(db_path=db_path, artifact_root=artifact_root)

    def fail_manifest_read(**_kwargs: object) -> None:
        raise OSError("temporary artifact read failure")

    monkeypatch.setattr(recovery_module, "load_artifact_manifest", fail_manifest_read)

    with pytest.raises(OSError, match="temporary artifact read failure"):
        recover_artifact_revision_intents(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
        )

    _assert_fact_intent_pending(db_path)


def test_recovery_directory_probe_os_error_preserves_intent_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _materialize_fact_intent(db_path=db_path, artifact_root=artifact_root)

    def fail_directory_probe(_path: Path) -> bool:
        raise OSError("temporary artifact stat failure")

    monkeypatch.setattr(recovery_module, "_is_directory", fail_directory_probe)

    with pytest.raises(OSError, match="temporary artifact stat failure"):
        recover_artifact_revision_intents(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
        )

    _assert_fact_intent_pending(db_path)


def test_recovery_integrity_error_terminalizes_invalid_intent(tmp_path: Path) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _materialize_fact_intent(db_path=db_path, artifact_root=artifact_root)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE memory_revision_intents SET artifact_root_path = '../escaped'"
        )

    recovered = recover_artifact_revision_intents(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
    )

    assert recovered == 0
    with sqlite3.connect(db_path) as connection:
        intent_count = connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone()[0]
        deletion_count = connection.execute(
            "SELECT COUNT(*) FROM memory_artifact_deletions"
        ).fetchone()[0]
    assert intent_count == 0
    assert deletion_count == 1


def test_offline_user_erasure_removes_domain_graph_and_artifacts(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-2', 'ja', '2026-07-18T00:00:00Z',
                    '2026-07-18T00:00:00Z')
            """
        )
    revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    assert revision.artifact_root_path is not None
    assert (artifact_root / revision.artifact_root_path).exists()
    legacy_root = artifact_root / "memory" / "users" / "user-1"
    legacy_root.mkdir(parents=True)
    (legacy_root / "legacy.md").write_text("legacy memory", encoding="utf-8")
    scratch_root = db_path.parent / "local_runtime_workspaces" / "scratch"
    erased_user_scratch = scratch_root / "user-1"
    retained_user_scratch = scratch_root / "user-2"
    (erased_user_scratch / "action-1" / "scratch").mkdir(parents=True)
    (erased_user_scratch / "action-1" / "scratch" / "draft.txt").write_text(
        "erase me", encoding="utf-8"
    )
    retained_user_scratch.mkdir(parents=True)
    retained_file = retained_user_scratch / "keep.txt"
    retained_file.write_text("keep me", encoding="utf-8")
    erased_suggestion_files = resolve_suggestion_artifacts_root(
        db_path=db_path, user_id="user-1", suggestion_id="sug-1"
    )
    erased_suggestion_files.mkdir(parents=True)
    (erased_suggestion_files / "draft.md").write_text("erase me", encoding="utf-8")
    retained_suggestion_file = (
        resolve_suggestion_artifacts_root(
            db_path=db_path, user_id="user-2", suggestion_id="sug-2"
        )
        / "keep.md"
    )
    retained_suggestion_file.parent.mkdir(parents=True)
    retained_suggestion_file.write_text("keep me", encoding="utf-8")

    result = erase_user_memory_offline(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        user_id="user-1",
    )

    assert result.erased is True
    assert not (artifact_root / "memory_catalog" / "users" / "user-1").exists()
    assert not legacy_root.exists()
    assert not erased_user_scratch.exists()
    assert retained_file.read_text(encoding="utf-8") == "keep me"
    assert not erased_suggestion_files.parent.exists()
    assert retained_suggestion_file.read_text(encoding="utf-8") == "keep me"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT user_id FROM users ORDER BY user_id"
        ).fetchall() == [("user-2",)]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_nodes WHERE user_id = 'user-1'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_artifact_deletions WHERE user_id = 'user-1'"
            ).fetchone()[0]
            == 0
        )


def test_offline_user_erasure_removes_only_target_user_update_lock(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    erased_user_lock_path = memory_update_lock_path(
        root_path=artifact_root,
        user_id="user-1",
    )
    retained_user_lock_path = memory_update_lock_path(
        root_path=artifact_root,
        user_id="user-2",
    )
    with MemoryUpdateLockLease(
        root_path=artifact_root,
        user_id="user-1",
        owner_id="run-1",
    ):
        pass
    with MemoryUpdateLockLease(
        root_path=artifact_root,
        user_id="user-2",
        owner_id="run-2",
    ):
        pass
    assert erased_user_lock_path.exists()
    assert retained_user_lock_path.exists()

    result = erase_user_memory_offline(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        user_id="user-1",
    )

    assert result.erased is True
    assert not erased_user_lock_path.exists()
    assert retained_user_lock_path.exists()


def test_offline_user_erasure_rejects_cross_tenant_directory_symlink(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-2', 'ja', '2026-07-18T00:00:00Z',
                    '2026-07-18T00:00:00Z')
            """
        )
    users_root = artifact_root / "memory_catalog" / "users"
    retained_user_root = users_root / "user-2"
    retained_user_root.mkdir(parents=True)
    retained_file = retained_user_root / "keep.md"
    retained_file.write_text("tenant two", encoding="utf-8")
    erased_user_root = users_root / "user-1"
    erased_user_root.symlink_to(retained_user_root, target_is_directory=True)

    with pytest.raises(MemoryCatalogIntegrityError):
        erase_user_memory_offline(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
            user_id="user-1",
        )

    assert erased_user_root.is_symlink()
    assert retained_file.read_text(encoding="utf-8") == "tenant two"
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE user_id IN ('user-1', 'user-2')"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute(
            """
            SELECT state FROM memory_artifact_deletions
            WHERE user_id = 'user-1' AND reason = 'user_erasure'
            """
        ).fetchone() == ("planned",)


def test_offline_user_erasure_rejects_scratch_user_root_symlink(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    project_repo = tmp_path / "project-repo"
    project_repo.mkdir()
    project_file = project_repo / "tracked.txt"
    project_file.write_text("project data", encoding="utf-8")
    scratch_root = db_path.parent / "local_runtime_workspaces" / "scratch"
    scratch_root.mkdir(parents=True)
    erased_user_scratch = scratch_root / "user-1"
    erased_user_scratch.symlink_to(project_repo, target_is_directory=True)

    with pytest.raises(MemoryCatalogIntegrityError):
        erase_user_memory_offline(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
            user_id="user-1",
        )

    assert erased_user_scratch.is_symlink()
    assert project_file.read_text(encoding="utf-8") == "project data"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT deletion.state, EXISTS(
                SELECT 1 FROM users WHERE user_id = deletion.user_id
            )
            FROM memory_artifact_deletions AS deletion
            WHERE deletion.user_id = 'user-1' AND reason = 'user_erasure'
            """
        ).fetchone() == ("quarantined", 1)


def test_pending_user_erasure_rejects_cross_tenant_quarantine_symlink(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    deletion_id = "erase-1"
    quarantine_relative_path = f"memory_catalog/erasure/user-1-{deletion_id}"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-2', 'ja', '2026-07-18T00:00:00Z',
                    '2026-07-18T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO memory_artifact_deletions(
                user_id, deletion_id, artifact_path, reason, state,
                created_at, updated_at
            ) VALUES (
                'user-1', ?, ?, 'user_erasure', 'database_detached',
                '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """,
            (deletion_id, quarantine_relative_path),
        )
    retained_user_root = artifact_root / "memory_catalog" / "users" / "user-2"
    retained_user_root.mkdir(parents=True)
    retained_file = retained_user_root / "keep.md"
    retained_file.write_text("tenant two", encoding="utf-8")
    quarantine_path = artifact_root / quarantine_relative_path
    quarantine_path.parent.mkdir(parents=True)
    quarantine_path.symlink_to(retained_user_root, target_is_directory=True)
    scratch_user_root = (
        db_path.parent / "local_runtime_workspaces" / "scratch" / "user-1"
    )
    scratch_user_root.mkdir(parents=True)

    with pytest.raises(MemoryCatalogIntegrityError):
        resume_pending_user_erasures(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            artifact_root=artifact_root,
        )

    assert quarantine_path.is_symlink()
    assert retained_file.read_text(encoding="utf-8") == "tenant two"
    assert not scratch_user_root.exists()
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE user_id = 'user-2'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            """
            SELECT state FROM memory_artifact_deletions
            WHERE user_id = 'user-1' AND deletion_id = ?
            """,
            (deletion_id,),
        ).fetchone() == ("database_detached",)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-1', 'ja', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_fact_structuring_runs(
                fact_run_id, user_id, fact_id, status, prompt_name,
                prompt_version, source_insight_ids, created_at, updated_at
            ) VALUES (
                'run-1', 'user-1', 'fact-1', 'processing', 'fact_structuring',
                '1.0', '[]', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO activity_logs(
                log_id, user_id, period_start, period_end, description, status,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES (
                'log-1', 'user-1', '2026-07-18T00:00:00Z',
                '2026-07-18T00:01:00Z', 'consumed evidence', 'success',
                'activity', '1.0', '2026-07-18T00:01:00Z',
                '2026-07-18T00:01:00Z'
            )
            """
        )
    _claim_memory_update_job(db_path)
    return db_path, artifact_root


def _claim_memory_update_job(db_path: Path) -> None:
    """A publication is bound to a running Memory run, so claim one."""

    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        request=build_local_memory_update_enqueue_request(MEMORY_JOB_PAYLOAD),
    )
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None


def _memory_run() -> MemoryRunBinding:
    return memory_run_binding_from_payload(MEMORY_JOB_PAYLOAD)


def _fact_draft(db_path: Path) -> MemoryDraftCheckpoint:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="fact",
                source_record_id="fact-1",
            )
    return create_memory_draft(
        user_id="user-1",
        owner_node_id=node.node_id,
        base_revision_id=None,
        documents=(MemoryDocument("facts/index.md", "# Facts\n- Durable\n"),),
    )


def _materialize_fact_intent(*, db_path: Path, artifact_root: Path) -> None:
    prepared = _create_fact_intent(db_path=db_path)
    materialize_artifact_revision(artifact_root=artifact_root, prepared=prepared)


def _create_fact_intent(
    *, db_path: Path
) -> publication_module.PreparedArtifactRevision:
    request = MemoryPublicationRequest(
        source="fact",
        source_record_id="fact-1",
        draft=_fact_draft(db_path),
        body_kind="artifact_tree",
        intent_kind="fact",
        domain_payload_json=(
            '{"base_sha256":null,"base_storage_path":null,'
            '"created_at":"2026-07-18T00:00:00Z","fact_id":"fact-1",'
            '"fact_run_id":"run-1","profile_brief":"brief",'
            '"prompt_name":"fact_structuring","prompt_version":"1.0",'
            '"source_activity_log_ids":["log-1"],'
            '"source_insight_ids":[],"user_id":"user-1"}'
        ),
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            prepared = create_artifact_revision_intent(
                connection=connection, request=request
            )
    return prepared


def _assert_fact_intent_pending(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        run_status = connection.execute(
            "SELECT status FROM agent_fact_structuring_runs WHERE fact_run_id = 'run-1'"
        ).fetchone()[0]
        intent_count = connection.execute(
            "SELECT COUNT(*) FROM memory_revision_intents"
        ).fetchone()[0]
        deletion_count = connection.execute(
            "SELECT COUNT(*) FROM memory_artifact_deletions"
        ).fetchone()[0]
    assert run_status == "processing"
    assert intent_count == 1
    assert deletion_count == 0


def _publication(draft: MemoryDraftCheckpoint) -> FactArtifactPublication:
    return FactArtifactPublication(
        memory_run=_memory_run(),
        user_id="user-1",
        fact_id="fact-1",
        draft=draft,
        prompt_name="memory_update",
        prompt_version="1.0",
        source_insight_ids=(),
        created_at="2026-07-18T00:00:00Z",
    )
