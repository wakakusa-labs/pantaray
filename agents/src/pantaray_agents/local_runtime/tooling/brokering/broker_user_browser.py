"""``browser`` on the user's own browser (``browser: "user"``): what the broker
decides before Electron is asked.

The run's tab there is signed in to the user's real accounts, so it opens only
sites the user approved for the run, one host at a time, and never a local
file, a loopback host or a print. The browser is the one the run's tab is bound
to, or else the first known browser with remote debugging on; it is chosen as
the call is validated and the call runs with it. The host Python reads from a
URL is only what the user is asked about: Electron parses the URL as Chromium
does and refuses a host the run was not allowed to open.
"""

from __future__ import annotations

import ipaddress
from typing import Final, NoReturn
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from pantaray_agents.local_runtime.runtime.browser_relay import (
    bound_user_browser,
    user_browser_read_hosts,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.user_browser import KNOWN_USER_BROWSERS, UserBrowserId
from pantaray_agents.schema.websocket.server_messages import BrowserHost
from pantaray_agents.tools.contract import BrokerPolicyError

from ..sandbox.browser_debugging_ports import browsers_with_debugging_on
from .broker_browser import web_target
from .broker_common import BrokerContext, ensure_tool_authorization
from .browser_protocol import (
    FIX_HINTS,
    BrowserNavigateArgs,
    BrowserPrintArgs,
    BrowserScreenshotArgs,
    BrowserToolArgs,
    ValidatedUserBrowserReadRequest,
)

# urlsplit gives the host lowercase, as the user's browser compares hosts.
_HOST: Final[TypeAdapter[str]] = TypeAdapter(BrowserHost)
_DEBUGGING_OFF_MESSAGE: Final = (
    "None of the user's browsers has remote debugging on, so Pantaray cannot "
    "use it. Turn it on in Google Chrome at chrome://inspect/#remote-debugging "
    "(Microsoft Edge: edge://inspect, Brave: brave://inspect)."
)


def user_browser_for_run(owner_process_id: str) -> UserBrowserId:
    """The browser the run's tab is bound to, or else the first known one with
    remote debugging on."""

    bound = bound_user_browser(owner_process_id)
    if bound is not None:
        return bound
    debugging_on = browsers_with_debugging_on()
    if not debugging_on:
        raise BrokerPolicyError(
            _DEBUGGING_OFF_MESSAGE,
            code="BROWSER_USER_DEBUGGING_OFF",
            fix_hint=FIX_HINTS["BROWSER_USER_DEBUGGING_OFF"],
        )
    return debugging_on[0]


def refuse_printing(call: BrowserToolArgs) -> None:
    """Refuse ``print`` and ``screenshot {printed_pages}``: the user's browser
    does not print."""

    args = call.root
    if isinstance(args, BrowserPrintArgs):
        _not_offered("print")
    if isinstance(args, BrowserScreenshotArgs) and args.printed_pages is not None:
        _not_offered("screenshot printed_pages")


def _user_browser_host(url: str) -> str:
    """The lowercase host of an http(s) URL the user's browser may be asked to
    open; a local file or a loopback host is refused."""

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        _not_offered("local files")
    target = web_target(url)
    try:
        host = _HOST.validate_python(urlsplit(target.url).hostname)
    except (ValueError, ValidationError) as exc:
        raise BrokerPolicyError(
            f"Not a URL the user's browser can open: {url}",
            code="BROWSER_NAVIGATION_FAILED",
            fix_hint="Give an http(s) URL with a host name, such as https://example.com/.",
        ) from exc
    if _is_loopback(host):
        _not_offered(f"the loopback host {host}")
    return host


def build_validated_user_browser_read_request(
    *,
    context: BrokerContext,
    args: BrowserNavigateArgs,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedUserBrowserReadRequest:
    """Accept ``navigate`` in the user's browser: with its approval, unless the
    run's tab there may already open the host."""

    # Before the browser is chosen: a URL it never opens needs no browser.
    host = _user_browser_host(args.url)
    user_browser = user_browser_for_run(context.actor_process_id)
    # Names no page text: the browser, the host the model asked for and the
    # model's user-facing reason, shown as the approval question.
    command_summary: dict[str, JSONValue] = {
        "summary_kind": "user_browser_read",
        "browser": KNOWN_USER_BROWSERS[user_browser].display_name,
        "host": host,
        "reason": args.justification,
    }
    approval_session_id, approval_source = None, None
    if host not in user_browser_read_hosts(context.actor_process_id):
        if args.justification is None:
            raise BrokerPolicyError(
                f"browser navigate to {host} in the user's browser needs "
                "justification: the site has not been approved in this run",
                fix_hint=(
                    "Call navigate again with justification: one short sentence "
                    "in the language of the user's request saying what you will "
                    "read there and why."
                ),
            )
        if not preflight_only and not tool_invocation_id:
            raise BrokerPolicyError(
                "tool_invocation_id is required for browser navigate"
            )
        approval_session_id, approval_source = ensure_tool_authorization(
            context=context,
            tool_invocation_id=tool_invocation_id,
            tool_request_id=tool_request_id,
            command_summary=command_summary,
            requested_at=requested_at,
            require_user_prompt=True,
        )
    return ValidatedUserBrowserReadRequest(
        tool_invocation_id=tool_invocation_id or "",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session.execution_session_id,
        action_id=context.execution_session.action_id or "unknown",
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        command_summary_json=command_summary,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
        user_browser=user_browser,
        host=host,
        args=args,
    )


def _is_loopback(host: str) -> bool:
    host = host.rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _not_offered(what: str) -> NoReturn:
    raise BrokerPolicyError(
        f"The user's browser is not used for {what}.",
        code="BROWSER_USER_NOT_OFFERED",
        fix_hint=FIX_HINTS["BROWSER_USER_NOT_OFFERED"],
    )


__all__ = [
    "build_validated_user_browser_read_request",
    "refuse_printing",
    "user_browser_for_run",
]
