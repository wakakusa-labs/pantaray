"""The Action's ``browser`` tool: the run's one page in the desktop app's browser.

Electron runs the page (``runtime.browser_relay``); this executor owns what it
is asked to do. A local file opens only if ``read`` may open it, and the page
loads local files only where ``read`` may, never Pantaray's private storage or a
file holding secrets. With the command network setting off, a call that needs
the network (a URL to open, a host to write to) is refused when it is
validated, before Electron or anyone is asked, and the broker then closes the
run's page; every other operation carries the setting, so the page reaches no
host, loopback included.
The page belongs to the calling run (its ``actor_process_id``), which the run's
end closes.

``screenshot {printed_pages}`` prints the page as ``print`` does and attaches
the pages drawn, as ``render_pdf_page`` draws a PDF.

External hosts get reads only until ``allow_writes`` names one: the approval
review, or the user, decides from the user's words, the host, the host the run
opened and the model's reason, never from page text. Once approved, the run's
pages may write to that host until the run ends, with no new approval.

Everything taken from the page goes in the output's ``data``, marked
``untrusted``, and nowhere else; storage keeps a large one's preview there too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, TypedDict

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserContactedHost,
    BrowserNote,
    BrowserOperationSucceeded,
    BrowserPageData,
    BrowserTruncation,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    BrowserOperationDone,
    BrowserRelayFailed,
    allow_browser_writes,
    browser_page_host,
    browser_write_hosts,
    close_browser_session,
    request_browser_operation,
)
from pantaray_agents.local_runtime.runtime.local_image_store import (
    write_local_image_blob,
)
from pantaray_agents.local_runtime.runtime.tool_image_attachments import (
    ToolImageAttachment,
    assemble_tool_image,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
)
from pantaray_agents.local_runtime.tooling.documents import RENDERED_PAGE_MEDIA_TYPE
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_FULL_ACCESS
from pantaray_agents.schema.websocket.server_messages import (
    BrowserFileTarget,
    BrowserNavigateOperation,
    BrowserOperation,
    BrowserPagePolicy,
    BrowserPrintOperation,
    BrowserScreenshotOperation,
    BrowserWebTarget,
    SecretPathRule,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.read_scope import ReadScope
from pantaray_agents.tools.files.read_target import resolve_read_target
from pantaray_agents.tools.files.secret_paths import secret_path_patterns

from ..repository.command_network_settings import load_command_network_enabled
from .action_path_policy import read_scope
from .broker_browser_print import save_printed_pdf
from .broker_common import (
    BrokerContext,
    ensure_session_capabilities,
    ensure_tool_authorization,
)
from .broker_direct_render_pdf import rendered_documents_dir
from .broker_failure_finalization import NetworkReachedPolicyError
from .broker_outcome import UnprojectedBrokerToolOutcome
from .browser_protocol import (
    BrowserAllowWritesArgs,
    BrowserCloseArgs,
    BrowserControlOutput,
    BrowserFailureCode,
    BrowserImageAttachment,
    BrowserNavigateArgs,
    BrowserPageOutput,
    BrowserPrintArgs,
    BrowserPrintOutput,
    BrowserScreenshotArgs,
    BrowserScreenshotOutput,
    BrowserToolArgs,
    ValidatedBrowserPrintRequest,
    ValidatedBrowserRequest,
    ValidatedBrowserWriteRequest,
)
from .printed_pages import draw_printed_pages

_WEB_SCHEMES: Final = ("http://", "https://")
_SNAPSHOT_AGAIN: Final = "Take a new snapshot and use a ref from it."
_RETRY_ONCE: Final = (
    "Try once more; if it fails again, tell the user the browser could not be "
    "used. The desktop app must be open."
)
_NAVIGATE_AGAIN: Final = "The page was discarded. Navigate again to continue."
_USER_CAN_FIX: Final = (
    "Tell the user, in their language, what the message says, and wait for them "
    "before trying their browser again."
)
FIX_HINTS: Final[dict[BrowserFailureCode, str]] = {
    "BROWSER_NO_PAGE": "Open a page with navigate first.",
    "BROWSER_REF_UNKNOWN": _SNAPSHOT_AGAIN,
    "BROWSER_REF_STALE": f"The page changed since the snapshot. {_SNAPSHOT_AGAIN}",
    "BROWSER_NAVIGATION_FAILED": (
        "Check the URL or path. For a local dev server, check with "
        "read_process_output that it is running."
    ),
    "BROWSER_TIMED_OUT": _NAVIGATE_AGAIN,
    "BROWSER_RENDERER_CRASHED": _NAVIGATE_AGAIN,
    "BROWSER_SESSION_GONE": "The page was closed. Navigate again to continue.",
    "BROWSER_ACTION_FAILED": (
        "Take a snapshot to see the page as it is now, then try another way."
    ),
    "BROWSER_USER_NOT_RUNNING": _USER_CAN_FIX,
    "BROWSER_USER_DEBUGGING_OFF": _USER_CAN_FIX,
    "BROWSER_USER_FILE_BLOCKED": _USER_CAN_FIX,
    "BROWSER_USER_DENIED": (
        "The user declined. Use their browser again only if they ask you to."
    ),
    "BROWSER_USER_UNAVAILABLE": "Tell the user their browser could not be used.",
    "BROWSER_NETWORK_OFF": (
        "The command network setting is off, which blocks every URL, localhost "
        "included. Open a local file instead, or ask the user to turn it on."
    ),
    "BROWSER_OUTSIDE_READ_SCOPE": (
        "Open a file read may open (see Workspace Path Rules), or an http(s) URL."
    ),
    "BROWSER_RELAY_TIMED_OUT": _RETRY_ONCE,
    "BROWSER_INTEGRITY_FAILED": _RETRY_ONCE,
}


class BrowserNetworkOffError(BrokerPolicyError):
    """A browser call that needs the network, refused while the setting is off.

    The page keeps the network until Electron is sent the setting, which this
    refusal never does, so whoever raises it ends with the run's page closed.
    """


def refuse_browser_network_use(
    *, context: BrokerContext, call: BrowserToolArgs
) -> None:
    """Raise :class:`BrowserNetworkOffError` for a call that needs the network
    while the user's setting is off."""

    args = call.root
    needs_network = isinstance(args, BrowserAllowWritesArgs) or (
        isinstance(args, BrowserNavigateArgs)
        and args.url.strip().startswith(_WEB_SCHEMES)
    )
    if needs_network and not _network_enabled(context):
        raise BrowserNetworkOffError(
            f"browser {args.action} was refused: the command network setting is "
            "off, so this run's page was closed",
            code="BROWSER_NETWORK_OFF",
            fix_hint=FIX_HINTS["BROWSER_NETWORK_OFF"],
        )


def build_validated_browser_write_request(
    *,
    context: BrokerContext,
    args: BrowserAllowWritesArgs,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedBrowserWriteRequest:
    """Accept ``allow_writes``: with its approval, unless the run may already
    write to the host."""

    owner_process_id = context.actor_process_id
    # Names no page text: the host the runtime opened, not what the page says.
    command_summary: dict[str, JSONValue] = {
        "summary_kind": "browser_write",
        "host": args.host,
        "page_host": browser_page_host(owner_process_id, "pantaray"),
        # The model's user-facing reason, shown as the approval question.
        "reason": args.justification,
    }
    approval_session_id, approval_source = None, None
    if args.host not in browser_write_hosts(owner_process_id, "pantaray"):
        if not preflight_only and not tool_invocation_id:
            raise BrokerPolicyError(
                "tool_invocation_id is required for browser allow_writes"
            )
        approval_session_id, approval_source = ensure_tool_authorization(
            context=context,
            tool_invocation_id=tool_invocation_id,
            tool_request_id=tool_request_id,
            command_summary=command_summary,
            requested_at=requested_at,
            require_user_prompt=True,
        )
    return ValidatedBrowserWriteRequest(
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


async def run_browser_executor(
    *,
    context: BrokerContext,
    request: ValidatedBrowserRequest
    | ValidatedBrowserPrintRequest
    | ValidatedBrowserWriteRequest,
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    args = (
        request.args.root
        if isinstance(request, ValidatedBrowserRequest)
        else request.args
    )
    if isinstance(args, BrowserCloseArgs):
        await close_browser_session(
            owner_process_id=context.actor_process_id, browser="pantaray"
        )
    elif isinstance(args, BrowserAllowWritesArgs):
        # Only an approved call, or one for a host already allowed, gets here.
        allow_browser_writes(
            owner_process_id=context.actor_process_id,
            browser="pantaray",
            host=args.host,
        )
    if isinstance(args, (BrowserCloseArgs, BrowserAllowWritesArgs)):
        return UnprojectedBrokerToolOutcome(
            status="success",
            output=_dump(BrowserControlOutput(status="ok", action=args.action)),
        )
    scope = read_scope(context)
    user_id = context.execution_session.user_id
    allow_network = _network_enabled(context)
    operation: BrowserOperation
    if isinstance(args, BrowserNavigateArgs):
        operation = BrowserNavigateOperation(
            action="navigate",
            target=_navigation_target(args.url, scope),
        )
    elif isinstance(args, BrowserPrintArgs) or (
        isinstance(args, BrowserScreenshotArgs) and args.printed_pages is not None
    ):
        operation = BrowserPrintOperation(action="print")
    elif isinstance(args, BrowserScreenshotArgs):
        operation = BrowserScreenshotOperation(
            action="screenshot", ref=args.ref, full_page=args.full_page
        )
    else:
        operation = args
    outcome = await request_browser_operation(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=user_id,
        owner_process_id=context.actor_process_id,
        browser="pantaray",
        user_browser=None,
        operation=operation,
        policy=browser_page_policy(scope, allow_network=allow_network),
    )
    if isinstance(outcome, BrowserRelayFailed):
        raise NetworkReachedPolicyError(
            f"browser {args.action} failed: {outcome.message}",
            code=outcome.code,
            fix_hint=FIX_HINTS[outcome.code],
            network=[host.model_dump(mode="json") for host in outcome.network],
        )
    if isinstance(args, BrowserPrintArgs):
        # ValidatedBrowserRequest refuses print: only an approved one gets here.
        assert isinstance(request, ValidatedBrowserPrintRequest)
        # The relay has verified the PDF a print answer claims.
        assert outcome.artifact is not None
        saved = await save_printed_pdf(
            context=context, request=request, pdf=outcome.artifact
        )
        return UnprojectedBrokerToolOutcome(
            status="success",
            output=_dump(
                BrowserPrintOutput(
                    **_page_fields(outcome.result),
                    action="print",
                    pdf_path=str(saved.path),
                    replaced=saved.replaced,
                )
            ),
            file_paths=(args.pdf_path,),
            file_reference_paths=(
                () if saved.outside_workspace else (str(saved.path),)
            ),
        )
    if not isinstance(args, BrowserScreenshotArgs):
        return UnprojectedBrokerToolOutcome(
            status="success",
            output=_dump(
                BrowserPageOutput(**_page_fields(outcome.result), action=args.action)
            ),
        )
    if args.printed_pages is not None:
        return await _printed_pages(
            context=context, requested=args.printed_pages, printed=outcome
        )
    # The relay has verified the PNG a screenshot answer claims.
    assert outcome.artifact is not None
    image = _store_image(
        user_id=user_id,
        payload=outcome.artifact,
        mime_type="image/png",
        prefix="browser",
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=_dump(
            BrowserScreenshotOutput(
                **_page_fields(outcome.result),
                action="screenshot",
                attachments=[BrowserImageAttachment.model_validate(image.output_entry)],
            )
        ),
        attachments=(image.attachment,),
    )


async def _printed_pages(
    *, context: BrokerContext, requested: list[int], printed: BrowserOperationDone
) -> UnprojectedBrokerToolOutcome:
    """Draw the requested pages of the printed PDF and attach them."""

    # The relay has verified the PDF a print answer claims.
    assert printed.artifact is not None
    converted_dir = rendered_documents_dir(context)
    converted_dir.mkdir(mode=MANAGED_DIRECTORY_MODE, exist_ok=True)
    try:
        # Named by no URL or title: what the page says goes in data alone.
        drawn, message = await draw_printed_pages(
            pdf=printed.artifact,
            name="the page",
            requested=requested,
            converted_dir=converted_dir,
        )
    except BrokerPolicyError as exc:
        # The print already reached hosts, and Electron reports them only once.
        raise NetworkReachedPolicyError(
            str(exc),
            code=exc.code,
            fix_hint=exc.fix_hint,
            network=[host.model_dump(mode="json") for host in printed.result.network],
        ) from exc
    images = [
        _store_image(
            user_id=context.execution_session.user_id,
            payload=page.payload,
            mime_type=RENDERED_PAGE_MEDIA_TYPE,
            prefix=f"page-{page.number}",
            output_fields={"page_number": page.number},
        )
        for page in drawn.pages
    ]
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=_dump(
            BrowserScreenshotOutput(
                **_page_fields(printed.result),
                action="screenshot",
                attachments=[
                    BrowserImageAttachment.model_validate(image.output_entry)
                    for image in images
                ],
                page_count=drawn.page_count,
                # The refs are in the message as well: on the string prompt
                # path, the prompt text says where each image goes.
                message=" ".join(
                    [
                        message,
                        *(
                            f"Page {page.number}: {image.ref}"
                            for page, image in zip(drawn.pages, images, strict=True)
                        ),
                    ]
                ),
            )
        ),
        attachments=tuple(image.attachment for image in images),
    )


def _store_image(
    *,
    user_id: str,
    payload: bytes,
    mime_type: str,
    prefix: str,
    output_fields: dict[str, JSONValue] | None = None,
) -> ToolImageAttachment:
    blob = write_local_image_blob(user_id=user_id, payload=payload, mime_type=mime_type)
    suffix = Path(blob.storage_path).suffix
    return assemble_tool_image(
        blob,
        display_name=lambda image_id: f"{prefix}-{image_id}{suffix}",
        output_fields=output_fields or {},
    )


def _network_enabled(context: BrokerContext) -> bool:
    return load_command_network_enabled(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=context.execution_session.user_id,
    )


def browser_page_policy(scope: ReadScope, *, allow_network: bool) -> BrowserPagePolicy:
    """What the page may load: local files only where ``read`` may, by both
    secret tiers as for ``read``, and the network by the user's setting."""

    return BrowserPagePolicy(
        allow_network=allow_network,
        file_roots=[str(root) for root in _file_roots(scope)],
        private_roots=[str(root) for root in scope.private_storage.storage_roots],
        private_root_exceptions=[
            str(root) for root in scope.private_storage.readable_roots
        ],
        secret_paths=[
            SecretPathRule(withheld=rule.withheld, excepted=rule.excepted)
            for rule in (
                *secret_path_patterns("app_secret"),
                *secret_path_patterns("cli_credential"),
            )
        ],
        # The relay sends the hosts the run's approved allow_writes named.
        write_hosts=[],
    )


def _file_roots(scope: ReadScope) -> tuple[Path, ...]:
    """The folders the page may load local files from: those read may open."""

    if scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return (Path("/"),)
    return tuple(
        root.canonical_real_path for root in scope.manifest_roots if root.can_read
    )


class _PageFields(TypedDict):
    status: Literal["ok"]
    truncated: BrowserTruncation | None
    notes: list[BrowserNote]
    network: list[BrowserContactedHost]
    untrusted: Literal[True]
    data: BrowserPageData


def _page_fields(result: BrowserOperationSucceeded) -> _PageFields:
    """What every output of a page read carries; the page's text is in ``data``."""

    return {
        "status": "ok",
        "truncated": result.truncated,
        "notes": result.notes,
        "network": result.network,
        "untrusted": True,
        "data": result.page_data,
    }


def _dump(
    output: BrowserPageOutput
    | BrowserScreenshotOutput
    | BrowserPrintOutput
    | BrowserControlOutput,
) -> JSONValue:
    # Unset, not None: ``truncated`` and the page's own nulls stay in the output,
    # and fields an action does not fill (``page_count``) stay out of it.
    return output.model_dump(mode="json", exclude_unset=True)


def _navigation_target(
    url: str, scope: ReadScope
) -> BrowserWebTarget | BrowserFileTarget:
    """An http(s) URL, the network being on, or a file ``read`` may open."""

    url = url.strip()
    if url.startswith(_WEB_SCHEMES):
        try:
            return BrowserWebTarget(kind="web", url=url)
        except ValidationError as exc:
            raise BrokerPolicyError(
                f"Not a URL the browser can open: {url}",
                code="BROWSER_NAVIGATION_FAILED",
                fix_hint=(
                    "Percent-encode spaces, backslashes and control characters "
                    "in the URL."
                ),
            ) from exc
    try:
        target = resolve_read_target(scope=scope, raw_path=url)
    except BrokerPolicyError as exc:
        raise BrokerPolicyError(
            str(exc),
            code="BROWSER_OUTSIDE_READ_SCOPE",
            fix_hint=exc.fix_hint or FIX_HINTS["BROWSER_OUTSIDE_READ_SCOPE"],
        ) from exc
    return BrowserFileTarget(kind="file", path=str(target.real_path))


__all__ = [
    "FIX_HINTS",
    "BrowserNetworkOffError",
    "browser_page_policy",
    "build_validated_browser_write_request",
    "refuse_browser_network_use",
    "run_browser_executor",
]
