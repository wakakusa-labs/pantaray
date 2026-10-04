from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.embedding_local import (
    LocalEmbeddingModel,
    LocalEmbeddingUnavailableError,
    load_local_embedding_model,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_consistency import (
    has_unfinished_embedding_work,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    EmbeddingGeneration,
    MemoryEmbeddingActivationError,
    activate_embedding_generation,
    current_embedding_specification,
    ensure_user_embedding_generation,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_work import (
    PendingMemoryEmbedding,
    claim_due_embeddings,
    mark_embedding_work_failed,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    MemoryEmbeddingValidationError,
    commit_embedding_projection_batch,
    encode_embedding_vectors,
    validate_pending_embedding,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

logger = logging.getLogger(__name__)

# How often one chunk may be claimed before the runtime gives up on it. Without
# a cap, a chunk the model cannot process is re-claimed on every pass forever.
MEMORY_EMBEDDING_MAX_ATTEMPTS = 3
MEMORY_EMBEDDING_INFERENCE_FAILED = "MEMORY_EMBEDDING_INFERENCE_FAILED"


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingProjectionResult:
    selected_count: int
    indexed_count: int
    failed_count: int

    def followed_by(
        self, other: MemoryEmbeddingProjectionResult
    ) -> MemoryEmbeddingProjectionResult:
        return MemoryEmbeddingProjectionResult(
            selected_count=self.selected_count + other.selected_count,
            indexed_count=self.indexed_count + other.indexed_count,
            failed_count=self.failed_count + other.failed_count,
        )


def run_memory_embedding_projection(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    stop_event: threading.Event | None = None,
) -> MemoryEmbeddingProjectionResult:
    """Index this owner's outstanding chunks until none are left.

    Indexing runs entirely on the local CPU now, so a pass keeps going instead
    of handing one chunk back to a scheduler that waits a second before asking
    again; the owner would otherwise wait hours for a first index.
    """
    try:
        model = load_local_embedding_model()
    except LocalEmbeddingUnavailableError:
        # The loader has already reported why. Without a model there is nothing
        # to index and no generation to create; memory search says
        # `provider_unavailable` until the model is installed and the runtime is
        # restarted.
        return MemoryEmbeddingProjectionResult(0, 0, 0)
    # The installed artifact decides the generation, so a new model revision
    # starts a new build here instead of adding its vectors to the old index.
    generation = ensure_user_embedding_generation(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        specification=current_embedding_specification(model.manifest),
    )
    total = MemoryEmbeddingProjectionResult(0, 0, 0)
    while stop_event is None or not stop_event.is_set():
        batch = _project_one_batch(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            generation=generation,
            model=model,
        )
        total = total.followed_by(batch)
        if batch.selected_count == 0:
            break
    _activate_completed_build(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        generation=generation,
    )
    return total


def _project_one_batch(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    generation: EmbeddingGeneration,
    model: LocalEmbeddingModel,
) -> MemoryEmbeddingProjectionResult:
    now = now_utc_iso()
    documents = claim_due_embeddings(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        generation_id=generation.generation_id,
        now=now,
    )
    if not documents:
        return MemoryEmbeddingProjectionResult(0, 0, 0)

    valid_documents, failed_count = _reject_invalid_documents(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        documents=documents,
    )
    if not valid_documents:
        return MemoryEmbeddingProjectionResult(
            selected_count=len(documents),
            indexed_count=0,
            failed_count=failed_count,
        )

    try:
        encoded_vectors = encode_embedding_vectors(
            model.embed_documents(
                [document.content_text for document in valid_documents]
            ),
            expected_count=len(valid_documents),
            generation=valid_documents[0].generation,
        )
    except MemoryEmbeddingValidationError as exc:
        failed_count += mark_embedding_work_failed(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            documents=valid_documents,
            error_code=exc.error_code,
            updated_at=now_utc_iso(),
        )
        return MemoryEmbeddingProjectionResult(
            selected_count=len(documents),
            indexed_count=0,
            failed_count=failed_count,
        )
    # onnxruntime and tokenizers report an unusable input or an exhausted
    # machine through exception types with no common base of their own. Without
    # this the claimed rows stay pending and the same chunk is re-claimed on
    # every pass, forever.
    except Exception:
        exhausted = [
            document
            for document in valid_documents
            if document.attempt_count >= MEMORY_EMBEDDING_MAX_ATTEMPTS
        ]
        logger.warning(
            "memory embedding inference failed",
            exc_info=True,
            extra={"exhausted_chunk_count": len(exhausted)},
        )
        if exhausted:
            failed_count += mark_embedding_work_failed(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                documents=exhausted,
                error_code=MEMORY_EMBEDDING_INFERENCE_FAILED,
                updated_at=now_utc_iso(),
            )
        return MemoryEmbeddingProjectionResult(
            selected_count=0,
            indexed_count=0,
            failed_count=failed_count,
        )

    indexed_count = commit_embedding_projection_batch(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        documents=valid_documents,
        encoded_vectors=encoded_vectors,
        created_at=now_utc_iso(),
    )
    return MemoryEmbeddingProjectionResult(
        selected_count=len(documents),
        indexed_count=indexed_count,
        failed_count=failed_count,
    )


def _activate_completed_build(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    generation: EmbeddingGeneration,
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        state = connection.execute(
            """
            SELECT state FROM memory_embedding_user_generations
            WHERE user_id = ? AND generation_id = ?
            """,
            (user_id, generation.generation_id),
        ).fetchone()
        has_unfinished_work = has_unfinished_embedding_work(
            connection,
            user_id=user_id,
            generation_id=generation.generation_id,
        )
    if state is None or str(state[0]) != "building" or has_unfinished_work:
        return
    try:
        activate_embedding_generation(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            generation_id=generation.generation_id,
        )
    except MemoryEmbeddingActivationError:
        # The catalog does not yet describe what the index holds -- a fragment
        # whose stored hash disagrees with its text, for instance. Repair owns
        # that, and the next pass activates once it has run.
        logger.warning(
            "memory embedding generation is not ready to activate",
            exc_info=True,
            extra={"generation_id": generation.generation_id},
        )


def _reject_invalid_documents(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    documents: Sequence[PendingMemoryEmbedding],
) -> tuple[tuple[PendingMemoryEmbedding, ...], int]:
    valid_documents: list[PendingMemoryEmbedding] = []
    failures_by_code: dict[str, list[PendingMemoryEmbedding]] = {}
    for document in documents:
        try:
            validate_pending_embedding(document)
        except MemoryEmbeddingValidationError as exc:
            failures_by_code.setdefault(exc.error_code, []).append(document)
        else:
            valid_documents.append(document)
    failed_count = 0
    for error_code, failed_documents in failures_by_code.items():
        failed_count += mark_embedding_work_failed(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            documents=failed_documents,
            error_code=error_code,
            updated_at=now_utc_iso(),
        )
    return tuple(valid_documents), failed_count


__all__ = [
    "MEMORY_EMBEDDING_INFERENCE_FAILED",
    "MEMORY_EMBEDDING_MAX_ATTEMPTS",
    "MemoryEmbeddingProjectionResult",
    "run_memory_embedding_projection",
]
