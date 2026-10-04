from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from ..locks.workspace_lock_coordinator import (
    WorkspaceLockConflictError,
    acquire_workspace_root_lock,
    release_workspace_lock,
)
from ..outside_workspace_grant import folder_can_be_granted
from .action_subagent_broker_authority import authorize_direct_workspace_writes
from .broker_common import (
    BrokerContext,
    BrokerExecutionError,
    BrokerPolicyError,
)
from .broker_current_memory_patch import run_current_memory_patch
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_patch_read_gate import needs_read_output
from .broker_protocol import (
    ApplyPatchAddChange,
    ValidatedPatchRequest,
)
from .broker_structured_patch import (
    StructuredPatchError,
    apply_structured_workspace_patch,
    read_structured_patch_target_text,
    structured_patch_llm_feedback,
)
from .command_approval_summaries import build_apply_patch_summary
from .manifest_paths import ManifestRoot, ResolvedManifestPath
from .outside_workspace import (
    OutsideWorkspacePatchTarget,
    outside_workspace_resolved_path,
)
from .tool_path_policy import resolve_write_tool_path

PATCH_ERROR_PATH_MULTIPLE_MOUNTS = "PATCH_PATH_MOUNT_MISMATCH"
PATCH_ERROR_PATH_CONFLICT = "PATCH_PATH_CONFLICT"
PATCH_ERROR_PATH_RETARGETED = "PATCH_PATH_RETARGETED"


@dataclass(frozen=True, slots=True)
class PatchMountPlan:
    root: ManifestRoot
    path_rewrites: dict[str, str]
    # Set when the one root is a folder outside the workspace; writing there
    # needs the user's approval of this exact tool call.
    outside_workspace_folder: Path | None


def resolve_patch_mount_plan(
    *,
    context: BrokerContext,
    patch_paths: tuple[str, ...],
) -> PatchMountPlan:
    resolved_paths: list[tuple[str, ResolvedManifestPath]] = []
    for path in patch_paths:
        resolved = resolve_write_tool_path(
            context=context,
            raw_path=path,
            must_exist=False,
        )
        if isinstance(resolved, OutsideWorkspacePatchTarget):
            resolved = outside_workspace_resolved_path(context=context, target=resolved)
        resolved_paths.append((path, resolved))
    resolved_only = [resolved for _original, resolved in resolved_paths]
    authorize_direct_workspace_writes(
        context=context,
        resolved_paths=tuple(resolved.path for resolved in resolved_only),
    )
    root_ids = {path.root.root_id for path in resolved_only}
    if len(root_ids) != 1:
        raise BrokerPolicyError(
            f"{PATCH_ERROR_PATH_MULTIPLE_MOUNTS}: apply_patch paths must belong "
            "to one workspace root",
            code=PATCH_ERROR_PATH_MULTIPLE_MOUNTS,
        )
    _reject_resolved_patch_path_conflicts(tuple(resolved_paths))
    patch_root = resolved_only[0].root
    return PatchMountPlan(
        root=patch_root,
        path_rewrites={
            original: resolved.root_relative_path
            for original, resolved in resolved_paths
        }
        | {
            str(resolved.path): resolved.root_relative_path
            for _original, resolved in resolved_paths
        },
        outside_workspace_folder=(
            None
            if patch_root in context.manifest_roots
            else patch_root.canonical_real_path
        ),
    )


def build_patch_approval_summary(
    *,
    context: BrokerContext,
    patch_paths: tuple[str, ...],
    plan: PatchMountPlan,
) -> dict[str, JSONValue]:
    folder = plan.outside_workspace_folder
    return build_apply_patch_summary(
        patch_paths=patch_paths,
        outside_workspace_folder=folder,
        outside_workspace_grantable=(
            folder is not None
            and folder_can_be_granted(folder=folder, db_path=context.db_path)
        ),
    )


def _reject_resolved_patch_path_conflicts(
    resolved_paths: tuple[tuple[str, ResolvedManifestPath], ...],
) -> None:
    seen_by_mount_path: dict[str, str] = {}
    for original_path, resolved in resolved_paths:
        previous_path = seen_by_mount_path.get(resolved.root_relative_path)
        if previous_path is not None and previous_path != original_path:
            raise BrokerPolicyError(
                f"{PATCH_ERROR_PATH_CONFLICT}: patch paths {previous_path!r} and "
                f"{original_path!r} resolve to the same workspace file",
                code=PATCH_ERROR_PATH_CONFLICT,
            )
        seen_by_mount_path[resolved.root_relative_path] = original_path


def run_apply_patch_executor(
    *,
    context: BrokerContext,
    request: ValidatedPatchRequest,
) -> UnprojectedBrokerToolOutcome:
    patch_paths = tuple(request.patch_paths)
    if request.tool_invocation_id is None or not request.tool_invocation_id.strip():
        raise BrokerPolicyError(
            "invocation_id is required for non-preflight workspace mutation tools"
        )
    plan = resolve_patch_mount_plan(context=context, patch_paths=patch_paths)
    if (
        build_patch_approval_summary(
            context=context, patch_paths=patch_paths, plan=plan
        )
        != request.command_summary_json
    ):
        # Consent names the folder; a path re-resolving elsewhere is not approved.
        raise BrokerPolicyError(
            "apply_patch destination changed after approval",
            code=PATCH_ERROR_PATH_RETARGETED,
        )
    patch_mount = plan.root
    path_rewrites = plan.path_rewrites
    patch_root = patch_mount.canonical_real_path
    locked_resolved_paths = tuple(
        patch_root / path_rewrites[path] for path in patch_paths
    )
    workspace_lock_lease = None
    try:
        workspace_lock_lease = acquire_workspace_root_lock(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            lock_key=str(patch_root),
            workspace_root=patch_root,
            execution_session_id=context.execution_session.execution_session_id,
            tool_invocation_id=request.tool_invocation_id,
            action_id=context.execution_session.action_id,
            acquired_at=request.requested_at,
        )
    except WorkspaceLockConflictError as exc:
        raise BrokerExecutionError(f"PATCH_LOCK_CONFLICT: {exc}") from exc

    try:
        try:
            if patch_mount.source_type == "agent_experience":
                return run_current_memory_patch(
                    context=context,
                    patch_root=patch_root,
                    path_rewrites=path_rewrites,
                    request=request,
                )
            read_output = _needs_read_output_if_required(
                context=context,
                patch_root=patch_root,
                path_rewrites=path_rewrites,
                request=request,
            )
            if read_output is not None:
                return UnprojectedBrokerToolOutcome(
                    status="success",
                    output=read_output,
                    stdout_text=None,
                    stderr_text=None,
                    file_paths=(),
                )
            if any(
                path.resolve(strict=False) != path for path in locked_resolved_paths
            ):
                raise BrokerPolicyError(
                    "apply_patch destination changed after workspace lock acquisition",
                    code=PATCH_ERROR_PATH_RETARGETED,
                )
            authorize_direct_workspace_writes(
                context=context,
                resolved_paths=locked_resolved_paths,
            )
            diff_text = apply_structured_workspace_patch(
                patch_root=patch_root,
                path_rewrites=path_rewrites,
                changes=request.changes,
            )
        except StructuredPatchError as exc:
            return _structured_patch_error_outcome(
                error=exc,
                patch_paths=patch_paths,
                applied_paths=tuple(
                    str(patch_root / path_rewrites[path]) for path in exc.applied_paths
                ),
                file_reference_paths=_existing_patch_file_references(
                    plan=plan,
                    paths=tuple(exc.applied_paths),
                ),
            )
        return UnprojectedBrokerToolOutcome(
            status="success",
            output={
                "status": "success",
                "applied_paths": [str(path) for path in locked_resolved_paths],
                "diff": diff_text,
            },
            stdout_text=diff_text,
            stderr_text=None,
            file_paths=patch_paths,
            file_reference_paths=_existing_patch_file_references(
                plan=plan,
                paths=patch_paths,
            ),
        )
    finally:
        if workspace_lock_lease is not None:
            release_workspace_lock(
                db_path=context.db_path,
                busy_timeout_ms=context.busy_timeout_ms,
                lease=workspace_lock_lease,
                released_at=now_utc_iso(),
            )


def _needs_read_output_if_required(
    *,
    context: BrokerContext,
    patch_root: Path,
    path_rewrites: dict[str, str],
    request: ValidatedPatchRequest,
) -> dict[str, JSONValue] | None:
    change = request.changes[0]
    if isinstance(change, ApplyPatchAddChange):
        return None
    current_text = read_structured_patch_target_text(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        path=change.path,
    )
    return needs_read_output(
        context=context,
        path=str(patch_root / path_rewrites[change.path]),
        current_text=current_text,
        change=change,
    )


def _existing_patch_file_references(
    *,
    plan: PatchMountPlan,
    paths: tuple[str, ...],
) -> tuple[str, ...]:
    # File references bind to manifest roots; an approved outside folder has none.
    if plan.outside_workspace_folder is not None:
        return ()
    patch_root = plan.root.canonical_real_path
    path_rewrites = plan.path_rewrites
    references: list[str] = []
    for path in paths:
        rewritten = path_rewrites.get(path)
        if rewritten is None:
            continue
        target = (patch_root / rewritten).resolve(strict=False)
        if target.is_file():
            references.append(str(target))
    return tuple(references)


def _structured_patch_error_outcome(
    *,
    error: StructuredPatchError,
    patch_paths: tuple[str, ...],
    applied_paths: tuple[str, ...],
    file_reference_paths: tuple[str, ...],
) -> UnprojectedBrokerToolOutcome:
    output: dict[str, JSONValue] = {
        "status": "error",
        "applied_paths": list(applied_paths),
        "error": {
            "type": "PatchApplyError",
            "code": error.code,
            "message": str(error),
            "llm_feedback": structured_patch_llm_feedback(error.code),
            "exit_code": None,
        },
    }
    return UnprojectedBrokerToolOutcome(
        status="error",
        output=output,
        stdout_text=None,
        stderr_text=str(error),
        file_paths=patch_paths,
        file_reference_paths=file_reference_paths,
    )
