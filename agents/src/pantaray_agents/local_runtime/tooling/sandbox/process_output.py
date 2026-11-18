"""The unread output of one stream of a long-running process.

The stream is always drained, and what has not been read yet is kept up to a
cap: its first half and its last half, with the bytes in between counted and
dropped. The process itself is never ended for writing too much.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StreamOutput:
    """Output that arrived since the last read; ``text`` marks any dropped middle."""

    text: str
    dropped_bytes: int


def _split_at_byte(text: str, size: int) -> tuple[str, str]:
    """Split before the character holding byte ``size`` of ``text``'s UTF-8 form."""

    head = text.encode("utf-8")[:size].decode("utf-8", errors="ignore")
    return head, text[len(head) :]


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(slots=True)
class RetainedStream:
    cap_bytes: int
    # Every byte the stream has carried, read or dropped.
    total_bytes: int = 0
    _head: list[str] = field(default_factory=list)
    _head_bytes: int = 0
    _tail: deque[str] = field(default_factory=deque)
    _tail_bytes: int = 0
    _dropped_bytes: int = 0

    def append(self, text: str) -> None:
        size = _byte_size(text)
        self.total_bytes += size
        head_room = self.cap_bytes // 2 - self._head_bytes
        if not self._tail and head_room > 0:
            head, text = _split_at_byte(text, head_room)
            self._head.append(head)
            self._head_bytes += _byte_size(head)
            size -= _byte_size(head)
        if not text:
            return
        self._tail.append(text)
        self._tail_bytes += size
        excess = self._tail_bytes - (self.cap_bytes - self.cap_bytes // 2)
        while excess > 0:
            oldest = self._tail.popleft()
            oldest_bytes = _byte_size(oldest)
            if oldest_bytes > excess:
                # Cut at the first whole character past the excess.
                kept = oldest.encode("utf-8")[excess:].decode("utf-8", errors="ignore")
                self._tail.appendleft(kept)
                removed = oldest_bytes - _byte_size(kept)
            else:
                removed = oldest_bytes
            self._tail_bytes -= removed
            self._dropped_bytes += removed
            excess -= removed

    def take(self) -> StreamOutput:
        """Everything unread, which then counts as read."""

        gap = (
            f"\n[... {self._dropped_bytes} bytes of output dropped ...]\n"
            if self._dropped_bytes
            else ""
        )
        output = StreamOutput(
            text="".join(self._head) + gap + "".join(self._tail),
            dropped_bytes=self._dropped_bytes,
        )
        self._head.clear()
        self._tail.clear()
        self._head_bytes = self._tail_bytes = self._dropped_bytes = 0
        return output
