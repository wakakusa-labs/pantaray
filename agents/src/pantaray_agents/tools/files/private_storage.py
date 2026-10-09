"""Pantaray's private app storage, which agent tools must not read or change.

The command sandbox denies these roots to processes; Action read/search/write
paths and command cwds are refused here with the same exceptions. The roots the
app itself places in storage for this Action (its workspace, published tool
results, and agent experience) stay usable with the access their manifest root
grants. Suggestion's file tools see only their run's folder of large results.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.tools.contract import BrokerPolicyError

PRIVATE_APP_STORAGE_MESSAGE = (
    "This path is in Pantaray's private app storage, which tools cannot read or change."
)


@dataclass(frozen=True, slots=True)
class PrivateSearchScope:
    """How a ripgrep search from one base keeps out of private app storage.

    Both are base-relative POSIX paths. A search skips each pruned subtree
    whole and searches each own root on its own, since ripgrep cannot walk into
    a skipped directory for just one of its children.
    """

    pruned: tuple[str, ...]
    own_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrivateAppStorage:
    """Built once per tool call so a directory scan does not re-resolve the roots."""

    storage_roots: tuple[Path, ...]
    readable_roots: tuple[Path, ...]

    def hides(self, path: Path) -> bool:
        """Whether a resolved path lies in storage this Action may not use."""

        return is_within_any(path, self.storage_roots) and not is_within_any(
            path, self.readable_roots
        )

    def prunes(self, path: Path) -> bool:
        """Whether a scan may skip this entry and everything under it unopened.

        A hidden directory that holds one of the Action's own roots is still
        walked, so the root stays reachable from a registered parent folder.
        """

        return self.hides(path) and not any(
            is_within_any(root, (path,)) for root in self.readable_roots
        )

    def search_scope(self, base: Path) -> PrivateSearchScope:
        pruned = _below(base, self.storage_roots)
        return PrivateSearchScope(
            pruned=tuple(pruned.values()),
            # A root that does not exist yet (no published tool results) would
            # fail the whole search as a missing path.
            own_roots=tuple(
                relative
                for root, relative in _below(base, self.readable_roots).items()
                if is_within_any(root, tuple(pruned)) and root.is_dir()
            ),
        )


def private_app_storage_error(*, code: str) -> BrokerPolicyError:
    return BrokerPolicyError(
        PRIVATE_APP_STORAGE_MESSAGE,
        code=code,
        fix_hint=(
            "Read Pantaray's own records (suggestions, actions, insights, "
            "activity logs, ...) with memory_sql instead of opening its files."
        ),
    )


def is_within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    # Directory identity, so a case alias on APFS cannot step around a root.
    return any(
        _relative_to_directory_identity(path=path, directory=root) is not None
        for root in roots
    )


def _relative_to_directory_identity(*, path: Path, directory: Path) -> Path | None:
    try:
        return path.relative_to(directory)
    except ValueError:
        parts: list[str] = []
        ancestor = path
        while ancestor.parent != ancestor:
            if (
                ancestor.name.casefold() == directory.name.casefold()
                and _same_directory(ancestor, directory)
            ):
                return Path(*reversed(parts))
            parts.append(ancestor.name)
            ancestor = ancestor.parent
        return None


def _same_directory(left: Path, right: Path) -> bool:
    return left == right or (left.is_dir() and right.is_dir() and left.samefile(right))


def _below(base: Path, roots: tuple[Path, ...]) -> dict[Path, str]:
    """Roots within base, keyed to their base-relative POSIX path."""

    below: dict[Path, str] = {}
    for root in roots:
        relative = _relative_to_directory_identity(path=root, directory=base)
        if relative is not None:
            below[root] = relative.as_posix()
    return below


__all__ = [
    "PRIVATE_APP_STORAGE_MESSAGE",
    "PrivateAppStorage",
    "PrivateSearchScope",
    "is_within_any",
    "private_app_storage_error",
]
