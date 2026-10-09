from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_FULL_ACCESS
from pantaray_agents.tools.contract import BrokerPolicyError

from .read_paths import resolve_read_path
from .read_scope import ReadScope


@dataclass(frozen=True, slots=True)
class ReadTarget:
    display_path: str
    real_path: Path
    canonical_root_path: Path
    root_relative_path: str
    action_reference_path: str | None
    allow_symlink_directory_entries: bool = False


def resolve_read_target(*, scope: ReadScope, raw_path: str) -> ReadTarget:
    _reject_non_path_syntax(raw_path)
    resolved = resolve_read_path(
        scope=scope,
        raw_path=raw_path.strip(),
        must_exist=True,
    )
    return ReadTarget(
        display_path=str(resolved.path),
        real_path=resolved.path,
        canonical_root_path=resolved.root.canonical_real_path,
        root_relative_path=resolved.root_relative_path,
        action_reference_path=(
            str(resolved.path) if resolved.root in scope.manifest_roots else None
        ),
        allow_symlink_directory_entries=(
            scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
        ),
    )


def action_reference_paths(target: ReadTarget) -> tuple[str, ...]:
    if target.action_reference_path is None:
        return ()
    return (target.action_reference_path,)


def _reject_non_path_syntax(raw_path: str) -> None:
    stripped = raw_path.strip()
    if not stripped:
        raise BrokerPolicyError("read.path must not be empty")
    if stripped.startswith("~"):
        raise BrokerPolicyError("read.path must not use shell home expansion")


__all__ = [
    "ReadTarget",
    "action_reference_paths",
    "resolve_read_target",
]
