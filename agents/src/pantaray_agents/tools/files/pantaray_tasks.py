"""The folder of Pantaray tasks: one file each, which Pantaray runs on its own.

Only the user and the task tool write it. A task fires only once the user
approved its current content, but a write there could still delete or replace
the user's task, so no generic file tool writes inside it, no folder that holds
it is opened for writing, and sandboxed commands may not write it or the
folders above it. Reading it stays ordinary.

It is protected by its name and by the real path that name leads to, compared
after APFS's case and Unicode folding.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pantaray_agents.tools.contract import BrokerPolicyError

from .secret_paths import apfs_folded, ere_literal

PANTARAY_TASKS_WRITE_DENIED = "PANTARAY_TASKS_WRITE_DENIED"


def pantaray_tasks_dir() -> Path:
    return Path.home() / ".pantaray" / "tasks"


def holds_pantaray_tasks(path: Path) -> bool:
    """Whether writing at ``path`` can change a task: it is the tasks folder,
    lies inside it, or is a folder above it, by its name or a link's target."""

    folder = pantaray_tasks_dir()
    return any(
        candidate == protected
        or candidate.startswith(f"{protected}/")
        or protected.startswith(f"{candidate}/")
        for protected in {
            apfs_folded(root.as_posix()).rstrip("/")
            for root in (folder, folder.resolve(strict=False))
        }
        for candidate in {
            apfs_folded(name.as_posix()).rstrip("/")
            for name in (path, path.resolve(strict=False))
        }
    )


def refuse_pantaray_tasks_writes(*paths: Path) -> None:
    """Raise a file tool's refusal when any of these paths can change a task."""

    for path in paths:
        if holds_pantaray_tasks(path):
            raise BrokerPolicyError(
                f"{PANTARAY_TASKS_WRITE_DENIED}: {path} holds Pantaray's tasks, "
                "which only the user changes",
                code=PANTARAY_TASKS_WRITE_DENIED,
                fix_hint=(
                    "Do not write in ~/.pantaray/tasks with any tool or command. "
                    "Tell the user what to change there instead."
                ),
            )


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


__all__ = [
    "PANTARAY_TASKS_WRITE_DENIED",
    "holds_pantaray_tasks",
    "pantaray_tasks_dir",
    "pantaray_tasks_write_patterns",
    "refuse_pantaray_tasks_writes",
]
