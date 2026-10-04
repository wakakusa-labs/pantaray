from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches
from itertools import islice
from pathlib import Path

from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
)

from ..action_plan_document import is_action_plan_artifact_path
from ..workspace_manifest_roots import path_belongs_to_manifest_root
from .broker_common import BrokerContext, BrokerPolicyError
from .manifest_paths import (
    WORKSPACE_PATH_ESCAPES_ROOT,
    WORKSPACE_PATH_OUTSIDE_ROOTS,
    WORKSPACE_PATH_ROOT_NOT_READABLE,
    ManifestRoot,
    ResolvedManifestPath,
    ResolvedProcessCwd,
    resolve_local_path,
    resolve_process_cwd,
    validate_manifest_root,
)
from .outside_workspace import (
    OutsideWorkspaceCwd,
    OutsideWorkspacePatchTarget,
    resolve_outside_workspace_cwd,
    resolve_outside_workspace_patch_target,
)
from .private_app_storage import private_app_storage_error, private_app_storage_filter

READ_PATH_NOT_FOUND = "READ_PATH_NOT_FOUND"
READ_PATH_DENIED = "READ_PATH_DENIED"
READ_SCOPE_DENIED = "READ_SCOPE_DENIED"
WRITE_PATH_NOT_FOUND = "WRITE_PATH_NOT_FOUND"
WRITE_PATH_DENIED = "WRITE_PATH_DENIED"
EXEC_CWD_NOT_FOUND = "EXEC_CWD_NOT_FOUND"
EXEC_CWD_DENIED = "EXEC_CWD_DENIED"
ACTION_PLAN_PATH_PRIVATE = "ACTION_PLAN_PATH_PRIVATE"
SUGGESTION_LIMIT = 3
SUGGESTION_SCAN_LIMIT = 200

_READ_WORKSPACE_SCOPE_ERROR_CODES = frozenset(
    {
        WORKSPACE_PATH_OUTSIDE_ROOTS,
        WORKSPACE_PATH_ROOT_NOT_READABLE,
    }
)


@dataclass(frozen=True, slots=True)
class ExecSandboxRoots:
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]


def resolve_read_tool_path(
    *,
    context: BrokerContext,
    raw_path: str,
    must_exist: bool,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> ResolvedManifestPath:
    if context.path_access_kind != "read":
        raise BrokerPolicyError(
            f"tool {context.tool_definition.tool_id} is not a read/search tool",
            code=READ_PATH_DENIED,
        )
    try:
        resolved = _resolve_read_tool_path_unchecked(
            context=context,
            raw_path=raw_path,
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    except FileNotFoundError as exc:
        suggestions = _resolve_missing_path_suggestions(
            context=context,
            raw_path=raw_path,
        )
        raise _missing_path_error(
            raw_path=raw_path.strip(),
            suggestions=suggestions,
            full_access=context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS,
        ) from exc
    reject_private_action_plan_path(context=context, path=resolved.path)
    if private_app_storage_filter(context)(resolved.path):
        raise private_app_storage_error(code=READ_PATH_DENIED)
    return resolved


def hidden_read_path_filter(context: BrokerContext) -> Callable[[Path], bool]:
    is_private_storage = private_app_storage_filter(context)
    return lambda path: (
        is_private_storage(path)
        or is_private_action_plan_path(context=context, path=path)
    )


def resolve_write_tool_path(
    *,
    context: BrokerContext,
    raw_path: str,
    must_exist: bool,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> ResolvedManifestPath | OutsideWorkspacePatchTarget:
    if context.path_access_kind != "write":
        raise BrokerPolicyError(
            f"tool {context.tool_definition.tool_id} is not a workspace write tool",
            code=WRITE_PATH_DENIED,
        )
    is_private_storage = private_app_storage_filter(context)
    try:
        resolved = resolve_local_path(
            roots=context.manifest_roots,
            raw_path=raw_path,
            cwd_path=Path(context.execution_session.cwd_path),
            capability="apply_patch",
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    except FileNotFoundError as exc:
        if not _missing_path_is_within_workspace_roots(
            context=context,
            raw_path=raw_path,
        ):
            raise _write_path_denied_error() from exc
        raise BrokerPolicyError(
            f"WRITE_PATH_NOT_FOUND: path must reference an existing workspace path: {raw_path.strip()}",
            code=WRITE_PATH_NOT_FOUND,
            fix_hint="Retry with an existing path under workspace roots",
        ) from exc
    except BrokerPolicyError as exc:
        if exc.code == WORKSPACE_PATH_OUTSIDE_ROOTS:
            candidate = _candidate_path(
                raw_path=raw_path,
                cwd_path=Path(context.execution_session.cwd_path),
            )
            outside = resolve_outside_workspace_patch_target(
                context=context, candidate=candidate
            )
            if outside is not None:
                return outside
            if is_private_storage(candidate.resolve(strict=False)):
                raise private_app_storage_error(code=WRITE_PATH_DENIED) from exc
        raise _write_path_denied_error() from exc
    reject_private_action_plan_path(context=context, path=resolved.path)
    # Manifest roots match the most specific root first, so a path in the
    # Action's own storage roots resolves to them and gets their permissions.
    if is_private_storage(resolved.path):
        raise private_app_storage_error(code=WRITE_PATH_DENIED)
    return resolved


def is_private_action_plan_path(
    *,
    context: BrokerContext,
    path: Path,
) -> bool:
    return is_action_plan_artifact_path(
        scratch_root=context.scratch_root_path,
        path=path,
    )


def reject_private_action_plan_path(
    *,
    context: BrokerContext,
    path: Path,
) -> None:
    if not is_private_action_plan_path(context=context, path=path):
        return
    raise BrokerPolicyError(
        "The app-managed Action plan is private to the parent plan tools",
        code=ACTION_PLAN_PATH_PRIVATE,
    )


def resolve_exec_tool_cwd(
    *,
    context: BrokerContext,
    raw_cwd: str | None,
) -> ResolvedProcessCwd | OutsideWorkspaceCwd:
    if context.path_access_kind != "exec":
        raise BrokerPolicyError(
            f"tool {context.tool_definition.tool_id} is not a workspace exec tool",
            code=EXEC_CWD_DENIED,
        )
    is_private_storage = private_app_storage_filter(context)
    try:
        resolved = resolve_process_cwd(
            roots=context.manifest_roots,
            raw_cwd=raw_cwd,
            default_cwd=Path(context.execution_session.cwd_path),
        )
    except FileNotFoundError as exc:
        raw_display = (raw_cwd or ".").strip()
        if not _missing_path_is_within_workspace_roots(
            context=context,
            raw_path=raw_display,
        ):
            raise _exec_cwd_denied_error() from exc
        raise BrokerPolicyError(
            f"EXEC_CWD_NOT_FOUND: cwd must reference an existing workspace directory: {raw_display}",
            code=EXEC_CWD_NOT_FOUND,
            fix_hint="Retry with `.` or an existing directory under workspace roots.",
        ) from exc
    except BrokerPolicyError as exc:
        if exc.code == WORKSPACE_PATH_OUTSIDE_ROOTS:
            candidate = _candidate_path(
                raw_path=raw_cwd or ".",
                cwd_path=Path(context.execution_session.cwd_path),
            )
            outside = resolve_outside_workspace_cwd(
                context=context, candidate=candidate
            )
            if outside is not None:
                return outside
            if is_private_storage(candidate.resolve(strict=False)):
                raise private_app_storage_error(code=EXEC_CWD_DENIED) from exc
        raise _exec_cwd_denied_error() from exc
    if is_private_storage(resolved.path):
        raise private_app_storage_error(code=EXEC_CWD_DENIED)
    return resolved


def resolve_exec_sandbox_roots(
    *,
    context: BrokerContext,
    action_temp_dir: Path,
) -> ExecSandboxRoots:
    if context.path_access_kind != "exec":
        raise BrokerPolicyError(
            f"tool {context.tool_definition.tool_id} is not a workspace exec tool",
            code=EXEC_CWD_DENIED,
        )
    process_read_roots: list[Path] = []
    process_write_roots: list[Path] = []
    for root in context.manifest_roots:
        if root.can_process_read or root.can_process_write:
            validate_manifest_root(root)
        if root.can_process_read:
            process_read_roots.append(root.canonical_real_path)
        if root.can_process_write:
            process_write_roots.append(root.canonical_real_path)
    return ExecSandboxRoots(
        read_roots=_dedupe_resolved_paths(
            *process_read_roots,
            action_temp_dir,
        ),
        write_roots=_dedupe_resolved_paths(
            *process_write_roots,
        ),
    )


def _resolve_read_tool_path_unchecked(
    *,
    context: BrokerContext,
    raw_path: str,
    must_exist: bool,
    must_be_file: bool,
    must_be_dir: bool,
) -> ResolvedManifestPath:
    if context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return _resolve_full_access_path(
            raw_path=raw_path,
            cwd_path=Path(context.execution_session.cwd_path),
            manifest_roots=context.manifest_roots,
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    if context.read_access_scope != READ_ACCESS_SCOPE_WORKSPACE:
        raise BrokerPolicyError(
            f"Unsupported read_access_scope: {context.read_access_scope}",
            code=READ_PATH_DENIED,
        )
    try:
        return resolve_local_path(
            roots=context.manifest_roots,
            raw_path=raw_path,
            cwd_path=Path(context.execution_session.cwd_path),
            capability="read",
            must_exist=must_exist,
            must_be_file=must_be_file,
            must_be_dir=must_be_dir,
        )
    except BrokerPolicyError as exc:
        if exc.code == WORKSPACE_PATH_ESCAPES_ROOT or (
            exc.code == WORKSPACE_PATH_OUTSIDE_ROOTS
            and _raw_candidate_starts_inside_readable_workspace_root(
                context=context,
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
    cwd_path: Path,
    manifest_roots: tuple[ManifestRoot, ...],
    must_exist: bool,
    must_be_file: bool,
    must_be_dir: bool,
) -> ResolvedManifestPath:
    candidate = _candidate_path(raw_path=raw_path, cwd_path=cwd_path)
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
    context: BrokerContext,
    raw_path: str,
) -> tuple[str, ...]:
    candidate = _candidate_path(
        raw_path=raw_path,
        cwd_path=Path(context.execution_session.cwd_path),
    )
    _ensure_missing_path_is_in_read_scope(context=context, raw_path=str(candidate))
    parent = candidate.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except FileNotFoundError:
        return ()
    if not resolved_parent.is_dir():
        return ()
    _ensure_missing_path_is_in_read_scope(
        context=context, raw_path=str(resolved_parent)
    )
    if private_app_storage_filter(context)(resolved_parent):
        raise private_app_storage_error(code=READ_PATH_DENIED)
    is_hidden = hidden_read_path_filter(context)
    visible_suggestions: list[str] = []
    for suggestion in _suggest_local_paths(parent=resolved_parent, raw_path=raw_path):
        try:
            resolved_suggestion = Path(suggestion).resolve(strict=False)
        except RuntimeError:
            continue
        if not is_hidden(resolved_suggestion):
            visible_suggestions.append(suggestion)
    return tuple(visible_suggestions)


def _ensure_missing_path_is_in_read_scope(
    *,
    context: BrokerContext,
    raw_path: str,
) -> None:
    if context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return
    try:
        _resolve_read_tool_path_unchecked(
            context=context,
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


def _candidate_path(*, raw_path: str, cwd_path: Path) -> Path:
    stripped = raw_path.strip()
    if not stripped:
        raise BrokerPolicyError("path must not be empty")
    if stripped.startswith("~"):
        raise BrokerPolicyError("path must not use shell home expansion")
    path = Path(stripped)
    return path if path.is_absolute() else cwd_path / path


def _dedupe_resolved_paths(*paths: Path) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(path.resolve() for path in paths))


def _raw_candidate_starts_inside_readable_workspace_root(
    *,
    context: BrokerContext,
    raw_path: str,
) -> bool:
    candidate = _candidate_path(
        raw_path=raw_path,
        cwd_path=Path(context.execution_session.cwd_path),
    )
    for root in context.manifest_roots:
        if not root.can_read:
            continue
        try:
            candidate.relative_to(root.canonical_real_path)
        except ValueError:
            continue
        return True
    return False


def _missing_path_is_within_workspace_roots(
    *,
    context: BrokerContext,
    raw_path: str,
) -> bool:
    try:
        candidate = _candidate_path(
            raw_path=raw_path,
            cwd_path=Path(context.execution_session.cwd_path),
        ).resolve(strict=False)
    except OSError:
        return False
    for root in context.manifest_roots:
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
) -> BrokerPolicyError:
    details = f"READ_PATH_NOT_FOUND: path does not exist: {raw_path}"
    if suggestions:
        details += "\n\nDid you mean one of these?\n" + "\n".join(suggestions)
    return BrokerPolicyError(
        details,
        code=READ_PATH_NOT_FOUND,
        fix_hint=(
            "Retry with an existing local path. Absolute paths and paths relative to "
            "the current cwd are valid for read/search."
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


def _write_path_denied_error() -> BrokerPolicyError:
    return BrokerPolicyError(
        "WRITE_PATH_DENIED: path is not writable by apply_patch.",
        code=WRITE_PATH_DENIED,
        fix_hint="Use a path under Workspace Roots that is writable by apply_patch.",
    )


def _exec_cwd_denied_error() -> BrokerPolicyError:
    return BrokerPolicyError(
        "EXEC_CWD_DENIED: cwd is not allowed for command execution.",
        code=EXEC_CWD_DENIED,
        fix_hint=(
            "Use `.` or a command cwd under Workspace Roots with command execution access."
        ),
    )


__all__ = [
    "ACTION_PLAN_PATH_PRIVATE",
    "ExecSandboxRoots",
    "READ_PATH_DENIED",
    "READ_PATH_NOT_FOUND",
    "READ_SCOPE_DENIED",
    "EXEC_CWD_DENIED",
    "EXEC_CWD_NOT_FOUND",
    "SUGGESTION_SCAN_LIMIT",
    "WRITE_PATH_DENIED",
    "WRITE_PATH_NOT_FOUND",
    "hidden_read_path_filter",
    "resolve_exec_sandbox_roots",
    "resolve_exec_tool_cwd",
    "resolve_read_tool_path",
    "resolve_write_tool_path",
]
