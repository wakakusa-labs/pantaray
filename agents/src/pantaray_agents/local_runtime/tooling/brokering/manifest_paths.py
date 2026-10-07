from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.tools.contract import BrokerPolicyError

from ..repository.common import _configure_connection
from ..workspace_manifest_roots import (
    ManifestRoot,
    load_ready_manifest_root_in_connection,
    load_ready_manifest_roots_in_connection,
    path_belongs_to_manifest_root,
    validate_manifest_root_authority,
)
from ..workspace_root_authority import (
    WorkspaceRootAuthorityError,
)

WORKSPACE_PATH_ESCAPES_ROOT = "WORKSPACE_PATH_ESCAPES_ROOT"
WORKSPACE_PATH_OUTSIDE_ROOTS = "WORKSPACE_PATH_OUTSIDE_ROOTS"
WORKSPACE_PATH_ROOT_NOT_PATCHABLE = "WORKSPACE_PATH_ROOT_NOT_PATCHABLE"
WORKSPACE_PATH_ROOT_NOT_PROCESS_READABLE = "WORKSPACE_PATH_ROOT_NOT_PROCESS_READABLE"
WORKSPACE_PATH_ROOT_NOT_PROCESS_WRITABLE = "WORKSPACE_PATH_ROOT_NOT_PROCESS_WRITABLE"
WORKSPACE_PATH_ROOT_NOT_READABLE = "WORKSPACE_PATH_ROOT_NOT_READABLE"
WORKSPACE_ROOT_AUTHORITY_INVALID = "WORKSPACE_ROOT_AUTHORITY_INVALID"


@dataclass(frozen=True, slots=True)
class ResolvedManifestPath:
    root: ManifestRoot
    path: Path
    root_relative_path: str


@dataclass(frozen=True, slots=True)
class ResolvedProcessCwd:
    root: ManifestRoot
    path: Path
    process_scope_root: Path


def load_manifest_roots(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    manifest_id: str,
) -> tuple[ManifestRoot, ...]:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        roots = load_ready_manifest_roots_in_connection(
            connection=connection,
            user_id=user_id,
            manifest_id=manifest_id,
        )
    if not roots:
        raise BrokerPolicyError("ready workspace manifest is not available")
    for root in roots:
        validate_manifest_root(root)
    return roots


def load_tool_results_root(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    manifest_id: str,
    action_id: str,
) -> Path:
    """Load and validate only the host-owned tool-results root for an Action."""

    expected_root_id = f"root:{action_id}:tool-results"
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        root = load_ready_manifest_root_in_connection(
            connection=connection,
            user_id=user_id,
            manifest_id=manifest_id,
            action_id=action_id,
            root_id=expected_root_id,
        )
    if root is None:
        raise BrokerPolicyError("Action tool-results manifest root is not available")
    validate_manifest_root(root)
    return root.canonical_real_path


def resolve_tool_results_root(
    *,
    roots: tuple[ManifestRoot, ...],
    action_id: str,
) -> Path:
    """Resolve the single host-owned tool-results root for an Action manifest."""

    expected_root_id = f"root:{action_id}:tool-results"
    matches = tuple(root for root in roots if root.root_id == expected_root_id)
    if len(matches) != 1:
        raise BrokerPolicyError("Action tool-results manifest root is not available")
    return matches[0].canonical_real_path


def resolve_local_path(
    *,
    roots: tuple[ManifestRoot, ...],
    raw_path: str,
    cwd_path: Path,
    capability: str,
    must_exist: bool,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> ResolvedManifestPath:
    for root in roots:
        validate_manifest_root(root)
    candidate = _candidate_path(raw_path=raw_path, cwd_path=cwd_path)
    resolved = _resolve_candidate(candidate=candidate, must_exist=must_exist)
    root = _find_root_for_path(roots=roots, path=resolved, capability=capability)
    _ensure_inside_root(candidate=resolved, root=root.canonical_real_path)
    if must_be_file and not resolved.is_file():
        raise BrokerPolicyError("path must reference an existing file")
    if must_be_dir and not resolved.is_dir():
        raise BrokerPolicyError("path must reference an existing directory")
    return ResolvedManifestPath(
        root=root,
        path=resolved,
        root_relative_path=_root_relative_path(path=resolved, root=root),
    )


def resolve_process_cwd(
    *,
    roots: tuple[ManifestRoot, ...],
    raw_cwd: str | None,
    default_cwd: Path,
) -> ResolvedProcessCwd:
    resolved = resolve_local_path(
        roots=roots,
        raw_path=raw_cwd or ".",
        cwd_path=default_cwd,
        capability="process_write",
        must_exist=True,
        must_be_dir=True,
    )
    return ResolvedProcessCwd(
        root=resolved.root,
        path=resolved.path,
        process_scope_root=_derive_process_scope_root(
            cwd=resolved.path,
            root_path=resolved.root.canonical_real_path,
        ),
    )


def _candidate_path(*, raw_path: str, cwd_path: Path) -> Path:
    stripped = raw_path.strip()
    if not stripped:
        raise BrokerPolicyError("path must not be empty")
    if stripped.startswith("~"):
        raise BrokerPolicyError("path must not use shell home expansion")
    path = Path(stripped)
    return path if path.is_absolute() else cwd_path / path


def validate_manifest_root(root: ManifestRoot) -> None:
    try:
        validate_manifest_root_authority(root)
    except WorkspaceRootAuthorityError as exc:
        raise BrokerPolicyError(
            str(exc),
            code=WORKSPACE_ROOT_AUTHORITY_INVALID,
        ) from exc


def _resolve_candidate(*, candidate: Path, must_exist: bool) -> Path:
    if must_exist:
        return candidate.resolve(strict=True)
    return candidate.resolve(strict=False)


def _find_root_for_path(
    *,
    roots: tuple[ManifestRoot, ...],
    path: Path,
    capability: str,
) -> ManifestRoot:
    for root in roots:
        if not path_belongs_to_manifest_root(path=path, root=root):
            continue
        if capability == "read" and not root.can_read:
            raise BrokerPolicyError(
                "path root is not readable",
                code=WORKSPACE_PATH_ROOT_NOT_READABLE,
            )
        if capability == "apply_patch" and not root.can_apply_patch:
            raise BrokerPolicyError(
                "path root cannot be patched",
                code=WORKSPACE_PATH_ROOT_NOT_PATCHABLE,
            )
        if capability == "process_read" and not root.can_process_read:
            raise BrokerPolicyError(
                "path root is not process-readable",
                code=WORKSPACE_PATH_ROOT_NOT_PROCESS_READABLE,
            )
        if capability == "process_write" and not root.can_process_write:
            raise BrokerPolicyError(
                "path root is not process-writable",
                code=WORKSPACE_PATH_ROOT_NOT_PROCESS_WRITABLE,
            )
        return root
    raise BrokerPolicyError(
        "path is outside the action workspace roots",
        code=WORKSPACE_PATH_OUTSIDE_ROOTS,
    )


def _path_belongs_to_root(*, path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _root_relative_path(*, path: Path, root: ManifestRoot) -> str:
    relative = path.relative_to(root.canonical_real_path).as_posix()
    return relative or "."


def _derive_process_scope_root(*, cwd: Path, root_path: Path) -> Path:
    current = cwd.resolve(strict=True)
    resolved_root = root_path
    while True:
        if (current / ".git").exists():
            return current
        if current == resolved_root:
            return resolved_root
        try:
            current.relative_to(resolved_root)
        except ValueError as exc:
            raise BrokerPolicyError("process cwd escapes the workspace root") from exc
        current = current.parent


def _ensure_inside_root(*, candidate: Path, root: Path) -> None:
    if not _path_belongs_to_root(path=candidate, root=root):
        raise BrokerPolicyError(
            "resolved path escapes the workspace root",
            code=WORKSPACE_PATH_ESCAPES_ROOT,
        )


__all__ = [
    "ManifestRoot",
    "ResolvedManifestPath",
    "ResolvedProcessCwd",
    "load_manifest_roots",
    "load_tool_results_root",
    "resolve_local_path",
    "resolve_process_cwd",
    "resolve_tool_results_root",
    "validate_manifest_root",
]
