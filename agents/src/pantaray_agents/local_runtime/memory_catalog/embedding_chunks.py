"""Splitting a memory fragment into the chunks that get embedded.

The split is by character count, not by tokens: the same split is computed in
SQL when embedding work is enqueued, and it has to produce the same chunks
whether or not the embedding model is installed. The generation's
`max_text_chars` comes from the model manifest's token window, small enough that
a chunk cannot overflow it (see `embedding_local.manifest`).
"""

from __future__ import annotations

from dataclasses import dataclass

from .fragments import content_sha256


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingChunk:
    chunk_index: int
    content_text: str
    content_sha256: str


def split_memory_embedding_chunks(
    content_text: str,
    *,
    max_text_chars: int,
) -> tuple[MemoryEmbeddingChunk, ...]:
    chunk_size = _require_max_text_chars(max_text_chars)
    return tuple(
        MemoryEmbeddingChunk(
            chunk_index=chunk_index,
            content_text=chunk_text,
            content_sha256=content_sha256(chunk_text),
        )
        for chunk_index, start in enumerate(range(0, len(content_text), chunk_size))
        if (chunk_text := content_text[start : start + chunk_size])
    )


def memory_embedding_chunk_at(
    content_text: str,
    *,
    chunk_index: int,
    max_text_chars: int,
) -> MemoryEmbeddingChunk:
    if chunk_index < 0:
        raise ValueError("embedding chunk_index must not be negative")
    chunk_size = _require_max_text_chars(max_text_chars)
    start = chunk_index * chunk_size
    chunk_text = content_text[start : start + chunk_size]
    if not chunk_text:
        raise ValueError("embedding chunk_index is outside the fragment")
    return MemoryEmbeddingChunk(
        chunk_index=chunk_index,
        content_text=chunk_text,
        content_sha256=content_sha256(chunk_text),
    )


def validate_memory_embedding_chunk(
    *,
    fragment_content_text: str,
    fragment_content_sha256: str,
    chunk_index: int,
    chunk_content_sha256: str,
    max_text_chars: int,
) -> None:
    if content_sha256(fragment_content_text) != fragment_content_sha256:
        raise ValueError("embedding fragment content hash is invalid")
    chunk = memory_embedding_chunk_at(
        fragment_content_text,
        chunk_index=chunk_index,
        max_text_chars=max_text_chars,
    )
    if chunk.content_sha256 != chunk_content_sha256:
        raise ValueError("embedding chunk content hash is invalid")


def _require_max_text_chars(max_text_chars: int) -> int:
    if isinstance(max_text_chars, bool) or not isinstance(max_text_chars, int):
        raise TypeError("embedding max_text_chars must be an integer")
    if max_text_chars <= 0:
        raise ValueError("embedding max_text_chars must be positive")
    return max_text_chars


__all__ = [
    "MemoryEmbeddingChunk",
    "memory_embedding_chunk_at",
    "split_memory_embedding_chunks",
    "validate_memory_embedding_chunk",
]
