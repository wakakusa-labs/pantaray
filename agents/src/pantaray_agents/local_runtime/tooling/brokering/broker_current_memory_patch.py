"""Apply Action patches to the user's current Agent Experience files."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path, PurePosixPath

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    DescriptorPathMissingError,
    DescriptorPathPolicyError,
    open_directory_at_descriptor,
)
from pantaray_agents.local_runtime.descriptor_write import CommittedFileWriteError
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.current_files_index import (
    refresh_current_memory_index,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MemoryFileConflictError,
    MemoryFiles,
    editable_memory_relative_path,
    locked_memory_files,
    validate_editable_memory_path,
)
from pantaray_agents.local_runtime.memory_catalog.errors import MemoryCatalogError
from pantaray_agents.local_runtime.memory_catalog.reference_edits import (
    validate_reference_text_replacement,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.tools.contract import BrokerPolicyError

from .action_subagent_broker_authority import authorize_direct_workspace_writes
from .broker_common import BrokerContext
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_patch_read_gate import needs_read_output
from .broker_protocol import (
    ApplyPatchAddChange,
    ApplyPatchUpdateChange,
    ValidatedPatchRequest,
)
from .broker_structured_patch import (
    PATCH_ERROR_PATH_INVALID,
    PATCH_ERROR_READ_FAILED,
    PATCH_ERROR_TARGET_EXISTS,
    PATCH_ERROR_TARGET_MISSING,
    PATCH_ERROR_TARGET_NOT_FILE,
    PATCH_ERROR_WRITE_FAILED,
    StructuredPatchError,
    StructuredPatchFileChange,
    apply_patch_edits,
    create_patch_diff,
    join_patch_new_file_text,
)


def run_current_memory_patch(
    *,
    context: BrokerContext,
    patch_root: Path,
    path_rewrites: dict[str, str],
    request: ValidatedPatchRequest,
) -> UnprojectedBrokerToolOutcome:
    artifact_root = read_local_runtime_artifact_root()
    user_id = context.execution_session.user_id
    expected_root = (
        artifact_root / editable_memory_relative_path(user_id) / "agent_experience"
    )
    if patch_root != expected_root.resolve():
        raise BrokerPolicyError(
            "Agent Experience root does not belong to this user",
            code="WRITE_PATH_DENIED",
        )
    change = request.changes[0]
    memory_path = f"agent_experience/{path_rewrites[change.path]}"
    target = str(patch_root / path_rewrites[change.path])
    try:
        validate_editable_memory_path(memory_path)
    except ValueError as exc:
        raise StructuredPatchError(str(exc), code=PATCH_ERROR_PATH_INVALID) from exc

    saved = False
    try:
        # Workspace lease -> memory directory lock -> DB write authority. No LLM
        # or network operation runs while the file lock is held.
        with (
            locked_memory_files(artifact_root=artifact_root, user_id=user_id) as files,
            open_memory_catalog_connection(
                db_path=context.db_path, busy_timeout_ms=context.busy_timeout_ms
            ) as connection,
            immediate_transaction(connection),
        ):
            authorize_direct_workspace_writes(
                context=context, resolved_paths=(Path(target),)
            )
            if isinstance(change, ApplyPatchAddChange):
                _require_absent_add_target(files.descriptor, memory_path)
            old_text = (
                ""
                if isinstance(change, ApplyPatchAddChange)
                else _read_patch_target(files, memory_path)
            )
            if not isinstance(change, ApplyPatchAddChange):
                read_output = needs_read_output(
                    context=context, path=target, current_text=old_text, change=change
                )
                if read_output is not None:
                    return UnprojectedBrokerToolOutcome(
                        status="success",
                        output=read_output,
                        stdout_text=None,
                        stderr_text=None,
                        file_paths=(),
                    )
            if isinstance(change, ApplyPatchAddChange):
                new_text = join_patch_new_file_text(
                    lines=change.new_lines, trailing_newline=change.trailing_newline
                )
            elif isinstance(change, ApplyPatchUpdateChange):
                new_text = apply_patch_edits(old_text=old_text, edits=change.edits)
            else:
                new_text = ""
            validate_reference_text_replacement(
                path=memory_path, before=old_text, after=new_text
            )
            if change.op == "delete":
                files.delete(path=memory_path, expected_text=old_text)
            else:
                files.write(
                    path=memory_path,
                    text=new_text,
                    expected_text=None
                    if isinstance(change, ApplyPatchAddChange)
                    else old_text,
                )
            saved = True
            refresh_current_memory_index(connection=connection, files=files)
            diff_text = create_patch_diff(
                StructuredPatchFileChange(
                    operation=change.op,
                    path=change.path,
                    old_text=old_text,
                    new_text=new_text,
                )
            )
    except (
        MemoryCatalogError,
        MemoryFileConflictError,
        DescriptorPathError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        if isinstance(exc, (MemoryFileConflictError, FileExistsError)) and isinstance(
            change, ApplyPatchAddChange
        ):
            raise StructuredPatchError(
                f"{PATCH_ERROR_TARGET_EXISTS}: target already exists: {change.path}",
                code=PATCH_ERROR_TARGET_EXISTS,
            ) from exc
        committed = saved or isinstance(exc, CommittedFileWriteError)
        raise StructuredPatchError(
            f"{PATCH_ERROR_WRITE_FAILED}: {exc}. Read the current file before retrying.",
            code=PATCH_ERROR_WRITE_FAILED,
            applied_paths=(change.path,) if committed else (),
        ) from exc
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={"status": "success", "applied_paths": [target], "diff": diff_text},
        stdout_text=diff_text,
        stderr_text=None,
        file_paths=(change.path,),
        file_reference_paths=() if change.op == "delete" else (target,),
    )


def _require_absent_add_target(descriptor: int, path: str) -> None:
    # Add rejects directories too; reading a regular file is not an existence test.
    target = PurePosixPath(path)
    try:
        parent = open_directory_at_descriptor(
            parent_descriptor=descriptor, relative_path=str(target.parent)
        )
    except DescriptorPathMissingError:
        return
    try:
        try:
            os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise MemoryFileConflictError(f"memory add target already exists: {path}")
    finally:
        os.close(parent)


def _read_patch_target(files: MemoryFiles, path: str) -> str:
    try:
        return files.read(path)
    except DescriptorPathMissingError as exc:
        raise StructuredPatchError(str(exc), code=PATCH_ERROR_TARGET_MISSING) from exc
    except DescriptorPathPolicyError as exc:
        raise StructuredPatchError(str(exc), code=PATCH_ERROR_TARGET_NOT_FILE) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise StructuredPatchError(str(exc), code=PATCH_ERROR_READ_FAILED) from exc
