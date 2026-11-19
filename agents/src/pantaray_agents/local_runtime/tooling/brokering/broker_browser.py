"""The Action's ``browser`` tool: the run's one page in the desktop app's browser.

Electron runs the page (``runtime.browser_relay``); this executor owns what it
is asked to do. A local file opens only if ``read`` may open it, and the page
loads local files only where ``read`` may, never Pantaray's private storage or a
file holding secrets. With the command network setting off, a URL is refused
here, before Electron is asked, and the run's page is closed; every other
operation carries the setting, so the page reaches no host, loopback included.
The page belongs to the calling run (its ``actor_process_id``), which the run's
end closes.

Everything taken from the page goes in the output's ``data``, marked
``untrusted``, and nowhere else; storage keeps a large one's preview there too.
"""

from __future__ import annotations

from typing import Final

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserContactedHost,
    BrowserOperationSucceeded,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    BrowserRelayFailed,
    close_browser_session,
    request_browser_operation,
)
from pantaray_agents.local_runtime.runtime.local_image_store import (
    write_local_image_blob,
)
from pantaray_agents.local_runtime.runtime.tool_image_attachments import (
    assemble_tool_image,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.websocket.server_messages import (
    BrowserFileTarget,
    BrowserNavigateOperation,
    BrowserOperation,
    BrowserPagePolicy,
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
from .broker_common import BrokerContext, ensure_session_capabilities
from .broker_direct_render_html import _file_roots
from .broker_failure_finalization import NetworkReachedPolicyError
from .broker_outcome import UnprojectedBrokerToolOutcome
from .browser_protocol import (
    BrowserAllowWritesArgs,
    BrowserCloseArgs,
    BrowserFailureCode,
    BrowserNavigateArgs,
    BrowserPrintArgs,
    BrowserScreenshotArgs,
    ValidatedBrowserRequest,
)

BROWSER_ACTION_UNAVAILABLE: Final = "BROWSER_ACTION_UNAVAILABLE"
_WEB_SCHEMES: Final = ("http://", "https://")
_SNAPSHOT_AGAIN: Final = "Take a new snapshot and use a ref from it."
_RETRY_ONCE: Final = (
    "Try once more; if it fails again, tell the user the browser could not be "
    "used. The desktop app must be open."
)
_NAVIGATE_AGAIN: Final = "The page was discarded. Navigate again to continue."
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


async def run_browser_executor(
    *, context: BrokerContext, request: ValidatedBrowserRequest
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    args = request.args.root
    if isinstance(args, BrowserCloseArgs):
        await close_browser_session(owner_process_id=context.actor_process_id)
        return UnprojectedBrokerToolOutcome(
            status="success", output={"status": "ok", "action": "close"}
        )
    # Parsed by the contract's full union; these come with later releases.
    if isinstance(args, (BrowserPrintArgs, BrowserAllowWritesArgs)) or (
        isinstance(args, BrowserScreenshotArgs) and args.printed_pages is not None
    ):
        raise BrokerPolicyError(
            f"browser {args.action} is not available",
            code=BROWSER_ACTION_UNAVAILABLE,
        )
    scope = read_scope(context)
    user_id = context.execution_session.user_id
    allow_network = load_command_network_enabled(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=user_id,
    )
    operation: BrowserOperation
    if isinstance(args, BrowserNavigateArgs):
        if args.url.strip().startswith(_WEB_SCHEMES) and not allow_network:
            # A page opened while the network was on keeps it until Electron is
            # sent the setting, which this refusal never does: the page ends.
            await close_browser_session(owner_process_id=context.actor_process_id)
            raise BrokerPolicyError(
                f"{args.url.strip()} was not opened: the command network setting "
                "is off, so this run's page was closed",
                code="BROWSER_NETWORK_OFF",
                fix_hint=FIX_HINTS["BROWSER_NETWORK_OFF"],
            )
        operation = BrowserNavigateOperation(
            action="navigate",
            target=_navigation_target(args.url, scope),
        )
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
        action_id=request.action_id,
        owner_process_id=context.actor_process_id,
        tool_request_id=request.tool_request_id,
        operation=operation,
        policy=browser_page_policy(scope, allow_network=allow_network),
    )
    if isinstance(outcome, BrowserRelayFailed):
        raise NetworkReachedPolicyError(
            f"browser {args.action} failed: {outcome.message}",
            code=outcome.code,
            fix_hint=FIX_HINTS[outcome.code],
            network=_network(outcome.network),
        )
    if outcome.artifact is None:
        return UnprojectedBrokerToolOutcome(
            status="success", output=_page_output(args.action, outcome.result)
        )
    # The relay has verified the PNG a screenshot answer claims.
    blob = write_local_image_blob(
        user_id=user_id, payload=outcome.artifact, mime_type="image/png"
    )
    image = assemble_tool_image(
        blob, display_name=lambda image_id: f"browser-{image_id}.png"
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=_page_output(
            args.action, outcome.result, attachments=[image.output_entry]
        ),
        attachments=(image.attachment,),
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
        # Writes to external hosts need an approval no release asks for yet.
        write_hosts=[],
    )


def _page_output(
    action: str,
    result: BrowserOperationSucceeded,
    *,
    attachments: list[JSONValue] | None = None,
) -> dict[str, JSONValue]:
    return {
        "status": "ok",
        "action": action,
        "truncated": result.truncated,
        "notes": list(result.notes),
        "network": _network(result.network),
        "untrusted": True,
        "data": result.page_data.model_dump(mode="json"),
        **({} if attachments is None else {"attachments": attachments}),
    }


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
                fix_hint="Percent-encode spaces and other blanks in the URL.",
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


def _network(
    hosts: list[BrowserContactedHost] | tuple[BrowserContactedHost, ...],
) -> list[JSONValue]:
    """The hosts the page reached, in the ``network`` shape every tool that lets
    data leave the machine reports and the conversation row shows."""

    return [{"host": host.host, "port": host.port} for host in hosts]


__all__ = [
    "BROWSER_ACTION_UNAVAILABLE",
    "FIX_HINTS",
    "browser_page_policy",
    "run_browser_executor",
]
