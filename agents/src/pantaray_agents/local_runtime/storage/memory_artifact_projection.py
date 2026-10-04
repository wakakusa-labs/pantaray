from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from pantaray_agents.utils.memory_source_policy import EMPTY_TEXT_SHA256_HEX
from pantaray_agents.utils.timestamps import format_iso8601_utc_z_milliseconds

MemoryProjectionSource = Literal["long_term_insight", "facts"]
MemoryBlockKind = Literal["heading", "paragraph", "list_item"]

TEXT_MARKDOWN_MIME_TYPE = "text/markdown; charset=utf-8"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class MemoryArtifactProjection:
    user_id: str
    source_type: MemoryProjectionSource
    source_record_id: str
    root_path: str
    relative_path: str
    content: str | None
    logical_created_at: str
    logical_updated_at: str
    sha256: str | None = None
    byte_size: int | None = None
    mime_type: str = TEXT_MARKDOWN_MIME_TYPE


@dataclass(frozen=True, slots=True)
class MemorySearchBlock:
    block_kind: MemoryBlockKind
    heading_path: str | None
    block_index: int
    search_text: str
    preview_text: str
    start_offset: int
    end_offset: int


def upsert_memory_artifact_projection(
    *,
    db_path: str,
    busy_timeout_ms: int,
    projection: MemoryArtifactProjection,
) -> None:
    upsert_memory_artifact_file_projections(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        projections=(projection,),
    )


def upsert_memory_artifact_file_projections(
    *,
    db_path: str,
    busy_timeout_ms: int,
    projections: tuple[MemoryArtifactProjection, ...],
) -> None:
    if not projections:
        raise ValueError("projections must not be empty")
    for projection in projections:
        _validate_projection(projection)
    first = projections[0]
    if any(
        projection.user_id != first.user_id
        or projection.source_type != first.source_type
        or projection.source_record_id != first.source_record_id
        or projection.root_path != first.root_path
        for projection in projections
    ):
        raise ValueError("all projections must belong to the same memory artifact")
    relative_paths = [projection.relative_path for projection in projections]
    if len(set(relative_paths)) != len(relative_paths):
        raise ValueError("projection relative_path values must be unique")
    artifact_id = _stable_id(
        "artifact",
        first.user_id,
        first.source_type,
        first.source_record_id,
    )
    now = _utc_now()
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        with connection:
            connection.execute(
                """
                INSERT INTO memory_artifacts(
                    artifact_id,
                    user_id,
                    source_type,
                    source_record_id,
                    root_path,
                    content_sha256,
                    logical_created_at,
                    logical_updated_at,
                    indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, source_type, source_record_id) DO UPDATE SET
                    root_path = excluded.root_path,
                    content_sha256 = excluded.content_sha256,
                    logical_created_at = excluded.logical_created_at,
                    logical_updated_at = excluded.logical_updated_at,
                    indexed_at = excluded.indexed_at
                """,
                (
                    artifact_id,
                    first.user_id,
                    first.source_type,
                    first.source_record_id,
                    first.root_path,
                    _sha256_artifact_files(projections),
                    first.logical_created_at,
                    first.logical_updated_at,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT artifact_id
                FROM memory_artifacts
                WHERE user_id = ? AND source_type = ? AND source_record_id = ?
                LIMIT 1
                """,
                (
                    first.user_id,
                    first.source_type,
                    first.source_record_id,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("memory_artifacts upsert did not return a row")
            stored_artifact_id = str(row["artifact_id"])
            old_blocks = connection.execute(
                """
                SELECT blocks.block_rowid, blocks.search_text
                FROM memory_artifact_blocks AS blocks
                JOIN memory_artifact_files AS files
                  ON files.file_id = blocks.file_id
                WHERE files.artifact_id = ?
                """,
                (stored_artifact_id,),
            ).fetchall()
            for old_block in old_blocks:
                _delete_fts_block(
                    connection=connection,
                    block_rowid=int(old_block["block_rowid"]),
                    search_text=str(old_block["search_text"] or ""),
                )
            connection.execute(
                """
                DELETE FROM memory_artifact_blocks
                WHERE file_id IN (
                    SELECT file_id
                    FROM memory_artifact_files
                    WHERE artifact_id = ?
                )
                """,
                (stored_artifact_id,),
            )
            connection.execute(
                "DELETE FROM memory_artifact_files WHERE artifact_id = ?",
                (stored_artifact_id,),
            )
            for projection in projections:
                file_id = _stable_id(
                    "file", stored_artifact_id, projection.relative_path
                )
                connection.execute(
                    """
                    INSERT INTO memory_artifact_files(
                        file_id,
                        artifact_id,
                        relative_path,
                        sha256,
                        byte_size,
                        mime_type,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        stored_artifact_id,
                        projection.relative_path,
                        _projection_sha256(projection),
                        _projection_byte_size(projection),
                        projection.mime_type,
                        projection.logical_updated_at,
                    ),
                )
                if projection.content is None:
                    continue
                for block in parse_memory_search_blocks(projection.content):
                    _insert_block(
                        connection=connection,
                        file_id=file_id,
                        artifact_id=stored_artifact_id,
                        relative_path=projection.relative_path,
                        block=block,
                    )


def parse_memory_search_blocks(text: str) -> tuple[MemorySearchBlock, ...]:
    blocks: list[MemorySearchBlock] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph_lines: list[str] = []
    paragraph_start: int | None = None
    offset = 0
    block_index = 0
    for line in text.splitlines(keepends=True):
        raw_line = line.removesuffix("\n")
        line_text = raw_line.removesuffix("\r")
        heading = _HEADING_RE.match(line_text)
        list_item = _LIST_ITEM_RE.match(line_text)
        if heading is not None or list_item is not None or not line_text.strip():
            block_index = _flush_paragraph(
                blocks=blocks,
                heading_path=_format_heading_path(heading_stack),
                paragraph_lines=paragraph_lines,
                paragraph_start=paragraph_start,
                paragraph_end=offset,
                block_index=block_index,
            )
            paragraph_start = None
        if heading is not None:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            heading_stack = [
                (item_level, item_text)
                for item_level, item_text in heading_stack
                if item_level < level
            ]
            heading_stack.append((level, text))
            blocks.append(
                _block(
                    kind="heading",
                    heading_path=_format_heading_path(heading_stack),
                    block_index=block_index,
                    text=text,
                    start=offset,
                    end=offset + len(line),
                )
            )
            block_index += 1
        elif list_item is not None:
            text = list_item.group(1).strip()
            blocks.append(
                _block(
                    kind="list_item",
                    heading_path=_format_heading_path(heading_stack),
                    block_index=block_index,
                    text=text,
                    start=offset,
                    end=offset + len(line),
                )
            )
            block_index += 1
        elif line_text.strip():
            if paragraph_start is None:
                paragraph_start = offset
            paragraph_lines.append(line_text.strip())
        offset += len(line)
    _flush_paragraph(
        blocks=blocks,
        heading_path=_format_heading_path(heading_stack),
        paragraph_lines=paragraph_lines,
        paragraph_start=paragraph_start,
        paragraph_end=offset,
        block_index=block_index,
    )
    return tuple(blocks)


def parse_markdown_search_blocks(markdown: str) -> tuple[MemorySearchBlock, ...]:
    return parse_memory_search_blocks(markdown)


def _insert_block(
    *,
    connection: sqlite3.Connection,
    file_id: str,
    artifact_id: str,
    relative_path: str,
    block: MemorySearchBlock,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO memory_artifact_blocks(
            block_id,
            file_id,
            block_kind,
            heading_path,
            block_index,
            search_text,
            preview_text,
            start_offset,
            end_offset
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id(
                "block",
                artifact_id,
                relative_path,
                str(block.block_index),
                block.search_text,
            ),
            file_id,
            block.block_kind,
            block.heading_path,
            block.block_index,
            block.search_text,
            block.preview_text,
            block.start_offset,
            block.end_offset,
        ),
    )
    _insert_fts_block(
        connection=connection,
        block_rowid=int(cursor.lastrowid),
        search_text=block.search_text,
    )


def _insert_fts_block(
    *,
    connection: sqlite3.Connection,
    block_rowid: int,
    search_text: str,
) -> None:
    connection.execute(
        """
        INSERT INTO memory_artifact_blocks_fts(rowid, search_text)
        VALUES (?, ?)
        """,
        (block_rowid, search_text),
    )


def _delete_fts_block(
    *,
    connection: sqlite3.Connection,
    block_rowid: int,
    search_text: str,
) -> None:
    connection.execute(
        """
        INSERT INTO memory_artifact_blocks_fts(memory_artifact_blocks_fts, rowid, search_text)
        VALUES('delete', ?, ?)
        """,
        (block_rowid, search_text),
    )


def _flush_paragraph(
    *,
    blocks: list[MemorySearchBlock],
    heading_path: str | None,
    paragraph_lines: list[str],
    paragraph_start: int | None,
    paragraph_end: int,
    block_index: int,
) -> int:
    if not paragraph_lines or paragraph_start is None:
        paragraph_lines.clear()
        return block_index
    text = " ".join(paragraph_lines).strip()
    blocks.append(
        _block(
            kind="paragraph",
            heading_path=heading_path,
            block_index=block_index,
            text=text,
            start=paragraph_start,
            end=paragraph_end,
        )
    )
    paragraph_lines.clear()
    return block_index + 1


def _block(
    *,
    kind: MemoryBlockKind,
    heading_path: str | None,
    block_index: int,
    text: str,
    start: int,
    end: int,
) -> MemorySearchBlock:
    return MemorySearchBlock(
        block_kind=kind,
        heading_path=heading_path,
        block_index=block_index,
        search_text=text,
        preview_text=text[:500],
        start_offset=start,
        end_offset=end,
    )


def _validate_projection(projection: MemoryArtifactProjection) -> None:
    if not projection.user_id.strip():
        raise ValueError("user_id must not be empty")
    if not projection.source_record_id.strip():
        raise ValueError("source_record_id must not be empty")
    _validate_relative_path(projection.relative_path)
    if projection.root_path:
        _validate_relative_path(projection.root_path)
    if projection.byte_size is not None and projection.byte_size < 0:
        raise ValueError("byte_size must be >= 0")
    if not projection.mime_type.strip():
        raise ValueError("mime_type must not be empty")


def _validate_relative_path(relative_path: str) -> None:
    pure_path = PurePosixPath(relative_path.replace("\\", "/"))
    if pure_path.is_absolute():
        raise ValueError("relative path must not be absolute")
    if any(part in {"", ".", ".."} for part in pure_path.parts):
        raise ValueError("relative path contains forbidden traversal")


def _format_heading_path(heading_stack: list[tuple[int, str]]) -> str | None:
    if not heading_stack:
        return None
    return " > ".join(text for _, text in heading_stack)


def _stable_id(*parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_artifact_files(projections: tuple[MemoryArtifactProjection, ...]) -> str:
    if all(
        projection.content is not None and not projection.content.strip()
        for projection in projections
    ):
        return EMPTY_TEXT_SHA256_HEX
    digest = hashlib.sha256()
    for projection in sorted(projections, key=lambda item: item.relative_path):
        digest.update(projection.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_projection_sha256(projection).encode("utf-8"))
        digest.update(b"\0")
        digest.update(projection.mime_type.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _projection_sha256(projection: MemoryArtifactProjection) -> str:
    if projection.sha256 is not None:
        return projection.sha256
    return _sha256_text(projection.content or "")


def _projection_byte_size(projection: MemoryArtifactProjection) -> int:
    if projection.byte_size is not None:
        return projection.byte_size
    return len((projection.content or "").encode("utf-8"))


# Storage sits below local_runtime.runtime, so it formats with the primitive.
def _utc_now() -> str:
    return format_iso8601_utc_z_milliseconds(datetime.now(UTC))
