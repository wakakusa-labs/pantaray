from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_FULL_ACCESS
from pantaray_agents.tools.contract import BrokerPolicyError

from ..action_session_temp_paths import resolve_action_storage_paths
from ..models import BrokerNetworkPolicy
from ..outside_workspace_grant import app_owned_roots
from ..repository.command_network_settings import load_command_network_enabled
from ..sandbox.runtime_policy import resolve_runtime_budget
from .action_path_policy import (
    ExecSandboxRoots,
    resolve_exec_sandbox_roots,
    resolve_exec_tool_cwd,
)
from .action_subagent_broker_authority import (
    authorize_direct_workspace_writes,
    resolve_command_workspace_write_roots,
)
from .broker_common import (
    BrokerContext,
    ensure_tool_authorization,
)
from .broker_protocol import (
    BashToolArgs,
    BrokerExecutableSourceKind,
    BrokerExecutionKind,
    RunPythonToolArgs,
    ValidatedCommandRequest,
)
from .command_approval_summaries import (
    build_bash_summary,
    build_run_python_summary,
    outside_workspace_folder_paths,
)
from .command_runtime import build_command_env
from .outside_workspace import OutsideWorkspaceCwd

EXEC_CWD_RETARGETED = "EXEC_CWD_RETARGETED"
EXEC_WRITE_FOLDER_DENIED = "EXEC_WRITE_FOLDER_DENIED"
# What a command run outside the sandbox can write. Recorded as its write root,
# it keeps the run and Action subagent claims apart both ways: the run is
# refused while any claim or another actor's command is active (and always to
# a subagent), and no claim can be taken until the run is cleaned up.
UNSANDBOXED_WRITE_ROOTS = (Path("/"),)


@dataclass(frozen=True, slots=True)
class _CommandCwd:
    path: Path
    # PATH lookups for project tools (.venv/bin, node_modules/.bin) stay under it.
    execution_root: Path
    # Folders outside the workspace the command may write: an outside cwd and
    # the requested write folders. Running with any needs the user's approval of
    # this exact tool call.
    outside_workspace_folders: tuple[Path, ...]


def _resolve_command_cwd(
    *,
    context: BrokerContext,
    raw_cwd: str | None,
    additional_write_folders: list[str],
) -> _CommandCwd:
    resolved = resolve_exec_tool_cwd(context=context, raw_cwd=raw_cwd)
    resolved_folders = (
        _resolve_write_folder(context=context, raw_folder=raw_folder)
        for raw_folder in additional_write_folders
    )
    requested = tuple(folder for folder in resolved_folders if folder is not None)
    if isinstance(resolved, OutsideWorkspaceCwd):
        return _CommandCwd(
            path=resolved.path,
            execution_root=resolved.path,
            outside_workspace_folders=tuple(dict.fromkeys((resolved.path, *requested))),
        )
    return _CommandCwd(
        path=resolved.path,
        execution_root=resolved.process_scope_root,
        outside_workspace_folders=tuple(dict.fromkeys(requested)),
    )


def _resolve_write_folder(*, context: BrokerContext, raw_folder: str) -> Path | None:
    """Resolve one requested write folder; None when the workspace already covers it.

    A command may write anywhere under the folder, so it passes the rule for an
    outside command cwd, and is refused where that cwd would be.
    """

    stripped = raw_folder.strip()
    # The command's HOME is temporary unless it uses the login environment, so
    # `~` names the user's real home, as it does for use_login_environment.
    if stripped == "~" or stripped.startswith("~/"):
        folder = Path.home() / stripped[2:]
    else:
        folder = Path(stripped)
    if not folder.is_absolute():
        raise BrokerPolicyError(
            f"{EXEC_WRITE_FOLDER_DENIED}: write folder must be absolute: {raw_folder}",
            code=EXEC_WRITE_FOLDER_DENIED,
            fix_hint="Use an absolute folder path, or one that starts with ~/.",
        )
    if not folder.is_dir():
        raise BrokerPolicyError(
            f"{EXEC_WRITE_FOLDER_DENIED}: write folder is not an existing folder: "
            f"{raw_folder}",
            code=EXEC_WRITE_FOLDER_DENIED,
            fix_hint=(
                "Request the nearest existing parent folder, or the folder that "
                "contains the file to write."
            ),
        )
    try:
        resolved = resolve_exec_tool_cwd(context=context, raw_cwd=str(folder))
    except BrokerPolicyError as exc:
        raise BrokerPolicyError(
            f"{EXEC_WRITE_FOLDER_DENIED}: this folder cannot be opened for writing: "
            f"{raw_folder}",
            code=EXEC_WRITE_FOLDER_DENIED,
            fix_hint=(
                "Pantaray's own storage, folders that contain it (/, the home "
                "folder, ~/Library), and links out of a workspace folder are never "
                "writable, so files directly in the home folder cannot be written. "
                "Request a more specific folder, or tell the user it cannot be done."
            ),
        ) from exc
    return resolved.path if isinstance(resolved, OutsideWorkspaceCwd) else None


def _command_network_policy(context: BrokerContext) -> BrokerNetworkPolicy:
    enabled = load_command_network_enabled(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=context.execution_session.user_id,
    )
    return "allow" if enabled else "deny"


def _resolve_command_sandbox_roots(
    *,
    context: BrokerContext,
    action_temp_dir: Path,
    outside_workspace_folders: tuple[Path, ...],
) -> ExecSandboxRoots:
    candidates = resolve_exec_sandbox_roots(
        context=context,
        action_temp_dir=action_temp_dir,
    )
    write_roots = resolve_command_workspace_write_roots(
        context=context,
        candidate_roots=candidates.write_roots,
    )
    if not outside_workspace_folders:
        return ExecSandboxRoots(
            read_roots=candidates.read_roots, write_roots=write_roots
        )
    # Denied outright rather than dropped from the roots, so an approved
    # command never runs without the folders it was approved for.
    authorize_direct_workspace_writes(
        context=context, resolved_paths=outside_workspace_folders
    )
    return ExecSandboxRoots(
        read_roots=(*candidates.read_roots, *outside_workspace_folders),
        write_roots=(*write_roots, *outside_workspace_folders),
    )


def verify_outside_workspace_folders_unchanged(
    *, context: BrokerContext, request: ValidatedCommandRequest
) -> None:
    """Re-resolve every approved outside-workspace folder right before launch."""

    for folder in outside_workspace_folder_paths(request.command_summary_json):
        # Each folder must still be the same outside folder it was approved as,
        # by the rule that admitted it: the one for an outside command cwd.
        resolved = resolve_exec_tool_cwd(context=context, raw_cwd=folder)
        if not (
            isinstance(resolved, OutsideWorkspaceCwd) and resolved.path == Path(folder)
        ):
            raise BrokerPolicyError(
                "command folder changed after approval",
                code=EXEC_CWD_RETARGETED,
            )


def build_validated_command_request(
    *,
    context: BrokerContext,
    args: BashToolArgs,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedCommandRequest:
    if not preflight_only and (
        tool_invocation_id is None or not tool_invocation_id.strip()
    ):
        raise BrokerPolicyError("tool_invocation_id is required for bash")
    command = args.command
    if not command.strip():
        raise BrokerPolicyError("bash.command must be a non-empty string")
    if "\x00" in command:
        raise BrokerPolicyError("bash.command must not contain NUL characters")
    raw_cwd = args.cwd
    action_temp_dir = context.execution_session.action_temp_dir
    app_runtime_python = context.execution_session.app_runtime_python
    if action_temp_dir is None or app_runtime_python is None:
        raise BrokerPolicyError(
            "execution session is missing action_temp_dir or app_runtime_python"
        )
    resolved_action_temp_dir = Path(action_temp_dir).resolve()
    resolved_app_runtime_python = Path(app_runtime_python).resolve()
    resolved_cwd = _resolve_command_cwd(
        context=context,
        raw_cwd=raw_cwd,
        additional_write_folders=args.additional_write_folders,
    )
    command_cwd = resolved_cwd.path
    resolved_executable = Path("/bin/bash")
    execution_kind: BrokerExecutionKind = "workspace_command"
    executable_source_kind: BrokerExecutableSourceKind = "trusted_system_executable"
    resolved_argv = [str(resolved_executable), "--noprofile", "--norc", "-c", command]
    use_login_environment = args.use_login_environment
    run_outside_sandbox = args.run_outside_sandbox
    if run_outside_sandbox:
        # Checked before the user is asked; the execution start checks again.
        authorize_direct_workspace_writes(
            context=context, resolved_paths=UNSANDBOXED_WRITE_ROOTS
        )
    # Commands read what the read-access setting allows, like the read tool.
    full_disk_read = context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
    env = build_command_env(
        command_cwd=command_cwd,
        execution_kind=execution_kind,
        workspace_root=resolved_cwd.execution_root,
        resolved_executable=resolved_executable,
        use_login_environment=use_login_environment,
        full_disk_read=full_disk_read,
    )
    runtime_budget = resolve_runtime_budget(
        sandbox_profile="workspace_process_exec", db_path=context.db_path
    )
    sandbox_roots = _resolve_command_sandbox_roots(
        context=context,
        action_temp_dir=resolved_action_temp_dir,
        outside_workspace_folders=resolved_cwd.outside_workspace_folders,
    )
    command_summary_json = build_bash_summary(
        command=command,
        cwd_relative_path=str(command_cwd),
        timeout_ms=runtime_budget.sandbox_launch.timeout_ms,
        use_login_environment=use_login_environment,
        run_outside_sandbox=run_outside_sandbox,
        reason=args.justification,
        outside_workspace_folders=resolved_cwd.outside_workspace_folders,
    )
    # The approval binds to this exact summary, command text included, and the
    # argv below runs that same text. A run outside the sandbox is asked every
    # time, whatever the approval mode.
    approval_session_id, approval_source = ensure_tool_authorization(
        context=context,
        tool_invocation_id=tool_invocation_id,
        tool_request_id=tool_request_id,
        command_summary=command_summary_json,
        requested_at=requested_at,
        require_user_prompt=(
            run_outside_sandbox or bool(resolved_cwd.outside_workspace_folders)
        ),
    )
    storage = resolve_action_storage_paths(
        db_path=context.db_path,
        user_id=context.execution_session.user_id,
        action_id=cast(str, context.execution_session.action_id),
    )
    return ValidatedCommandRequest(
        tool_invocation_id=tool_invocation_id,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session.execution_session_id,
        action_id=context.execution_session.action_id or "unknown",
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        private_storage_roots=[str(root) for root in app_owned_roots(context.db_path)],
        action_workspace_root=str(storage.workspace),
        published_results_root=str(storage.tool_results),
        app_runtime_python=str(resolved_app_runtime_python),
        cwd=str(command_cwd),
        command_summary_json=command_summary_json,
        argv=resolved_argv,
        resolved_executable_path=str(resolved_executable),
        execution_kind=execution_kind,
        executable_source_kind=executable_source_kind,
        env=env,
        timeout_ms=runtime_budget.sandbox_launch.timeout_ms,
        stdout_max_bytes=runtime_budget.sandbox_launch.stdout_max_bytes,
        stderr_max_bytes=runtime_budget.sandbox_launch.stderr_max_bytes,
        temp_storage_limit_bytes=runtime_budget.sandbox_launch.temp_storage_limit_bytes,
        child_count_limit=runtime_budget.broker_local.child_count_limit,
        open_file_lease_limit=runtime_budget.broker_local.open_file_lease_limit,
        network_policy=_command_network_policy(context),
        use_login_environment=use_login_environment,
        run_outside_sandbox=run_outside_sandbox,
        # The usual roots stay listed: their ancestor metadata grants let tools
        # such as git stat the parents of a workspace inside private storage.
        real_read_roots=[
            *(["/"] if full_disk_read else []),
            *(str(path) for path in sandbox_roots.read_roots),
        ],
        real_write_roots=[
            str(path)
            for path in (
                UNSANDBOXED_WRITE_ROOTS
                if run_outside_sandbox
                else sandbox_roots.write_roots
            )
        ],
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
    )


def build_validated_python_request(
    *,
    context: BrokerContext,
    args: RunPythonToolArgs,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedCommandRequest:
    if not preflight_only and (
        tool_invocation_id is None or not tool_invocation_id.strip()
    ):
        raise BrokerPolicyError("tool_invocation_id is required for run_python")
    if not args.code.strip():
        raise BrokerPolicyError("run_python.code must be a non-empty string")
    action_temp_dir = context.execution_session.action_temp_dir
    app_runtime_python = context.execution_session.app_runtime_python
    if action_temp_dir is None or app_runtime_python is None:
        raise BrokerPolicyError(
            "execution session is missing action_temp_dir or app_runtime_python"
        )
    resolved_action_temp_dir = Path(action_temp_dir).resolve()
    resolved_app_runtime_python = Path(app_runtime_python).resolve()
    resolved_cwd = _resolve_command_cwd(
        context=context,
        raw_cwd=args.cwd,
        additional_write_folders=args.additional_write_folders,
    )
    command_cwd = resolved_cwd.path
    runtime_budget = resolve_runtime_budget(
        sandbox_profile="agent_generated_python", db_path=context.db_path
    )
    full_disk_read = context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
    sandbox_roots = _resolve_command_sandbox_roots(
        context=context,
        action_temp_dir=resolved_action_temp_dir,
        outside_workspace_folders=resolved_cwd.outside_workspace_folders,
    )
    command_summary_json = build_run_python_summary(
        cwd_relative_path=str(command_cwd),
        code=args.code,
        args_count=len(args.args),
        timeout_ms=runtime_budget.sandbox_launch.timeout_ms,
        reason=args.justification,
        outside_workspace_folders=resolved_cwd.outside_workspace_folders,
    )
    approval_session_id, approval_source = ensure_tool_authorization(
        context=context,
        tool_invocation_id=tool_invocation_id,
        tool_request_id=tool_request_id,
        command_summary=command_summary_json,
        requested_at=requested_at,
        require_user_prompt=bool(resolved_cwd.outside_workspace_folders),
    )
    storage = resolve_action_storage_paths(
        db_path=context.db_path,
        user_id=context.execution_session.user_id,
        action_id=cast(str, context.execution_session.action_id),
    )
    return ValidatedCommandRequest(
        tool_invocation_id=tool_invocation_id,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session.execution_session_id,
        action_id=context.execution_session.action_id or "unknown",
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        private_storage_roots=[str(root) for root in app_owned_roots(context.db_path)],
        action_workspace_root=str(storage.workspace),
        published_results_root=str(storage.tool_results),
        app_runtime_python=str(resolved_app_runtime_python),
        cwd=str(command_cwd),
        command_summary_json=command_summary_json,
        argv=[str(resolved_app_runtime_python), *args.args],
        resolved_executable_path=str(resolved_app_runtime_python),
        execution_kind="agent_generated",
        executable_source_kind="app_runtime_python",
        env=build_command_env(
            command_cwd=command_cwd,
            execution_kind="agent_generated",
            workspace_root=resolved_cwd.execution_root,
            resolved_executable=resolved_app_runtime_python,
            use_login_environment=False,
            full_disk_read=full_disk_read,
        ),
        timeout_ms=runtime_budget.sandbox_launch.timeout_ms,
        stdout_max_bytes=runtime_budget.sandbox_launch.stdout_max_bytes,
        stderr_max_bytes=runtime_budget.sandbox_launch.stderr_max_bytes,
        temp_storage_limit_bytes=runtime_budget.sandbox_launch.temp_storage_limit_bytes,
        child_count_limit=runtime_budget.broker_local.child_count_limit,
        open_file_lease_limit=runtime_budget.broker_local.open_file_lease_limit,
        network_policy=_command_network_policy(context),
        use_login_environment=False,
        run_outside_sandbox=False,
        generated_python_code=args.code,
        real_read_roots=[
            *(["/"] if full_disk_read else []),
            *(str(path) for path in sandbox_roots.read_roots),
        ],
        real_write_roots=[str(path) for path in sandbox_roots.write_roots],
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
    )


__all__ = [
    "EXEC_CWD_RETARGETED",
    "EXEC_WRITE_FOLDER_DENIED",
    "UNSANDBOXED_WRITE_ROOTS",
    "build_validated_command_request",
    "build_validated_python_request",
    "verify_outside_workspace_folders_unchanged",
]
