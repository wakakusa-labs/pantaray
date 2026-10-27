"""The Action's PDF export: an HTML page printed by the desktop app, then saved.

The page is printed exactly as ``render_html_page`` shows it
(``broker_direct_render_html``). The PDF is written under ``apply_patch``'s rules
for one file: the same writable paths and approval summary, so a folder outside
the registered workspaces asks the user every time; the same recheck of the
destination before writing; and the workspace root lock while it is written.
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import replace
from pathlib import Path

from pantaray_agents.local_runtime.descriptor_write import (
    open_writable_parent_descriptor,
    write_bytes_at_descriptor,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.read_scope import ReadScope
from pantaray_agents.tools.files.render_pages import SaveHtmlAsPdfToolArgs

from ..locks.workspace_lock_coordinator import (
    WorkspaceLockConflictError,
    acquire_workspace_root_lock,
    release_workspace_lock,
)
from .action_path_policy import WRITE_PATH_DENIED, WRITE_PATH_NOT_FOUND, read_scope
from .action_subagent_broker_authority import authorize_direct_workspace_writes
from .broker_common import (
    BrokerContext,
    BrokerExecutionError,
    ensure_tool_authorization,
)
from .broker_direct import (
    PATCH_ERROR_PATH_RETARGETED,
    PatchMountPlan,
    build_patch_approval_summary,
    resolve_patch_mount_plan,
)
from .broker_direct_render_html import (
    BLOCKED_REQUESTS_NOTE,
    blocked_requests_output,
    print_html_file,
    resolve_html_file,
)
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedSaveHtmlAsPdfRequest


def build_validated_save_pdf_request(
    *,
    context: BrokerContext,
    args: SaveHtmlAsPdfToolArgs,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedSaveHtmlAsPdfRequest:
    # Both ends are checked before the user is asked, so an approval is never
    # spent on a call that cannot run.
    resolve_html_file(scope=_html_read_scope(context), raw_path=args.path)
    plan, _ = _pdf_destination(context=context, pdf_path=args.pdf_path)
    if not preflight_only and not tool_invocation_id:
        raise BrokerPolicyError("tool_invocation_id is required for save_html_as_pdf")
    command_summary = build_patch_approval_summary(
        context=context, patch_paths=(args.pdf_path,), plan=plan
    )
    approval_session_id, approval_source = ensure_tool_authorization(
        context=context,
        tool_invocation_id=tool_invocation_id,
        tool_request_id=tool_request_id,
        command_summary=command_summary,
        requested_at=requested_at,
        require_user_prompt=plan.outside_workspace_folder is not None,
    )
    return ValidatedSaveHtmlAsPdfRequest(
        tool_invocation_id=tool_invocation_id or "",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session.execution_session_id,
        action_id=context.execution_session.action_id or "unknown",
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        args=args,
        command_summary_json=command_summary,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
    )


async def run_save_html_as_pdf_executor(
    *, context: BrokerContext, request: ValidatedSaveHtmlAsPdfRequest
) -> UnprojectedBrokerToolOutcome:
    target, rendered = await print_html_file(
        context=context,
        scope=_html_read_scope(context),
        raw_path=request.args.path,
        action_id=request.action_id,
        tool_request_id=request.tool_request_id,
    )
    # Resolved again after printing, the step closest to the write: consent
    # names the file and its folder, and a path now leading elsewhere is not
    # what was approved.
    plan, destination = _pdf_destination(
        context=context, pdf_path=request.args.pdf_path
    )
    if (
        build_patch_approval_summary(
            context=context, patch_paths=(request.args.pdf_path,), plan=plan
        )
        != request.command_summary_json
    ):
        raise BrokerPolicyError(
            "save_html_as_pdf destination changed after approval",
            code=PATCH_ERROR_PATH_RETARGETED,
        )
    # The lock and the write run in one thread, so a Stop that abandons the
    # wait still leaves a whole file and a released lock behind.
    replaced = await asyncio.to_thread(
        _write_pdf_locked,
        context=context,
        request=request,
        plan=plan,
        destination=destination,
        pdf=rendered.pdf,
    )
    notes = [
        f"Saved {target.display_path} as {destination} ({len(rendered.pdf)} bytes)."
    ]
    if replaced:
        notes.append("It replaced the file that was there.")
    if rendered.blocked_requests:
        notes.append(BLOCKED_REQUESTS_NOTE)
    output: dict[str, JSONValue] = {
        "status": "saved",
        "path": target.display_path,
        "pdf_path": str(destination),
        "byte_size": len(rendered.pdf),
        "replaced": replaced,
        "blocked_requests": blocked_requests_output(rendered),
        "message": " ".join(notes),
    }
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output,
        file_paths=(request.args.pdf_path,),
        # File references bind to manifest roots; an approved outside folder has none.
        file_reference_paths=(
            () if plan.outside_workspace_folder is not None else (str(destination),)
        ),
    )


def _html_read_scope(context: BrokerContext) -> ReadScope:
    # The page is read as ``read`` would read it, from this write tool's context.
    return read_scope(replace(context, path_access_kind="read"))


def _pdf_destination(
    *, context: BrokerContext, pdf_path: str
) -> tuple[PatchMountPlan, Path]:
    plan = resolve_patch_mount_plan(context=context, patch_paths=(pdf_path,))
    destination = plan.root.canonical_real_path / plan.path_rewrites[pdf_path]
    # The resolved name: through a link named deck.pdf, another kind of file
    # would be overwritten with the PDF.
    if destination.suffix.lower() != ".pdf":
        raise BrokerPolicyError(
            f"pdf_path leads to a file that is not a PDF: {pdf_path}",
            code=WRITE_PATH_DENIED,
            fix_hint="Save to a new .pdf path, or to an existing PDF file.",
        )
    if not destination.parent.is_dir():
        raise BrokerPolicyError(
            f"The folder of pdf_path does not exist: {pdf_path}",
            code=WRITE_PATH_NOT_FOUND,
            fix_hint="Save into an existing folder.",
        )
    return plan, destination


def _write_pdf_locked(
    *,
    context: BrokerContext,
    request: ValidatedSaveHtmlAsPdfRequest,
    plan: PatchMountPlan,
    destination: Path,
    pdf: bytes,
) -> bool:
    """Write the PDF in place of ``destination``; True when a file was replaced."""

    root = plan.root.canonical_real_path
    try:
        lease = acquire_workspace_root_lock(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            lock_key=str(root),
            workspace_root=root,
            execution_session_id=context.execution_session.execution_session_id,
            tool_invocation_id=request.tool_invocation_id,
            action_id=context.execution_session.action_id,
            acquired_at=request.requested_at,
        )
    except WorkspaceLockConflictError as exc:
        raise BrokerExecutionError(f"PATCH_LOCK_CONFLICT: {exc}") from exc
    try:
        if destination.resolve(strict=False) != destination:
            raise BrokerPolicyError(
                "save_html_as_pdf destination changed after workspace lock acquisition",
                code=PATCH_ERROR_PATH_RETARGETED,
            )
        authorize_direct_workspace_writes(
            context=context, resolved_paths=(destination,)
        )
        parent, name = open_writable_parent_descriptor(
            root_path=root,
            relative_path=plan.path_rewrites[request.args.pdf_path],
            create_missing=False,
        )
        try:
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise BrokerPolicyError(
                    f"pdf_path is not a regular file: {request.args.pdf_path}",
                    code=WRITE_PATH_DENIED,
                )
            write_bytes_at_descriptor(
                parent_descriptor=parent,
                destination_name=name,
                data=pdf,
                replacement_mode=(
                    None if existing is None else stat.S_IMODE(existing.st_mode)
                ),
                exclusive=False,
            )
        finally:
            os.close(parent)
        return existing is not None
    finally:
        release_workspace_lock(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            lease=lease,
            released_at=now_utc_iso(),
        )


__all__ = ["build_validated_save_pdf_request", "run_save_html_as_pdf_executor"]
