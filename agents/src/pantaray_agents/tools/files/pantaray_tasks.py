"""The folder of Pantaray tasks: one file each, which Pantaray runs on its own.

A task fires only once the user approved its current content, so a write
there cannot start work by itself. Sandboxed commands still may not write the
folder, its files or the folders above it, so that only the user and the
task tool change what is waiting for approval. Reading it stays ordinary.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .secret_paths import ere_literal


def pantaray_tasks_dir() -> Path:
    return Path.home() / ".pantaray" / "tasks"


def pantaray_tasks_write_patterns() -> tuple[str, ...]:
    """Seatbelt EREs for the paths a sandboxed command may not write: the
    folder and everything in it, and each folder above it, so renaming a
    folder cannot carry the tasks out of the deny."""

    patterns: dict[str, None] = {}
    folder = pantaray_tasks_dir()
    for root in dict.fromkeys(
        candidate.as_posix() for candidate in (folder, folder.resolve(strict=False))
    ):
        patterns.setdefault(f"^{ere_literal(root)}(/.*)?$")
        for parent in PurePosixPath(root).parents:
            # Nothing can rename "/".
            if str(parent) != "/":
                patterns.setdefault(f"^{ere_literal(str(parent))}$")
    return tuple(patterns)


__all__ = ["pantaray_tasks_dir", "pantaray_tasks_write_patterns"]
