"""``capture_screen`` execution: approve first, then ask Electron to capture.

Nothing here takes a screenshot. What may be captured depends on the recording
filter and on the macOS Screen Recording permission, both of which live in Electron
main; this module owns the order the two sides act in.

1. Consent is settled in the preflight, before any request reaches Electron. Under
   ``prompt_each_time`` the preflight pauses the Action, so the user answers before
   a single pixel is read - never "capture, then ask". The execution below then
   spends that approval through the same claim the brokered tools make, so one
   answer authorizes exactly one capture even across a resume.
2. Electron applies its own gates and either writes an image or refuses. A refusal
   carries a code and never carries bytes.
3. The runtime re-reads whatever Electron says it wrote and recomputes the content
   identity. Electron's ``storage_path``, ``sha256`` and ``byte_size`` are claims,
   not facts, so a mismatch fails the tool instead of handing the model an image
   nobody verified.

The image never enters the tool output. That output is durable history the memory
tools can read; the bytes ride the file-input path as an attachment.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    LocalImageAttachment,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.capture_screen_tool import (
    CAPTURE_SCREEN_TIMEOUT_MS,
    CAPTURE_SCREEN_TOOL_ID,
)
from pantaray_agents.agents.core.llm_file_inputs import TOOL_ATTACHMENT_REF_PREFIX
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.local_image_store import (
    read_local_image_blob,
)
from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    ScreenCaptureCaptured,
    ScreenCaptureRefusalCode,
    ScreenCaptureRefused,
    announce_screen_capture_request,
    screen_capture_broker,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.brokering.attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    ATTACHMENT_ID_HEX_LENGTH,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerPolicyError,
    ensure_tool_authorization,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.brokering.execution_start import (
    claim_approved_execution_start,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.utils.local_time import describe_local_time
from pantaray_agents.utils.trace_context import get_trace_context

from .approval_preparation import (
    build_approval_denied_preparation,
    build_approval_required_preparation,
)
from .shared import (
    CompletedToolControl,
    FailedToolControl,
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
    tool_failure_identity_from_exception,
)

CAPTURE_TIMED_OUT = "CAPTURE_TIMED_OUT"
CAPTURE_INTEGRITY_FAILED = "CAPTURE_INTEGRITY_FAILED"
CAPTURE_APPROVAL_STATE_CHANGED = "CAPTURE_APPROVAL_STATE_CHANGED"

_CAPTURE_TIMEOUT_SECONDS = CAPTURE_SCREEN_TIMEOUT_MS / 1000
_CAPTURE_SUMMARY_KIND = "screen_capture"
_REFUSAL_MESSAGES: dict[ScreenCaptureRefusalCode, str] = {
    "SCREEN_RECORDING_PERMISSION_REQUIRED": (
        "macOS Screen Recording permission is not granted to Pantaray, so no "
        "image was taken. Ask the user to allow it in System Settings > Privacy "
        "& Security > Screen Recording, then try again."
    ),
    "CAPTURE_TARGET_NOT_FOUND": (
        "That app has no window on screen, so no image was taken. A minimized or "
        "hidden window, or one on another desktop, cannot be captured. "
        "details.available_apps names the apps that can be captured now."
    ),
    "CAPTURE_REFUSED_BY_PRIVACY_FILTER": (
        "The user's recording filter excludes this window, so no image was taken. "
        "Do not retry; ask the user for the information instead."
    ),
    "CAPTURE_REFUSED_PASSWORD_MANAGER": (
        "The app is a password manager. Captures there are always refused, so no "
        "image was taken. Do not retry."
    ),
    "CAPTURE_REFUSED_URL_UNAVAILABLE": (
        "The page in this browser window could not be checked against the user's "
        "website filter, so no image was taken. Only Google Chrome and Safari "
        "windows can be checked."
    ),
    "CAPTURE_REFUSED_PRIVATE_WINDOW": (
        "The window is a Chrome Incognito window. Captures there are always "
        "refused, so no image was taken. Do not retry."
    ),
    "CAPTURE_REFUSED_SENSITIVE_PAGE": (
        "The browser window is on a sign-in or payment page. Captures there are "
        "always refused, so no image was taken. Do not retry; ask the user for the "
        "information instead."
    ),
}


class CaptureScreenIdentityError(RuntimeError):
    """The Action state does not identify who is asking for a capture."""


@dataclass(frozen=True, slots=True)
class _CaptureStep:
    """The step being built, carried together so every outcome names one step."""

    step_id: str
    tool_def: ToolDefinition
    requested_at: str


@dataclass(frozen=True, slots=True)
class _CaptureIdentity:
    action_id: str
    execution_session_id: str
    manifest_id: str
    process_id: str
    user_id: str


def build_capture_screen_summary(app_name: str) -> dict[str, JSONValue]:
    """What the approval panel shows for this request: the app to be captured.

    The same name is what Electron is asked to capture, so the user approves the
    target itself. Which of its windows is frontmost, and the page it shows, are
    known only inside Electron at the moment of capture.
    """

    return {"summary_kind": _CAPTURE_SUMMARY_KIND, "app_name": app_name}


async def run_capture_screen_preflight(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    app_name: str,
    tool_request_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    """Settle consent before the Action commits to a capture.

    The returned preparation reaches history only when it blocks execution; an
    authorized capture falls through to the call itself, which asks again.
    """

    step = _CaptureStep(step_id=step_id, tool_def=tool_def, requested_at=requested_at)
    try:
        identity = _resolve_identity(state)
    except CaptureScreenIdentityError as exc:
        return _failed_preparation(step, error=exc)
    try:
        _authorize_capture(
            identity=identity,
            app_name=app_name,
            tool_request_id=tool_request_id,
            requested_at=requested_at,
        )
    except BrokerApprovalRequiredError as exc:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        try:
            return build_approval_required_preparation(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                step_id=step_id,
                tool_def=tool_def,
                tool_request_id=tool_request_id,
                approval_session_id=exc.approval_session_id,
                user_id=identity.user_id,
                manifest_id=identity.manifest_id,
                action_id=identity.action_id,
            )
        except BrokerPolicyError as missing_session:
            return _failed_preparation(step, error=missing_session)
    except BrokerApprovalDeniedError as exc:
        return build_approval_denied_preparation(
            step_id=step_id,
            tool_def=tool_def,
            tool_request_id=tool_request_id,
            approval_session_id=exc.approval_session_id,
            requested_at=requested_at,
        )
    except (BrokerPolicyError, MigrationError) as exc:
        return _failed_preparation(step, error=exc)
    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="success",
            started_at=requested_at,
            completed_at=requested_at,
            output={"status": "authorized"},
        ),
        control=CompletedToolControl(),
    )


async def run_capture_screen_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    app_name: str,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> UnprojectedToolExecutionResult:
    """Ask Electron for one screenshot and return it as a verified attachment.

    Consent is re-checked and then spent here even though the preflight just
    ran: this is the call that causes a capture, and it must not depend on a
    caller having remembered to ask first.
    """

    step = _CaptureStep(step_id=step_id, tool_def=tool_def, requested_at=requested_at)
    identity = _resolve_identity(state)
    try:
        _claim_capture_authorization(
            identity=identity,
            app_name=app_name,
            tool_request_id=tool_request_id,
            tool_invocation_id=tool_invocation_id,
            requested_at=requested_at,
        )
    except (
        BrokerApprovalRequiredError,
        BrokerApprovalDeniedError,
        BrokerPolicyError,
    ) as exc:
        # The preflight settled consent moments ago; a different answer now means
        # the approval state moved underneath the call - the user withdrew it, or
        # this is a replay of a capture whose approval was already spent. This
        # stage returns a tool result rather than a pause, so the outcome is a
        # typed failure and the Supervisor may ask again, which re-runs the
        # preflight and asks the user afresh.
        return _error_result(
            step,
            code=CAPTURE_APPROVAL_STATE_CHANGED,
            message=(
                "Consent for this capture changed before it could run, so no image "
                f"was taken ({type(exc).__name__}). Request the capture again."
            ),
        )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    broker = screen_capture_broker()
    capture_request_id = broker.open(user_id=identity.user_id)
    try:
        announce_screen_capture_request(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=identity.user_id,
            action_id=identity.action_id,
            process_id=identity.process_id,
            tool_request_id=tool_request_id,
            capture_request_id=capture_request_id,
            app_name=app_name,
        )
    except Exception:
        broker.close(capture_request_id=capture_request_id)
        raise
    outcome = await broker.wait(
        capture_request_id=capture_request_id,
        timeout_seconds=_CAPTURE_TIMEOUT_SECONDS,
    )

    if outcome is None:
        return _error_result(
            step,
            code=CAPTURE_TIMED_OUT,
            message=(
                "The desktop app did not answer the capture request in "
                f"{int(_CAPTURE_TIMEOUT_SECONDS)}s. No image was taken."
            ),
        )
    if isinstance(outcome, ScreenCaptureRefused):
        return _error_result(
            step,
            code=outcome.code,
            message=_REFUSAL_MESSAGES[outcome.code],
            details=outcome.details,
        )
    return _captured_result(step, user_id=identity.user_id, captured=outcome)


def _claim_capture_authorization(
    *,
    identity: _CaptureIdentity,
    app_name: str,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> None:
    """Authorize this capture and spend the approval that allows it.

    The claim is the same one the brokered tools make at execution start, so an
    approved capture runs exactly once: the session is bound to this invocation
    and marked consumed in one transaction, and a replay after a resume or a
    crash is refused rather than taking a second screenshot.
    """

    approval_session_id, approval_source = _authorize_capture(
        identity=identity,
        app_name=app_name,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
    )
    if approval_source is None:
        # The session already carries the capability, so no approval was needed
        # and there is nothing to spend.
        return
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    claim_approved_execution_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        tool_request_id=tool_request_id,
        action_id=identity.action_id,
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        tool_invocation_id=tool_invocation_id,
        # The Action runtime records this tool's invocation row before execution,
        # so the claim binds to that row instead of writing a second one.
        invocation=None,
        started_at=requested_at,
    )


def _authorize_capture(
    *,
    identity: _CaptureIdentity,
    app_name: str,
    tool_request_id: str,
    requested_at: str,
) -> tuple[str | None, Literal["settings", "prompt"] | None]:
    """Return how this capture is authorized, or raise if it may not run now.

    Raises the broker's own approval errors, so the Action pauses, denies, or
    fails exactly as it does for any other consent-gated tool.
    """

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return ensure_tool_authorization(
        context=load_broker_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=CAPTURE_SCREEN_TOOL_ID,
            path_access_kind="none",
            user_id=identity.user_id,
            actor_process_id=identity.process_id,
            manifest_id=identity.manifest_id,
            execution_session_id=identity.execution_session_id,
        ),
        tool_invocation_id=None,
        tool_request_id=tool_request_id,
        command_summary=build_capture_screen_summary(app_name),
        requested_at=requested_at,
    )


def _resolve_identity(state) -> _CaptureIdentity:
    trace = get_trace_context()
    raw_process_id = trace.extra.get("process_id") if trace is not None else None
    values: dict[str, object] = {
        "action_id": state.get("action_id"),
        "execution_session_id": state.get("execution_session_id"),
        "manifest_id": state.get("manifest_id"),
        "process_id": raw_process_id,
        "user_id": state.get("user_id"),
    }
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise CaptureScreenIdentityError(f"{name} is required for capture_screen")
    return _CaptureIdentity(
        action_id=str(values["action_id"]),
        execution_session_id=str(values["execution_session_id"]),
        manifest_id=str(values["manifest_id"]),
        process_id=str(values["process_id"]),
        user_id=str(values["user_id"]),
    )


def _captured_result(
    step: _CaptureStep,
    *,
    user_id: str,
    captured: ScreenCaptureCaptured,
) -> UnprojectedToolExecutionResult:
    try:
        blob = read_local_image_blob(
            user_id=user_id,
            storage_path=captured.storage_path,
        )
    except (MigrationError, ValueError):
        blob = None
    if blob is None:
        return _error_result(
            step,
            code=CAPTURE_INTEGRITY_FAILED,
            message="The captured image could not be read back. None is available.",
        )
    sha256 = hashlib.sha256(blob.payload).hexdigest()
    if (
        sha256 != captured.sha256
        or len(blob.payload) != captured.byte_size
        or blob.mime_type != captured.mime_type
    ):
        return _error_result(
            step,
            code=CAPTURE_INTEGRITY_FAILED,
            message=(
                "The captured image on disk does not match what the desktop app "
                "reported. No image is available."
            ),
        )
    attachment_id = sha256[:ATTACHMENT_ID_HEX_LENGTH]
    ref = f"{TOOL_ATTACHMENT_REF_PREFIX}{attachment_id}"
    display_path = f"screen-{attachment_id}.png"
    attachment: LocalImageAttachment = {
        "type": "file",
        "ref": ref,
        "blob_ref": f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
        "display_path": display_path,
        "mime_type": blob.mime_type,
        "byte_size": len(blob.payload),
        "sha256": sha256,
        "source_kind": "local_image_blob",
        "storage_path": blob.storage_path,
    }
    captured_at = describe_local_time(captured.captured_at, None)
    output: dict[str, JSONValue] = {
        "status": "captured",
        "app_name": captured.app_name,
        "captured_at": captured_at,
        "width_px": captured.width_px,
        "height_px": captured.height_px,
        "message": (
            f"Captured the window of {captured.app_name} at {captured_at}. {ref}"
        ),
        # The durable step row keeps only this output, so the conversation read model
        # can name the image only if the output names it. The logical storage_path is
        # user-scoped metadata, never an absolute path and never the bytes.
        "attachments": [
            {
                "type": "file",
                "source_kind": "local_image_blob",
                "mime_type": blob.mime_type,
                "path": display_path,
                "storage_path": blob.storage_path,
                "ref": ref,
                "byte_size": len(blob.payload),
            }
        ],
    }
    return UnprojectedToolExecutionResult(
        step_id=step.step_id,
        tool_id=step.tool_def.tool_id,
        status="success",
        started_at=step.requested_at,
        completed_at=now_utc_iso(),
        output=output,
        attachments=[attachment],
    )


def _error_result(
    step: _CaptureStep,
    *,
    code: str,
    message: str,
    details: dict[str, JSONValue] | None = None,
) -> UnprojectedToolExecutionResult:
    return UnprojectedToolExecutionResult(
        step_id=step.step_id,
        tool_id=step.tool_def.tool_id,
        status="error",
        started_at=step.requested_at,
        completed_at=now_utc_iso(),
        output=build_runtime_tool_error_output(
            error_type=code,
            message=message,
            details=details,
        ),
    )


def _failed_preparation(
    step: _CaptureStep, *, error: Exception
) -> ToolExecutionPreparation:
    return ToolExecutionPreparation(
        result=_error_result(
            step,
            code=error.__class__.__name__,
            message=str(error),
        ),
        control=FailedToolControl(failure=tool_failure_identity_from_exception(error)),
    )


__all__ = [
    "CAPTURE_INTEGRITY_FAILED",
    "CAPTURE_TIMED_OUT",
    "CaptureScreenIdentityError",
    "build_capture_screen_summary",
    "run_capture_screen_preflight",
    "run_capture_screen_tool",
]
