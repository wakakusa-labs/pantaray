"""The Pantaray-wide AGENTS.md, which every later Action obeys.

A write to it persists instructions for all future work, so it is written only
by a call the per-call approval review covers, whatever folders the workspace
or a conversation grant opens. ``apply_patch`` treats it as outside the
workspace; no folder that holds it is opened for commands or for a whole
conversation; sandboxed commands may not write it at all, and a command outside
the sandbox is reviewed anyway. Reading it stays ordinary.

It is protected by the name Actions load it from and by the real path that name
leads to, compared after APFS's case and Unicode folding.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .secret_paths import apfs_folded, ere_literal

AGENTS_MD_FILENAME = "AGENTS.md"


def pantaray_agents_md_path() -> Path:
    """Where Actions load the Pantaray-wide instructions from."""

    return Path.home() / ".pantaray" / AGENTS_MD_FILENAME


def holds_pantaray_agents_md(path: Path) -> bool:
    """Whether writing at ``path`` can change the Pantaray-wide AGENTS.md: it is
    the file, the file a link there leads to, or a folder above either."""

    asked = {
        apfs_folded(candidate.as_posix()).rstrip("/")
        for candidate in (path, path.resolve(strict=False))
    }
    return any(
        protected == candidate or protected.startswith(f"{candidate}/")
        for protected in map(apfs_folded, _protected_paths())
        for candidate in asked
    )


def pantaray_agents_md_write_patterns() -> tuple[str, ...]:
    """Seatbelt EREs for the paths a sandboxed command may not write: the files,
    and each folder above them, so renaming a folder cannot carry them out of
    the deny. A folder pattern matches only the folder itself; its other
    children stay writable."""

    exact: dict[str, None] = {}
    for protected in _protected_paths():
        path = PurePosixPath(protected)
        for name in (path, *path.parents):
            # Nothing can rename "/".
            if str(name) != "/":
                exact.setdefault(f"^{ere_literal(str(name))}$")
    return tuple(exact)


def _protected_paths() -> tuple[str, ...]:
    path = pantaray_agents_md_path()
    return tuple(
        dict.fromkeys(
            candidate.as_posix() for candidate in (path, path.resolve(strict=False))
        )
    )


__all__ = [
    "AGENTS_MD_FILENAME",
    "holds_pantaray_agents_md",
    "pantaray_agents_md_path",
    "pantaray_agents_md_write_patterns",
]
