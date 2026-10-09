from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256

from pantaray_agents.local_runtime.tooling.fs_sandbox import PatchChunk
from pantaray_agents.local_runtime.tooling.read_windows import (
    READ_WINDOW_MAX_BYTES,
    ReadWindow,
    ReadWindowCandidate,
    ReadWindowTooLargeError,
    build_bounded_read_windows,
)
from pantaray_agents.schema.agent.base import JSONValue

FULL_READ_MAX_LINES = 800
WINDOW_MARGIN_LINES = 30


@dataclass(frozen=True, slots=True)
class MemoryReadSnapshot:
    path: str
    file_sha256: str
    full_file: bool
    visible_texts: tuple[str, ...]
    step_number: int


def build_read_snapshot(
    *,
    path: str,
    text: str,
    visible_texts: tuple[str, ...],
    full_file: bool,
    step_number: int,
) -> MemoryReadSnapshot:
    return MemoryReadSnapshot(
        path=path,
        file_sha256=text_sha256(text),
        full_file=full_file,
        visible_texts=visible_texts,
        step_number=step_number,
    )


def merge_read_snapshot(
    *,
    existing: MemoryReadSnapshot | None,
    path: str,
    text: str,
    visible_texts: tuple[str, ...],
    full_file: bool,
    step_number: int,
) -> MemoryReadSnapshot:
    file_sha256 = text_sha256(text)
    if existing is not None and existing.file_sha256 == file_sha256:
        visible_texts = tuple(dict.fromkeys((*existing.visible_texts, *visible_texts)))
        full_file = full_file or existing.full_file
    return MemoryReadSnapshot(
        path=path,
        file_sha256=file_sha256,
        full_file=full_file,
        visible_texts=visible_texts,
        step_number=step_number,
    )


def text_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def apply_patch_value_error_details(exc: ValueError) -> tuple[str, str]:
    if isinstance(exc, ReadWindowTooLargeError):
        return (
            "READ_WINDOW_TOO_LARGE",
            "Split the oversized target line before retrying.",
        )
    return "INVALID_PATCH", "Fix the patch structure and retry."


def patch_uses_visible_lines(
    *, chunks: tuple[PatchChunk, ...], snapshot: MemoryReadSnapshot
) -> bool:
    if snapshot.full_file:
        return True
    for chunk in chunks:
        pattern = _chunk_context_remove_pattern(chunk)
        if not pattern:
            return False
        if not _pattern_visible_in_any_text(
            pattern=pattern,
            visible_texts=snapshot.visible_texts,
        ):
            return False
    return True


def read_snapshot_is_valid(
    *,
    snapshot: MemoryReadSnapshot | None,
    text: str,
    chunks: tuple[PatchChunk, ...],
) -> bool:
    return (
        snapshot is not None
        and snapshot.file_sha256 == text_sha256(text)
        and patch_uses_visible_lines(chunks=chunks, snapshot=snapshot)
    )


def build_needs_read_output(
    *,
    path: str,
    text: str,
    chunks: tuple[PatchChunk, ...],
    snapshot: MemoryReadSnapshot | None = None,
) -> tuple[dict[str, JSONValue], tuple[str, ...]]:
    if _fits_full_read(text):
        output: dict[str, JSONValue] = {
            "status": "needs_read",
            "path": path,
            "patch_applied": False,
            "read_scope": "full_file",
            "file_truncated": False,
            "text": text,
            "windows": [],
            "remaining_chunk_count": 0,
            "retry_advice": (
                "Patch was not applied because the target file was not read first. "
                "Rebuild apply_patch context/remove lines only from this text and retry."
            ),
        }
        return output, (text,)

    existing_visible = _current_visible_texts(snapshot=snapshot, text=text)
    missing_chunks = tuple(
        chunk
        for chunk in chunks
        if not _chunk_visible(chunk=chunk, visible_texts=existing_visible)
    )
    windows = _target_windows(text=text, chunks=missing_chunks)
    visible_texts = tuple(
        dict.fromkeys((*existing_visible, *(window.text for window in windows)))
    )
    remaining_chunk_count = sum(
        not _chunk_visible(chunk=chunk, visible_texts=visible_texts) for chunk in chunks
    )
    output = {
        "status": "needs_read",
        "path": path,
        "patch_applied": False,
        "read_scope": "target_windows",
        "file_truncated": True,
        "text": "",
        "windows": [
            {
                "start_line": window.start_line,
                "end_line": window.end_line,
                "match_reason": window.match_reason,
                "text": window.text,
            }
            for window in windows
        ],
        "remaining_chunk_count": remaining_chunk_count,
        "retry_advice": _retry_advice(remaining_chunk_count),
    }
    return output, tuple(window.text for window in windows)


def _fits_full_read(text: str) -> bool:
    return (
        len(text.encode("utf-8")) <= READ_WINDOW_MAX_BYTES
        and len(_line_texts(text)) <= FULL_READ_MAX_LINES
    )


def _target_windows(
    *, text: str, chunks: tuple[PatchChunk, ...]
) -> tuple[ReadWindow, ...]:
    line_texts = _line_texts(text)
    if not line_texts:
        return (ReadWindow(1, 1, "empty_file", ""),)
    candidates: list[ReadWindowCandidate] = []
    for chunk in chunks:
        pattern = _chunk_context_remove_pattern(chunk)
        if not pattern:
            candidates.append(ReadWindowCandidate(0, 0, "no_context_start"))
            continue
        exact_index = _find_exact_pattern(line_texts=line_texts, pattern=pattern)
        if exact_index is not None:
            candidates.append(
                ReadWindowCandidate(
                    exact_index,
                    exact_index + len(pattern) - 1,
                    "exact_context_match",
                )
            )
            continue
        fuzzy_index = _find_fuzzy_line(line_texts=line_texts, expected=pattern[0])
        candidates.append(
            ReadWindowCandidate(
                fuzzy_index,
                fuzzy_index,
                "nearest_context_match",
            )
        )

    if not candidates:
        candidates.append(ReadWindowCandidate(0, 0, "no_context_start"))
    return build_bounded_read_windows(
        segments=_line_segments(text),
        candidates=tuple(candidates),
        margin_lines=WINDOW_MARGIN_LINES,
    )


def _chunk_context_remove_pattern(chunk: PatchChunk) -> tuple[str, ...]:
    return tuple(line.text for line in chunk.lines if line.op in ("context", "remove"))


def _pattern_visible_in_any_text(
    *,
    pattern: tuple[str, ...],
    visible_texts: tuple[str, ...],
) -> bool:
    return any(
        _find_exact_pattern(line_texts=_line_texts(visible_text), pattern=pattern)
        is not None
        for visible_text in visible_texts
    )


def _chunk_visible(*, chunk: PatchChunk, visible_texts: tuple[str, ...]) -> bool:
    pattern = _chunk_context_remove_pattern(chunk)
    return bool(pattern) and _pattern_visible_in_any_text(
        pattern=pattern,
        visible_texts=visible_texts,
    )


def _current_visible_texts(
    *, snapshot: MemoryReadSnapshot | None, text: str
) -> tuple[str, ...]:
    if snapshot is None or snapshot.file_sha256 != text_sha256(text):
        return ()
    return snapshot.visible_texts


def _retry_advice(remaining_chunk_count: int) -> str:
    if remaining_chunk_count:
        return (
            "Patch was not applied. Retry the same patch to read the remaining "
            "target windows before applying it."
        )
    return (
        "Patch was not applied. All target contexts are now visible; retry the "
        "same patch to apply it."
    )


def _find_exact_pattern(
    *, line_texts: tuple[str, ...], pattern: tuple[str, ...]
) -> int | None:
    if not pattern:
        return 0
    for index in range(0, len(line_texts) - len(pattern) + 1):
        if line_texts[index : index + len(pattern)] == pattern:
            return index
    return None


def _find_fuzzy_line(*, line_texts: tuple[str, ...], expected: str) -> int:
    best_index = 0
    best_ratio = -1.0
    for index, line in enumerate(line_texts):
        ratio = SequenceMatcher(None, expected, line).ratio()
        if ratio > best_ratio:
            best_index = index
            best_ratio = ratio
    return best_index


def _line_texts(text: str) -> tuple[str, ...]:
    return tuple(
        segment.removesuffix("\n").removesuffix("\r")
        for segment in _line_segments(text)
    )


def _line_segments(text: str) -> tuple[str, ...]:
    if text == "":
        return ()
    return tuple(text.splitlines(keepends=True))
