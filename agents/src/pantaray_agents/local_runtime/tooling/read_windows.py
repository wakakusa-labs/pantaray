from __future__ import annotations

from dataclasses import dataclass

READ_WINDOW_MAX_BYTES = 40_000


@dataclass(frozen=True, slots=True)
class ReadWindowCandidate:
    start_index: int
    end_index: int
    match_reason: str


@dataclass(frozen=True, slots=True)
class ReadWindow:
    start_line: int
    end_line: int
    match_reason: str
    text: str


class ReadWindowTooLargeError(ValueError):
    def __init__(self, *, required_bytes: int, max_bytes: int) -> None:
        super().__init__(
            "required patch context is too large for a bounded read window: "
            f"{required_bytes} bytes exceeds {max_bytes} bytes"
        )
        self.required_bytes = required_bytes
        self.max_bytes = max_bytes


def build_bounded_read_windows(
    *,
    segments: tuple[str, ...],
    candidates: tuple[ReadWindowCandidate, ...],
    margin_lines: int,
    max_total_bytes: int = READ_WINDOW_MAX_BYTES,
) -> tuple[ReadWindow, ...]:
    # Callers split lines (with endings kept) the same way their candidate
    # indices and visibility checks do, so window line numbers agree.
    if not segments:
        return (ReadWindow(1, 1, "empty_file", ""),)

    windows: list[ReadWindow] = []
    selected_ranges: set[tuple[int, int]] = set()
    total_bytes = 0
    for candidate in candidates:
        core_start = max(0, min(candidate.start_index, len(segments) - 1))
        core_end = max(core_start, min(candidate.end_index, len(segments) - 1))
        required_bytes = _range_size(segments, core_start, core_end)
        if required_bytes > max_total_bytes:
            raise ReadWindowTooLargeError(
                required_bytes=required_bytes,
                max_bytes=max_total_bytes,
            )

        start = max(0, core_start - margin_lines)
        end = min(len(segments) - 1, core_end + margin_lines)
        if (start, end) in selected_ranges:
            continue
        window_bytes = _range_size(segments, start, end)
        if total_bytes + window_bytes > max_total_bytes:
            if windows:
                break
            start, end = _fit_first_window(
                segments=segments,
                core_start=core_start,
                core_end=core_end,
                requested_start=start,
                requested_end=end,
                max_bytes=max_total_bytes,
            )
            window_bytes = _range_size(segments, start, end)

        selected_ranges.add((start, end))
        windows.append(
            ReadWindow(
                start_line=start + 1,
                end_line=end + 1,
                match_reason=candidate.match_reason,
                text="".join(segments[start : end + 1]),
            )
        )
        total_bytes += window_bytes
    return tuple(windows)


def _fit_first_window(
    *,
    segments: tuple[str, ...],
    core_start: int,
    core_end: int,
    requested_start: int,
    requested_end: int,
    max_bytes: int,
) -> tuple[int, int]:
    start = core_start
    end = core_end
    used_bytes = _range_size(segments, start, end)
    while start > requested_start or end < requested_end:
        changed = False
        if start > requested_start:
            line_bytes = len(segments[start - 1].encode("utf-8"))
            if used_bytes + line_bytes <= max_bytes:
                start -= 1
                used_bytes += line_bytes
                changed = True
        if end < requested_end:
            line_bytes = len(segments[end + 1].encode("utf-8"))
            if used_bytes + line_bytes <= max_bytes:
                end += 1
                used_bytes += line_bytes
                changed = True
        if not changed:
            break
    return start, end


def _range_size(segments: tuple[str, ...], start: int, end: int) -> int:
    return sum(len(segment.encode("utf-8")) for segment in segments[start : end + 1])
