from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pantaray_agents.tools.contract import BrokerPolicyError

from .manifest_paths import ResolvedManifestPath
from .read_contract import DiscoveryTruncationReason
from .ripgrep import ALWAYS_EXCLUDED_NAME, RIPGREP_TIMEOUT_SECONDS, sandboxed_argv
from .workspace_descriptor_access import WorkspaceScanSkips, scan_workspace_entries

# ripgrep prints no directories, so list asks this script, run under the
# profile glob and grep run under, which entries the same rules show.
LIST_IGNORE_WORKER = Path(__file__).with_name("list_ignore_worker.py")
_REPLY_LENGTH = struct.Struct(">q")
_UNREADABLE = -1


@dataclass(frozen=True, slots=True)
class DiscoveryPath:
    path: Path
    kind: Literal["file", "directory"]


@dataclass(frozen=True, slots=True)
class BoundedDiscoveryPaths:
    selected: list[DiscoveryPath]
    truncation_reason: DiscoveryTruncationReason | None
    skips: WorkspaceScanSkips


def list_discovery_paths(
    *,
    base: ResolvedManifestPath,
    max_depth: int | None,
    limit: int,
    include_path: Callable[[Path], bool],
    exclude_subtree: Callable[[Path], bool],
    sandbox_profile: str,
) -> BoundedDiscoveryPaths:
    shown = _ShownEntries(sandbox_profile=sandbox_profile)
    try:
        result = scan_workspace_entries(
            root_path=base.root.canonical_real_path,
            base_path=base.root_relative_path,
            max_depth=max_depth,
            limit=limit,
            include_path=include_path,
            # A hidden directory is walked only to reach readable roots below
            # it, and the sandbox does not let the worker read it either.
            exclude_subtree=lambda path: (
                exclude_subtree(path)
                or (include_path(path.parent) and shown.ignored(path))
            ),
        )
    finally:
        shown.close()
    return BoundedDiscoveryPaths(
        selected=[
            DiscoveryPath(
                path=base.root.canonical_real_path.joinpath(
                    *entry.root_relative_path.split("/")
                ),
                kind=entry.kind,
            )
            for entry in result.entries
        ],
        truncation_reason=result.truncation_reason,
        skips=result.skips,
    )


def entry_for_discovery_path(path: DiscoveryPath) -> dict[str, object]:
    return {
        "path": str(path.path),
        "kind": path.kind,
        "name": path.path.name,
    }


class _ShownEntries:
    """Which entries glob and grep would show, asked of the sandboxed worker.

    Each directory's entries are asked for once, as the scan reaches it. The
    worker stops after the discovery time budget, and the listing with it.
    """

    def __init__(self, *, sandbox_profile: str) -> None:
        self._process = subprocess.Popen(
            sandboxed_argv(
                (
                    sys.executable,
                    "-I",
                    "-B",
                    str(LIST_IGNORE_WORKER),
                    ALWAYS_EXCLUDED_NAME,
                ),
                sandbox_profile,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._timer = threading.Timer(RIPGREP_TIMEOUT_SECONDS, self._process.kill)
        self._timer.start()
        self._shown: dict[Path, frozenset[str]] = {}

    def ignored(self, path: Path) -> bool:
        directory = path.parent
        names = self._shown.get(directory)
        if names is None:
            names = self._ask(directory)
            self._shown[directory] = names
        return path.name not in names

    def close(self) -> None:
        self._timer.cancel()
        self._process.kill()
        self._process.wait()

    def _ask(self, directory: Path) -> frozenset[str]:
        stdin, stdout = self._process.stdin, self._process.stdout
        assert stdin is not None and stdout is not None
        try:
            stdin.write(os.fsencode(directory) + b"\0")
            stdin.flush()
        except BrokenPipeError as exc:
            raise _worker_stopped() from exc
        header = stdout.read(_REPLY_LENGTH.size)
        if len(header) < _REPLY_LENGTH.size:
            raise _worker_stopped()
        (length,) = _REPLY_LENGTH.unpack(header)
        if length == _UNREADABLE:
            # The scan has the directory open, so it changed under the scan.
            raise BrokerPolicyError(
                f"list could not apply ignore rules to {directory}",
                code="LIST_BACKEND_FAILED",
                fix_hint="List the path again.",
            )
        payload = stdout.read(length)
        if len(payload) < length:
            raise _worker_stopped()
        return frozenset(os.fsdecode(name) for name in payload.split(b"\0") if name)


def _worker_stopped() -> BrokerPolicyError:
    return BrokerPolicyError(
        "list stopped: its ignore-rule check ended, which runs at most "
        f"{RIPGREP_TIMEOUT_SECONDS:g} seconds",
        code="LIST_BACKEND_FAILED",
        fix_hint="List a narrower path or a smaller max_depth.",
    )
