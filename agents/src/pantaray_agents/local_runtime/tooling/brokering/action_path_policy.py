"""The Action's path checks: its read scope, and where it may write and run.

Reading itself is checked in ``tools.files.read_paths`` against the scope built
here; writes and commands are the broker's own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.manifest_paths import (
    WORKSPACE_PATH_OUTSIDE_ROOTS,
    ResolvedManifestPath,
    ResolvedProcessCwd,
    candidate_path,
    resolve_local_path,
    resolve_process_cwd,
    validate_manifest_root,
)
from pantaray_agents.tools.files.private_storage import (
    PrivateAppStorage,
    private_app_storage_error,
)
from pantaray_agents.tools.files.read_paths import READ_PATH_DENIED, resolve_read_path
from pantaray_agents.tools.files.read_scope import ReadScope

from ..outside_workspace_grant import app_owned_roots
from .broker_common import BrokerContext
from .outside_workspace import (
    OutsideWorkspaceCwd,
    OutsideWorkspacePatchTarget,
    resolve_outside_workspace_cwd,
    resolve_outside_workspace_patch_target,
)

WRITE_PATH_NOT_FOUND = "WRITE_PATH_NOT_FOUND"
WRITE_PATH_DENIED = "WRITE_PATH_DENIED"
EXEC_CWD_NOT_FOUND = "EXEC_CWD_NOT_FOUND"
EXEC_CWD_DENIED = "EXEC_CWD_DENIED"
# The app's own storage roots that an Action's manifest may grant back to it.
_APP_MANAGED_ROOT_SOURCE_TYPES = frozenset({"scratch", "agent_experience"})


@dataclass(frozen=True, slots=True)
class ExecSandboxRoots:
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]


def read_scope(context: BrokerContext) -> ReadScope:
    """The read scope of an Action's read/search tool call."""

    if context.path_access_kind != "read":
        raise BrokerPolicyError(
            f"tool {context.tool_definition.tool_id} is not a read/search tool",
            code=READ_PATH_DENIED,
        )
    return ReadScope(
        manifest_roots=context.manifest_roots,
        cwd_path=Path(context.execution_session.cwd_path),
        read_access_scope=context.read_access_scope,
        scratch_root_path=context.scratch_root_path,
        private_storage=_private_storage(context),
    )


def resolve_read_tool_path(
    *,
    context: BrokerContext,
    raw_path: str,
    must_exist: bool,
    must_be_file: bool = False,
) -> ResolvedManifestPath:
    """Resolve a path the Action's ``read`` tool could open (AGENTS.md lookup)."""

    return resolve_read_path(
        scope=read_scope(context),
        raw_path=raw_path,
        must_exist=must_exist,
        must_be_file=must_be_file,
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
    is_private_storage = _private_storage(context).hides
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
            candidate = candidate_path(
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
    # Manifest roots match the most specific root first, so a path in the
    # Action's own storage roots resolves to them and gets their permissions.
    if is_private_storage(resolved.path):
        raise private_app_storage_error(code=WRITE_PATH_DENIED)
    return resolved


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
    is_private_storage = _private_storage(context).hides
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
            candidate = candidate_path(
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


def _private_storage(context: BrokerContext) -> PrivateAppStorage:
    return PrivateAppStorage(
        storage_roots=app_owned_roots(context.db_path),
        readable_roots=tuple(
            root.canonical_real_path
            for root in context.manifest_roots
            if root.can_read and root.source_type in _APP_MANAGED_ROOT_SOURCE_TYPES
        ),
    )


def _dedupe_resolved_paths(*paths: Path) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(path.resolve() for path in paths))


def _missing_path_is_within_workspace_roots(
    *,
    context: BrokerContext,
    raw_path: str,
) -> bool:
    try:
        candidate = candidate_path(
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
    "EXEC_CWD_DENIED",
    "EXEC_CWD_NOT_FOUND",
    "WRITE_PATH_DENIED",
    "WRITE_PATH_NOT_FOUND",
    "ExecSandboxRoots",
    "read_scope",
    "resolve_exec_sandbox_roots",
    "resolve_exec_tool_cwd",
    "resolve_read_tool_path",
    "resolve_write_tool_path",
]
