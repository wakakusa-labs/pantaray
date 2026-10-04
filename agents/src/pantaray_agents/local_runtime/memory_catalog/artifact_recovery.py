from __future__ import annotations

import logging
import os
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .agent_experience import (
    agent_experience_publication_from_intent,
    write_agent_experience_projection,
)
from .agent_experience_validation import build_agent_experience_evidence_edges
from .artifact_domain_publication import (
    fact_publication_from_intent,
    long_term_publication_from_intent,
    memory_run_from_intent_payload,
    write_fact_projection,
    write_long_term_projection,
)
from .artifact_manifest import (
    build_artifact_manifest,
    load_artifact_manifest,
    manifest_sha256,
    reconstruct_artifact_draft,
)
from .artifact_repair import (
    parse_repair_payload,
    validate_agent_experience_repair_draft,
    write_artifact_repair_projection,
)
from .artifact_tree_durability import fsync_artifact_tree
from .connection import open_memory_catalog_connection
from .errors import (
    MemoryArtifactTreeIncompleteError,
    MemoryCatalogError,
    MemoryCatalogIntegrityError,
)
from .memory_run_binding import validate_memory_run_intent_owner
from .models import (
    ArtifactIntent,
    MemoryDraftCheckpoint,
    MemoryIntentKind,
    MemorySource,
)
from .publication import (
    MemoryPublicationRequest,
    PreparedArtifactRevision,
    activate_artifact_revision,
    materialize_artifact_revision,
)
from .repository import (
    list_artifact_revision_intents,
    load_artifact_revision_intent_for_node,
    load_node_by_source,
)

logger = logging.getLogger(__name__)
RECOVERY_ERROR_CODE = "MEMORY_ARTIFACT_RECOVERY_FAILED"


def recover_artifact_revision_intents(
    *, db_path: Path, busy_timeout_ms: int, artifact_root: Path
) -> int:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        intents = list_artifact_revision_intents(connection)
    recovered = 0
    for intent in intents:
        if _intent_waits_for_its_memory_run(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            intent=intent,
        ):
            continue
        if _recover_one_intent(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=artifact_root,
            intent=intent,
        ):
            recovered += 1
    return recovered


def recover_pending_artifact_intent_for_source(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
    source: MemorySource,
    source_record_id: str,
) -> bool:
    """Recover the source node's intent while its user memory lock is held."""
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        node = load_node_by_source(
            connection=connection,
            user_id=user_id,
            source=source,
            source_record_id=source_record_id,
        )
        if node is None:
            return False
        intent = load_artifact_revision_intent_for_node(
            connection=connection,
            user_id=user_id,
            node_id=node.node_id,
        )
    if intent is None:
        return False
    return _recover_one_intent(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        intent=intent,
    )


def _recover_one_intent(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    intent: ArtifactIntent,
) -> bool:
    try:
        revision_root = _confined_path(artifact_root, intent.artifact_root_path)
        source, source_record_id = _intent_source(intent)
        raw_manifest = build_artifact_manifest(
            revision_id=intent.revision_id,
            source=source,
            source_record_id=source_record_id,
            draft=intent.draft,
        )
        if manifest_sha256(raw_manifest) != intent.manifest_sha256:
            raise MemoryCatalogIntegrityError(
                "artifact intent draft does not match manifest hash"
            )
        request = MemoryPublicationRequest(
            source=source,
            source_record_id=source_record_id,
            draft=intent.draft,
            body_kind="artifact_tree",
            revision_id=intent.revision_id,
            intent_kind=intent.intent_kind,
            domain_payload_json=intent.domain_payload_json,
        )
        prepared = PreparedArtifactRevision(
            request=request,
            revision_id=intent.revision_id,
            final_relative_path=intent.artifact_root_path,
            manifest_sha256=intent.manifest_sha256,
            manifest=raw_manifest,
        )
        is_materialized = _is_directory(revision_root)
        if is_materialized:
            try:
                _validate_materialized_intent(
                    revision_root=revision_root,
                    intent=intent,
                    expected_source=source,
                    expected_source_record_id=source_record_id,
                )
            except MemoryArtifactTreeIncompleteError:
                _rematerialize_incomplete_artifact_tree(
                    artifact_root=artifact_root,
                    revision_root=revision_root,
                    prepared=prepared,
                )
        else:
            materialize_artifact_revision(
                artifact_root=artifact_root,
                prepared=prepared,
            )
        with open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection:
            with immediate_transaction(connection):
                _activate_with_projection(
                    connection=connection,
                    prepared=prepared,
                    intent=intent,
                )
    except (MemoryCatalogError, ValueError) as exc:
        _fail_unrecoverable_intent(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            intent=intent,
        )
        logger.error(
            "memory artifact recovery rejected intent",
            extra={
                "user_id": intent.user_id,
                "node_id": intent.node_id,
                "revision_id": intent.revision_id,
                "error_code": RECOVERY_ERROR_CODE,
                "detail": type(exc).__name__,
            },
        )
        return False
    logger.info(
        "memory artifact intent activated during recovery",
        extra={
            "user_id": intent.user_id,
            "node_id": intent.node_id,
            "revision_id": intent.revision_id,
        },
    )
    return True


def _validate_materialized_intent(
    *,
    revision_root: Path,
    intent: ArtifactIntent,
    expected_source: MemorySource,
    expected_source_record_id: str,
) -> None:
    manifest = load_artifact_manifest(
        revision_root=revision_root,
        expected_manifest_sha256=intent.manifest_sha256,
        expected_revision_id=intent.revision_id,
        expected_user_id=intent.user_id,
        expected_node_id=intent.node_id,
    )
    source, source_record_id, materialized_draft = reconstruct_artifact_draft(
        revision_root=revision_root,
        manifest=manifest,
    )
    if (
        source != expected_source
        or source_record_id != expected_source_record_id
        or _artifact_bound_draft(materialized_draft)
        != _artifact_bound_draft(intent.draft)
    ):
        raise MemoryCatalogIntegrityError(
            "materialized artifact does not match durable intent draft"
        )


def _rematerialize_incomplete_artifact_tree(
    *,
    artifact_root: Path,
    revision_root: Path,
    prepared: PreparedArtifactRevision,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="memory-recovery-", dir=revision_root.parent
    ) as quarantine:
        quarantine_root = Path(quarantine)
        os.replace(revision_root, quarantine_root / revision_root.name)
        fsync_artifact_tree(
            tree_root=quarantine_root,
            ancestor_root=artifact_root,
        )
        materialize_artifact_revision(
            artifact_root=artifact_root,
            prepared=prepared,
        )
    fsync_artifact_tree(tree_root=revision_root, ancestor_root=artifact_root)


def _artifact_bound_draft(draft: MemoryDraftCheckpoint) -> MemoryDraftCheckpoint:
    return replace(
        draft,
        documents=tuple(sorted(draft.documents, key=lambda item: item.source_path)),
        applied_commands=(),
    )


def _is_directory(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False
    if not stat.S_ISDIR(mode):
        raise MemoryCatalogIntegrityError(
            "artifact intent path exists but is not a directory"
        )
    return True


def _activate_with_projection(
    *,
    connection: sqlite3.Connection,
    prepared: PreparedArtifactRevision,
    intent: ArtifactIntent,
) -> None:
    if intent.intent_kind == "fact":
        fact_publication = fact_publication_from_intent(
            payload_json=intent.domain_payload_json,
            draft=prepared.request.draft,
        )
        activate_artifact_revision(
            connection=connection,
            prepared=prepared,
            write_domain_projection=lambda conn, revision: write_fact_projection(
                connection=conn, revision=revision, publication=fact_publication
            ),
        )
        return
    if intent.intent_kind == "agent_experience":
        publication = agent_experience_publication_from_intent(
            payload_json=intent.domain_payload_json,
            draft=prepared.request.draft,
        )
        recovered_request = replace(
            prepared.request,
            evidence_edges=build_agent_experience_evidence_edges(
                connection=connection,
                draft=prepared.request.draft,
                revision_id=prepared.revision_id,
            ),
        )
        recovered_prepared = replace(prepared, request=recovered_request)
        activate_artifact_revision(
            connection=connection,
            prepared=recovered_prepared,
            write_domain_projection=lambda conn, revision: (
                write_agent_experience_projection(
                    connection=conn,
                    revision=revision,
                    publication=publication,
                )
            ),
        )
        return
    if intent.intent_kind in {
        "fact_repair",
        "long_term_insight_repair",
        "agent_experience_repair",
    }:
        user_id, source_record_id = parse_repair_payload(intent.domain_payload_json)
        source = _repair_source(intent.intent_kind)
        if user_id != intent.user_id:
            raise MemoryCatalogIntegrityError("repair intent user does not match")
        repair_prepared = prepared
        if source == "agent_experience":
            validate_agent_experience_repair_draft(
                connection=connection,
                draft=prepared.request.draft,
                source_record_id=source_record_id,
            )
            repair_prepared = replace(
                prepared,
                request=replace(
                    prepared.request,
                    evidence_edges=build_agent_experience_evidence_edges(
                        connection=connection,
                        draft=prepared.request.draft,
                        revision_id=prepared.revision_id,
                    ),
                ),
            )
        activate_artifact_revision(
            connection=connection,
            prepared=repair_prepared,
            write_domain_projection=lambda conn, revision: (
                write_artifact_repair_projection(
                    connection=conn,
                    revision=revision,
                    source=source,
                    source_record_id=source_record_id,
                )
            ),
        )
        return
    long_term_publication = long_term_publication_from_intent(
        payload_json=intent.domain_payload_json,
        draft=prepared.request.draft,
    )
    activate_artifact_revision(
        connection=connection,
        prepared=prepared,
        write_domain_projection=lambda conn, revision: write_long_term_projection(
            connection=conn, revision=revision, publication=long_term_publication
        ),
    )


def _fail_unrecoverable_intent(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    intent: ArtifactIntent,
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            now = now_utc_iso()
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_artifact_deletions(
                    user_id, deletion_id, artifact_path, reason, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'revision_gc', 'planned', ?, ?)
                """,
                (
                    intent.user_id,
                    f"del_{uuid.uuid4().hex}",
                    intent.artifact_root_path,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM memory_revision_intents WHERE user_id = ? AND revision_id = ?",
                (intent.user_id, intent.revision_id),
            )
            connection.execute(
                """
                DELETE FROM memory_nodes
                WHERE user_id = ? AND node_id = ? AND lifecycle = 'preparing'
                  AND current_revision_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_revision_intents AS intents
                      WHERE intents.user_id = memory_nodes.user_id
                        AND intents.node_id = memory_nodes.node_id
                  )
                """,
                (intent.user_id, intent.node_id),
            )


def _empty_failure_draft(intent: ArtifactIntent) -> MemoryDraftCheckpoint:
    return MemoryDraftCheckpoint(
        draft_session_id="recovery_failure",
        user_id=intent.user_id,
        owner_node_id=intent.node_id,
        base_revision_id=None,
        draft_revision="recovery_failure",
        documents=(),
        links=(),
        applied_commands=(),
    )


def _intent_waits_for_its_memory_run(
    *, db_path: Path, busy_timeout_ms: int, intent: ArtifactIntent
) -> bool:
    """A unified Memory run reclaims its own intents when it runs again.

    Every category the run publishes carries the same run binding, so a job
    that was deferred back to the queue keeps all of them, not only its Agent
    Experience one.
    """

    try:
        binding = memory_run_from_intent_payload(intent.domain_payload_json)
    except (MemoryCatalogError, ValueError):
        return False
    if binding is None:
        return False
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        try:
            validate_memory_run_intent_owner(connection=connection, binding=binding)
        except MemoryCatalogIntegrityError:
            return False
    return True


def _intent_source(intent: ArtifactIntent) -> tuple[MemorySource, str]:
    if intent.intent_kind == "fact":
        fact_publication = fact_publication_from_intent(
            payload_json=intent.domain_payload_json,
            draft=_empty_failure_draft(intent),
        )
        source: MemorySource = "fact"
        source_record_id = fact_publication.fact_id
        domain_user_id = fact_publication.user_id
    elif intent.intent_kind == "long_term_insight":
        long_term_publication = long_term_publication_from_intent(
            payload_json=intent.domain_payload_json,
            draft=_empty_failure_draft(intent),
        )
        source = "long_term_insight"
        source_record_id = long_term_publication.user_id
        domain_user_id = long_term_publication.user_id
    elif intent.intent_kind == "agent_experience":
        publication = agent_experience_publication_from_intent(
            payload_json=intent.domain_payload_json,
            draft=_empty_failure_draft(intent),
        )
        source = "agent_experience"
        source_record_id = publication.binding.user_id
        domain_user_id = publication.binding.user_id
    else:
        user_id, repair_source_id = parse_repair_payload(intent.domain_payload_json)
        source = _repair_source(intent.intent_kind)
        source_record_id = repair_source_id
        domain_user_id = user_id
    if domain_user_id != intent.user_id:
        raise MemoryCatalogIntegrityError("intent user does not match domain payload")
    return source, source_record_id


def _repair_source(intent_kind: MemoryIntentKind) -> MemorySource:
    sources: dict[MemoryIntentKind, MemorySource] = {
        "fact_repair": "fact",
        "long_term_insight_repair": "long_term_insight",
        "agent_experience_repair": "agent_experience",
    }
    source = sources.get(intent_kind)
    if source is None:
        raise MemoryCatalogIntegrityError("artifact repair intent kind is invalid")
    return source


def _confined_path(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise MemoryCatalogIntegrityError("artifact intent path escapes runtime root")
    return candidate
