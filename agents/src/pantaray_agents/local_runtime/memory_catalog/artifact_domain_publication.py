from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from .errors import MemoryCatalogIntegrityError
from .memory_run_binding import (
    MemoryRunBinding,
    memory_run_binding_from_intent,
    memory_run_binding_payload,
    record_memory_run_category,
    validate_memory_run_runtime,
)
from .models import MemoryDraftCheckpoint, MemoryRevision
from .publication import (
    MemoryPublicationRequest,
    publish_artifact_revision,
)


@dataclass(frozen=True, slots=True)
class FactArtifactPublication:
    """One Fact revision and the unified Memory run that produced it.

    ``memory_run`` is absent only while recovering an intent a retired per-Fact
    run left pending: that run's ledger is closed, so the recovery writes the
    projection and records nothing else.
    """

    user_id: str
    fact_id: str
    memory_run: MemoryRunBinding | None
    draft: MemoryDraftCheckpoint
    profile_brief: str
    prompt_name: str
    prompt_version: str
    source_insight_ids: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class LongTermInsightArtifactPublication:
    """One long-term Insight revision and the run that produced it.

    ``source_run_id`` names that run on the durable state row; ``memory_run`` is
    absent only for an intent a retired Insight update run left pending.
    """

    user_id: str
    memory_run: MemoryRunBinding | None
    source_run_id: str
    draft: MemoryDraftCheckpoint
    profile_brief: str
    prompt_name: str
    prompt_version: str

    def __post_init__(self) -> None:
        if self.memory_run is not None and self.memory_run.job_id != self.source_run_id:
            raise ValueError("long-term Insight state must name its producing run")


def publish_fact_artifact(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    publication: FactArtifactPublication,
) -> MemoryRevision:
    return publish_artifact_revision(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        build_request=lambda connection: _validated_request(
            connection=connection,
            memory_run=publication.memory_run,
            request=MemoryPublicationRequest(
                source="fact",
                source_record_id=publication.fact_id,
                draft=publication.draft,
                body_kind="artifact_tree",
                intent_kind="fact",
                domain_payload_json=_fact_payload_json(publication),
            ),
        ),
        write_domain_projection=lambda connection, revision: write_fact_projection(
            connection=connection,
            revision=revision,
            publication=publication,
        ),
    )


def publish_long_term_insight_artifact(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    publication: LongTermInsightArtifactPublication,
) -> MemoryRevision:
    return publish_artifact_revision(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        build_request=lambda connection: _validated_request(
            connection=connection,
            memory_run=publication.memory_run,
            request=MemoryPublicationRequest(
                source="long_term_insight",
                source_record_id=publication.user_id,
                draft=publication.draft,
                body_kind="artifact_tree",
                intent_kind="long_term_insight",
                domain_payload_json=_long_term_payload_json(publication),
            ),
        ),
        write_domain_projection=lambda connection, revision: write_long_term_projection(
            connection=connection,
            revision=revision,
            publication=publication,
        ),
    )


def write_fact_projection(
    *,
    connection: sqlite3.Connection,
    revision: MemoryRevision,
    publication: FactArtifactPublication,
) -> None:
    now = now_utc_iso()
    _set_revision_profile_brief(
        connection=connection,
        revision=revision,
        profile_brief=publication.profile_brief,
    )
    final_path = _index_storage_path(revision, "facts/index.md")
    source_ids = json.dumps(publication.source_insight_ids, ensure_ascii=False)
    fact_cursor = connection.execute(
        """
        INSERT INTO agent_facts(
            fact_id, user_id, status, facts_profile_brief, error, prompt_text,
            response_text, llm_output, prompt_name, prompt_version,
            source_insight_ids, structured_fact_storage_path,
            structured_fact_sha256, created_at, updated_at
        ) VALUES (?, ?, 'success', ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fact_id) DO UPDATE SET
            status = 'success',
            facts_profile_brief = excluded.facts_profile_brief,
            error = NULL,
            prompt_name = excluded.prompt_name,
            prompt_version = excluded.prompt_version,
            source_insight_ids = excluded.source_insight_ids,
            structured_fact_storage_path = excluded.structured_fact_storage_path,
            structured_fact_sha256 = excluded.structured_fact_sha256,
            updated_at = excluded.updated_at
        WHERE agent_facts.user_id = excluded.user_id
        """,
        (
            publication.fact_id,
            publication.user_id,
            publication.profile_brief,
            publication.prompt_name,
            publication.prompt_version,
            source_ids,
            final_path,
            revision.content_sha256,
            publication.created_at,
            now,
        ),
    )
    if fact_cursor.rowcount != 1:
        raise MemoryCatalogIntegrityError("Fact publication owner does not match")
    if publication.memory_run is not None:
        validate_memory_run_runtime(
            connection=connection, binding=publication.memory_run
        )
        record_memory_run_category(
            connection=connection,
            binding=publication.memory_run,
            source="fact",
            revision=revision,
        )


def write_long_term_projection(
    *,
    connection: sqlite3.Connection,
    revision: MemoryRevision,
    publication: LongTermInsightArtifactPublication,
) -> None:
    now = now_utc_iso()
    _set_revision_profile_brief(
        connection=connection,
        revision=revision,
        profile_brief=publication.profile_brief,
    )
    final_path = _index_storage_path(revision, "insights/index.md")
    _upsert_long_term_state(
        connection=connection,
        publication=publication,
        final_path=final_path,
        content_sha256=revision.content_sha256,
        now=now,
    )
    if publication.memory_run is not None:
        validate_memory_run_runtime(
            connection=connection, binding=publication.memory_run
        )
        record_memory_run_category(
            connection=connection,
            binding=publication.memory_run,
            source="long_term_insight",
            revision=revision,
        )


def _upsert_long_term_state(
    *,
    connection: sqlite3.Connection,
    publication: LongTermInsightArtifactPublication,
    final_path: str,
    content_sha256: str,
    now: str,
) -> None:
    """One long-term Insight state row per user, replaced by its newest run.

    A run consumes several short-term Insights, so the row names no single
    source Insight.
    """

    connection.execute(
        """
        INSERT INTO agent_long_term_insight_state(
            user_id, source_insight_id, source_run_id, storage_path, sha256,
            insight_profile_brief, created_at, updated_at
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            source_insight_id = NULL,
            source_run_id = excluded.source_run_id,
            storage_path = excluded.storage_path,
            sha256 = excluded.sha256,
            insight_profile_brief = excluded.insight_profile_brief,
            updated_at = excluded.updated_at
        """,
        (
            publication.user_id,
            publication.source_run_id,
            final_path,
            content_sha256,
            publication.profile_brief,
            now,
            now,
        ),
    )


def _validated_request(
    *,
    connection: sqlite3.Connection,
    memory_run: MemoryRunBinding | None,
    request: MemoryPublicationRequest,
) -> MemoryPublicationRequest:
    if memory_run is not None:
        validate_memory_run_runtime(connection=connection, binding=memory_run)
    return request


def _index_storage_path(revision: MemoryRevision, relative_path: str) -> str:
    if revision.artifact_root_path is None:
        raise ValueError("artifact revision has no storage path")
    return f"{revision.artifact_root_path.rstrip('/')}/{relative_path}"


def _set_revision_profile_brief(
    *,
    connection: sqlite3.Connection,
    revision: MemoryRevision,
    profile_brief: str,
) -> None:
    brief = profile_brief.strip()
    if not brief:
        raise MemoryCatalogIntegrityError("artifact profile brief must not be blank")
    cursor = connection.execute(
        """
        UPDATE memory_revisions SET profile_brief = ?
        WHERE user_id = ? AND revision_id = ? AND profile_brief IS NULL
        """,
        (brief, revision.user_id, revision.revision_id),
    )
    if cursor.rowcount != 1:
        raise MemoryCatalogIntegrityError(
            "artifact revision profile brief is not writable"
        )


def _fact_payload_json(publication: FactArtifactPublication) -> str:
    return json.dumps(
        {
            "user_id": publication.user_id,
            "fact_id": publication.fact_id,
            "memory_run": _memory_run_payload(publication.memory_run),
            "profile_brief": publication.profile_brief,
            "prompt_name": publication.prompt_name,
            "prompt_version": publication.prompt_version,
            "source_insight_ids": list(publication.source_insight_ids),
            "created_at": publication.created_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _long_term_payload_json(publication: LongTermInsightArtifactPublication) -> str:
    return json.dumps(
        {
            "user_id": publication.user_id,
            "memory_run": _memory_run_payload(publication.memory_run),
            "profile_brief": publication.profile_brief,
            "prompt_name": publication.prompt_name,
            "prompt_version": publication.prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _memory_run_payload(binding: MemoryRunBinding | None) -> JSONValue:
    return None if binding is None else memory_run_binding_payload(binding)


def _memory_run_from_intent(payload: dict[str, object]) -> MemoryRunBinding | None:
    value = payload.get("memory_run")
    return None if value is None else memory_run_binding_from_intent(value)


def memory_run_from_intent_payload(payload_json: str) -> MemoryRunBinding | None:
    """The Memory run bound to a durable intent, whatever category it publishes."""

    return _memory_run_from_intent(_load_payload(payload_json))


def fact_publication_from_intent(
    *, payload_json: str, draft: MemoryDraftCheckpoint
) -> FactArtifactPublication:
    payload = _load_payload(payload_json)
    source_ids = payload.get("source_insight_ids")
    if not isinstance(source_ids, list) or not all(
        isinstance(item, str) for item in source_ids
    ):
        raise MemoryCatalogIntegrityError("Fact intent source IDs are invalid")
    return FactArtifactPublication(
        user_id=_require_payload_string(payload, "user_id"),
        fact_id=_require_payload_string(payload, "fact_id"),
        memory_run=_memory_run_from_intent(payload),
        draft=draft,
        profile_brief=_require_payload_string(payload, "profile_brief"),
        prompt_name=_require_payload_string(payload, "prompt_name"),
        prompt_version=_require_payload_string(payload, "prompt_version"),
        source_insight_ids=tuple(source_ids),
        created_at=_require_payload_string(payload, "created_at"),
    )


def long_term_publication_from_intent(
    *, payload_json: str, draft: MemoryDraftCheckpoint
) -> LongTermInsightArtifactPublication:
    payload = _load_payload(payload_json)
    memory_run = _memory_run_from_intent(payload)
    # An intent a retired Insight update run left pending names that run instead.
    source_run_id = (
        memory_run.job_id
        if memory_run is not None
        else _require_payload_string(payload, "insight_update_id")
    )
    return LongTermInsightArtifactPublication(
        user_id=_require_payload_string(payload, "user_id"),
        memory_run=memory_run,
        source_run_id=source_run_id,
        draft=draft,
        profile_brief=_require_payload_string(payload, "profile_brief"),
        prompt_name=_require_payload_string(payload, "prompt_name"),
        prompt_version=_require_payload_string(payload, "prompt_version"),
    )


def _load_payload(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise MemoryCatalogIntegrityError("artifact intent payload is invalid") from exc
    if not isinstance(payload, dict):
        raise MemoryCatalogIntegrityError("artifact intent payload must be an object")
    return payload


def _require_payload_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryCatalogIntegrityError(f"artifact intent {key} is invalid")
    return value
