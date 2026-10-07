"""What a read/list/glob/grep call may see, independent of who makes it.

The broker builds one from an Action's execution session after its own checks;
the read and search code works from this value and the call's arguments alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.schema.read_access import ReadAccessScope

from .manifest_paths import ManifestRoot
from .private_app_storage import PrivateAppStorage


@dataclass(frozen=True, slots=True)
class ReadScope:
    manifest_roots: tuple[ManifestRoot, ...]
    cwd_path: Path
    read_access_scope: ReadAccessScope
    scratch_root_path: Path
    private_storage: PrivateAppStorage

    def hides(self, path: Path) -> bool:
        """Whether a resolved path is kept out of every read and search result."""

        return self.private_storage.hides(path)


__all__ = ["ReadScope"]
