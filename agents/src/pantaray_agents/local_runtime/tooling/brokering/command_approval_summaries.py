"""Canonical command descriptions shared by consent and invocation auditing."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from pantaray_agents.schema.agent.base import JSONValue


def build_apply_patch_summary(
    *,
    patch_paths: tuple[str, ...],
    outside_workspace_folder: Path | None = None,
    outside_workspace_grantable: bool = False,
) -> dict[str, JSONValue]:
    summary: dict[str, JSONValue] = {
        "summary_kind": "apply_patch",
        "target_paths": list(patch_paths),
    }
    return _with_outside_workspace(
        summary,
        () if outside_workspace_folder is None else (outside_workspace_folder,),
        can_allow_for_conversation=outside_workspace_grantable,
    )


def build_bash_summary(
    *,
    command: str,
    cwd_relative_path: str,
    timeout_ms: int,
    use_login_environment: bool,
    run_outside_sandbox: bool,
    reason: str | None,
    outside_workspace_folders: tuple[Path, ...] = (),
) -> dict[str, JSONValue]:
    summary: dict[str, JSONValue] = {
        "summary_kind": "bash",
        "command": command,
        "cwd": cwd_relative_path,
        "timeout_ms": timeout_ms,
        "use_login_environment": use_login_environment,
        # The model's user-facing justification, shown as the approval question.
        "reason": reason,
    }
    if run_outside_sandbox:
        # The approval UI keys its risk wording on this, and the invocation's
        # stored summary records that the approved call ran unsandboxed. No
        # folder is named: the run is not confined to folders at all, and with
        # none "Allow for this conversation" has nothing to grant, so this
        # approval stays one time only.
        summary["run_outside_sandbox"] = True
        return summary
    # An outside command folder is approvable only when it could be granted.
    return _with_outside_workspace(
        summary, outside_workspace_folders, can_allow_for_conversation=True
    )


def build_run_python_summary(
    *,
    cwd_relative_path: str,
    code: str,
    args_count: int,
    timeout_ms: int,
    reason: str | None,
    outside_workspace_folders: tuple[Path, ...] = (),
) -> dict[str, JSONValue]:
    encoded_code = code.encode("utf-8")
    summary: dict[str, JSONValue] = {
        "summary_kind": "run_python",
        "cwd": cwd_relative_path,
        "code_sha256": sha256(encoded_code).hexdigest(),
        "code_size_bytes": len(encoded_code),
        "args_count": args_count,
        "timeout_ms": timeout_ms,
        "reason": reason,
    }
    # An outside command folder is approvable only when it could be granted.
    return _with_outside_workspace(
        summary, outside_workspace_folders, can_allow_for_conversation=True
    )


def _with_outside_workspace(
    summary: dict[str, JSONValue],
    folders: tuple[Path, ...],
    *,
    can_allow_for_conversation: bool,
) -> dict[str, JSONValue]:
    if folders:
        # The approval UI reads this exact shape to name the folders being opened
        # and to offer "Allow for this conversation", which grants every folder.
        summary["outside_workspace"] = {
            "folders": [
                {"path": str(folder), "display_name": folder.name or str(folder)}
                for folder in folders
            ],
            "can_allow_for_conversation": can_allow_for_conversation,
        }
    return summary


def outside_workspace_folder_paths(summary: dict[str, JSONValue]) -> tuple[str, ...]:
    """The folders outside the workspace that an approval summary opens."""

    outside = summary.get("outside_workspace")
    if not isinstance(outside, dict):
        return ()
    folders = outside.get("folders")
    if not isinstance(folders, list):
        return ()
    return tuple(
        path
        for folder in folders
        if isinstance(folder, dict) and isinstance(path := folder.get("path"), str)
    )
