"""Name the entries of a directory that ignore files leave shown.

Run as a script -- ``python -I -B <this file> <always excluded name>`` -- under
the seatbelt profile glob and grep run under, so the walk below can only read
what they can: rignore opens directories and ignore files by path, and a path
the list scan has already pinned can be swapped for a link elsewhere.

Each request on stdin is a directory's absolute path ended by a NUL byte. Each
reply is an 8-byte big-endian signed length, then that many bytes: the shown
entries' names, separated by NUL bytes; -1, with nothing after it, when the
directory could not be read here. Nothing here imports the package this file sits
in, which would load the whole local runtime.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import rignore

_READ_CHUNK_BYTES = 64 * 1024
_UNREADABLE = -1


def _shown_names(directory: Path, excluded_name: str) -> bytes:
    # The rules of RIPGREP_COMMON_ARGS and RIPGREP_EXCLUSION_ARGS; rignore wraps
    # the ignore library ripgrep is built on, but reads no .rgignore.
    entries = rignore.walk(
        directory,
        ignore_hidden=False,
        read_ignore_files=True,
        read_parents_ignores=True,
        read_git_ignore=True,
        read_global_git_ignore=False,
        read_git_exclude=True,
        require_git=True,
        overrides=[f"!{excluded_name}"],
        max_depth=1,
        follow_links=False,
    )
    return b"\0".join(
        os.fsencode(entry.name) for entry in entries if entry != directory
    )


def main() -> None:
    excluded_name = sys.argv[1]
    requests = sys.stdin.fileno()
    replies = sys.stdout.buffer
    pending = b""
    while chunk := os.read(requests, _READ_CHUNK_BYTES):
        *paths, pending = (pending + chunk).split(b"\0")
        for raw_path in paths:
            try:
                names = _shown_names(Path(os.fsdecode(raw_path)), excluded_name)
            except OSError:
                replies.write(struct.pack(">q", _UNREADABLE))
            else:
                replies.write(struct.pack(">q", len(names)) + names)
            replies.flush()


if __name__ == "__main__":
    main()
