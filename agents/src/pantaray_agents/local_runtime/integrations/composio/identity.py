"""The connected user's own ID in services that name people only by ID.

Slack results say "U…" for every person, so the model needs the ID that is the
user's. Composio's connected account does not hold it, so it is asked of the
service after a successful read call, when the session already holds the
toolkit; never after a write, whose result must not wait on another request.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Final

from pantaray_agents.schema.agent.base import JSONValue

from ..contracts import ToolFailed, ToolkitSlug, ToolOutcome, ToolSucceeded
from .client import ComposioClientClosedError, ComposioResponse
from .mapping import execute_outcome

logger = logging.getLogger(__name__)

# Per toolkit: the no-input read tool that returns the connected user's own ID
# (Slack's auth.test, which needs no scope; Composio passes Slack's body through
# unchanged as data).
_IDENTITY_TOOLS: Final[dict[ToolkitSlug, str]] = {"slack": "SLACK_TEST_AUTH"}

type Send = Callable[[JSONValue], Awaitable[ComposioResponse | ToolFailed]]


class AccountIdentities:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Reconnecting makes a new account, so a found ID never goes stale.
        self._found: dict[str, str] = {}
        # In flight; a failed or cancelled lookup is asked again by a later call.
        self._looking_up: set[str] = set()

    async def user_id(
        self, *, account: str, toolkit: ToolkitSlug, send: Send
    ) -> str | None:
        """The account's own ID, asked at most once at a time until one lookup
        succeeds; None while unknown. Never raises for a failed lookup."""

        slug = _IDENTITY_TOOLS.get(toolkit)
        if slug is None:
            return None
        with self._lock:
            if account in self._found or account in self._looking_up:
                return self._found.get(account)
            self._looking_up.add(account)
        user_id = None
        try:
            user_id = await _look_up(slug, account, send)
            return user_id
        finally:
            # Also on cancellation, so the next call can ask again.
            with self._lock:
                self._looking_up.discard(account)
                if user_id is not None:
                    self._found[account] = user_id


async def _look_up(slug: str, account: str, send: Send) -> str | None:
    try:
        reply = await send({"tool_slug": slug, "arguments": {}, "account": account})
    except ComposioClientClosedError:
        # The key went away after the call itself had run.
        return None
    outcome: ToolOutcome | ComposioResponse = reply
    if isinstance(reply, ComposioResponse) and reply.status == 200:
        outcome = execute_outcome(reply.body, is_write=False)
    user_id: JSONValue = None
    if isinstance(outcome, ToolSucceeded) and isinstance(outcome.data, dict):
        user_id = outcome.data.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id
    status = reply.status if isinstance(reply, ComposioResponse) else None
    kind = outcome.error.kind if isinstance(outcome, ToolFailed) else None
    # Never the reply itself: it carries the user's name and workspace.
    logger.warning(
        "Composio account identity unavailable",
        extra={"slug": slug, "status": status, "kind": kind},
    )
    return None
