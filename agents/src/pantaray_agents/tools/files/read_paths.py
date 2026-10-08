"""Where a read/list/glob/grep path leads, and whether the call may see it.

Every check here works from a ``ReadScope`` alone: the registered folders, the
read access setting, the cwd and the app's own storage that stays hidden.
"""

from __future__ import annotations

from difflib import get_close_matches
from itertools import islice
from pathlib import Path

from pantaray_agents.local_runtime.tooling.workspace_manifest_roots import (
    path_belongs_to_manifest_root,
)
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.manifest_paths import (
    WORKSPACE_PATH_ESCAPES_ROOT,
    WORKSPACE_PATH_OUTSIDE_ROOTS,
    WORKSPACE_PATH_ROOT_NOT_READABLE,
    ManifestRoot,
    ResolvedManifestPath,
    candidate_path,
    resolve_local_path,
)
from pantaray_agents.tools.files.private_storage import private_app_storage_error

from .read_scope import ReadScope

READ_PATH_NOT_FOUND = "READ_PATH_NOT_FOUND"
READ_PATH_DENIED = "READ_PATH_DENIED"
READ_SCOPE_DENIED = "READ_SCOPE_DENIED"
SUGGESTION_LIMIT = 3
SUGGESTION_SCAN_LIMIT = 200

_READ_WORKSPACE_SCOPE_ERROR_CODES = frozenset(
    {
        WORKSPACE_PATH_OUTSIDE_ROOTS,
        WORKSPACE_PATH_ROOT_NOT_READABLE,
    }
)


def resolve_read_path(
    *,
    scope: ReadScope,
    raw_path: str,
    must_exist: bool,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> ResolvedManifestPath:
    try:
        resolved = _resolve_read_tool_path_unchecked(
            scope=scope,
            raw_path=raw_path,
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    except FileNotFoundError as exc:
        suggestions = _resolve_missing_path_suggestions(
            scope=scope,
            raw_path=raw_path,
        )
        raise _missing_path_error(
            raw_path=raw_path.strip(),
            suggestions=suggestions,
            full_access=scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS,
            has_cwd=scope.cwd_path is not None,
        ) from exc
    if scope.private_storage.hides(resolved.path):
        raise private_app_storage_error(code=READ_PATH_DENIED)
    return resolved


def _resolve_read_tool_path_unchecked(
    *,
    scope: ReadScope,
    raw_path: str,
    must_exist: bool,
    must_be_file: bool,
    must_be_dir: bool,
) -> ResolvedManifestPath:
    if scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return _resolve_full_access_path(
            raw_path=raw_path,
            cwd_path=scope.cwd_path,
            manifest_roots=scope.manifest_roots,
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    if scope.read_access_scope != READ_ACCESS_SCOPE_WORKSPACE:
        raise BrokerPolicyError(
            f"Unsupported read_access_scope: {scope.read_access_scope}",
            code=READ_PATH_DENIED,
        )
    try:
        return resolve_local_path(
            roots=scope.manifest_roots,
            raw_path=raw_path,
            cwd_path=scope.cwd_path,
            capability="read",
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    except BrokerPolicyError as exc:
        if exc.code == WORKSPACE_PATH_ESCAPES_ROOT or (
            exc.code == WORKSPACE_PATH_OUTSIDE_ROOTS
            and _raw_candidate_starts_inside_readable_workspace_root(
                scope=scope,
                raw_path=raw_path,
            )
        ):
            raise BrokerPolicyError(
                "resolved path escapes the workspace root",
                code=READ_PATH_DENIED,
                fix_hint=(
                    "Retry with a concrete path under workspace roots that does not "
                    "resolve through a symlink outside the workspace."
                ),
            ) from exc
        if exc.code not in _READ_WORKSPACE_SCOPE_ERROR_CODES:
            raise
        raise _scope_denied_error() from exc


def _resolve_full_access_path(
    *,
    raw_path: str,
    cwd_path: Path | None,
    manifest_roots: tuple[ManifestRoot, ...],
    must_exist: bool,
    must_be_file: bool,
    must_be_dir: bool,
) -> ResolvedManifestPath:
    candidate = candidate_path(raw_path=raw_path, cwd_path=cwd_path)
    resolved = candidate.resolve(strict=must_exist)
    if must_be_file and not resolved.is_file():
        raise BrokerPolicyError("path must reference an existing file")
    if must_be_dir and not resolved.is_dir():
        raise BrokerPolicyError("path must reference an existing directory")
    for root in manifest_roots:
        if root.can_read and path_belongs_to_manifest_root(path=resolved, root=root):
            return ResolvedManifestPath(
                root=root,
                path=resolved,
                root_relative_path=_root_relative_path(
                    path=resolved, root_path=root.canonical_real_path
                ),
            )
    # Outside every registered folder no directory is trusted, so the reader
    # walks the whole resolved path down from "/" without following a link: a
    # directory on the way swapped for one after this check cannot redirect it.
    root_path = Path(resolved.anchor)
    root = ManifestRoot(
        root_id=f"full_access:{root_path}",
        manifest_id="full_access",
        source_type="local_filesystem",
        display_name=str(root_path),
        canonical_real_path=root_path,
        real_path=root_path,
        can_read=True,
        can_apply_patch=False,
        can_process_read=False,
        can_process_write=False,
    )
    return ResolvedManifestPath(
        root=root,
        path=resolved,
        root_relative_path=_root_relative_path(path=resolved, root_path=root_path),
    )


def _resolve_missing_path_suggestions(
    *,
    scope: ReadScope,
    raw_path: str,
) -> tuple[str, ...]:
    candidate = candidate_path(raw_path=raw_path, cwd_path=scope.cwd_path)
    _ensure_missing_path_is_in_read_scope(scope=scope, raw_path=str(candidate))
    parent = candidate.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except FileNotFoundError:
        return ()
    if not resolved_parent.is_dir():
        return ()
    _ensure_missing_path_is_in_read_scope(scope=scope, raw_path=str(resolved_parent))
    if scope.private_storage.hides(resolved_parent):
        raise private_app_storage_error(code=READ_PATH_DENIED)
    visible_suggestions: list[str] = []
    for suggestion in _suggest_local_paths(parent=resolved_parent, raw_path=raw_path):
        try:
            resolved_suggestion = Path(suggestion).resolve(strict=False)
        except RuntimeError:
            continue
        if not scope.hides(resolved_suggestion):
            visible_suggestions.append(suggestion)
    return tuple(visible_suggestions)


def _ensure_missing_path_is_in_read_scope(
    *,
    scope: ReadScope,
    raw_path: str,
) -> None:
    if scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return
    try:
        _resolve_read_tool_path_unchecked(
            scope=scope,
            raw_path=raw_path,
            must_exist=False,
            must_be_file=False,
            must_be_dir=False,
        )
    except BrokerPolicyError as exc:
        if exc.code == READ_SCOPE_DENIED:
            raise
        if exc.code in _READ_WORKSPACE_SCOPE_ERROR_CODES:
            raise _scope_denied_error() from exc
        raise


def _raw_candidate_starts_inside_readable_workspace_root(
    *,
    scope: ReadScope,
    raw_path: str,
) -> bool:
    candidate = candidate_path(raw_path=raw_path, cwd_path=scope.cwd_path)
    for root in scope.manifest_roots:
        if not root.can_read:
            continue
        try:
            candidate.relative_to(root.canonical_real_path)
        except ValueError:
            continue
        return True
    return False


def _root_relative_path(*, path: Path, root_path: Path) -> str:
    relative = path.relative_to(root_path).as_posix()
    return relative or "."


def _suggest_local_paths(*, parent: Path, raw_path: str) -> tuple[str, ...]:
    candidate = Path(raw_path.strip())
    try:
        entries = _bounded_child_names(parent)
    except OSError:
        return ()
    selected = _select_similar_names(base_name=candidate.name, entries=entries)
    return tuple(str(parent / name) for name in selected)


def _bounded_child_names(parent: Path) -> list[str]:
    return [child.name for child in islice(parent.iterdir(), SUGGESTION_SCAN_LIMIT)]


def _select_similar_names(*, base_name: str, entries: list[str]) -> tuple[str, ...]:
    case_matches = [
        name
        for name in entries
        if name.lower().startswith(base_name.lower()) and name != base_name
    ]
    fuzzy_matches = get_close_matches(
        base_name, entries, n=SUGGESTION_LIMIT, cutoff=0.55
    )
    ordered = [*case_matches, *fuzzy_matches]
    deduped: list[str] = []
    for name in ordered:
        if name not in deduped:
            deduped.append(name)
        if len(deduped) >= SUGGESTION_LIMIT:
            break
    return tuple(deduped)


def _missing_path_error(
    *,
    raw_path: str,
    suggestions: tuple[str, ...],
    full_access: bool,
    has_cwd: bool,
) -> BrokerPolicyError:
    details = f"READ_PATH_NOT_FOUND: path does not exist: {raw_path}"
    if suggestions:
        details += "\n\nDid you mean one of these?\n" + "\n".join(suggestions)
    return BrokerPolicyError(
        details,
        code=READ_PATH_NOT_FOUND,
        fix_hint=(
            (
                "Retry with an existing local path. Absolute paths and paths "
                "relative to the current cwd are valid for read/search."
                if has_cwd
                else "Retry with an existing absolute local path."
            )
            if full_access
            else "Retry with an existing path under workspace roots."
        ),
    )


def _scope_denied_error() -> BrokerPolicyError:
    return BrokerPolicyError(
        "Read/search path is outside workspace roots.",
        code=READ_SCOPE_DENIED,
        fix_hint=(
            "Current read_access_scope is workspace. Retry with a path under "
            "workspace roots, or ask the user to enable full_access for read/search."
        ),
        examples=(".", "/Users/example/project", "src"),
    )


__all__ = [
    "READ_PATH_DENIED",
    "READ_PATH_NOT_FOUND",
    "READ_SCOPE_DENIED",
    "SUGGESTION_SCAN_LIMIT",
    "resolve_read_path",
]
