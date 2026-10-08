"""What a read/list/glob/grep call may see, independent of who makes it.

A caller builds one after its own checks (the broker, from an Action's
execution session; ``read_only_tools`` for an agent without a broker); the read
and search code works from this value and the call's arguments alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.schema.read_access import ReadAccessScope
from pantaray_agents.tools.files.manifest_paths import ManifestRoot
from pantaray_agents.tools.files.private_storage import PrivateAppStorage


@dataclass(frozen=True, slots=True)
class ReadScope:
    manifest_roots: tuple[ManifestRoot, ...]
    # None: the caller has no current directory and names absolute paths only.
    cwd_path: Path | None
    read_access_scope: ReadAccessScope
    # The Action's scratch workspace, whose session temp folder stays unlisted.
    scratch_root_path: Path | None
    private_storage: PrivateAppStorage

    def hides(self, path: Path) -> bool:
        """Whether a resolved path is kept out of every read and search result."""

        return self.private_storage.hides(path)


__all__ = ["ReadScope"]
