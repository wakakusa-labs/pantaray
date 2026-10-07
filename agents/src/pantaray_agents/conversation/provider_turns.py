"""The provider turns one run may hand back, and the account that may read them.

OpenAI asks callers to send a reasoning item back on the next turn of a tool-use
chain. The turn arrives beside the response, is kept here by the id of what
produced it, and the caller's projection places it back on that assistant item
when it still stands behind the prefix it was produced behind (``prefix.py``).

Whose account and model issued a turn decides whether it can go back at all,
and that lives here rather than in a projection, which stays a pure function of
the history it is given: a stop barrier protects the account, while a model may
change during a run. What outlives the run -- the record each accepted turn
returns -- is the caller's to store.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from pantaray_agents.local_runtime.runtime.connection_store import (
    LlmConnection,
    peek_llm_connection,
    read_llm_route,
)
from pantaray_agents.local_runtime.runtime.route_identity import connection_identity
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_llm.contracts.conversation import (
    LlmConversation,
    LlmProviderTurn,
    LlmTurnAssistantItem,
)
from pantaray_llm.errors import PROXY_INVALID_INPUT, LlmProxyExecutionError

logger = logging.getLogger(__name__)


def read_provider_turn_target(
    *, inference_profile: str
) -> tuple[str | None, LlmConnection | None]:
    """Identity and direct connection for one prepared turn.

    ``None`` on the unconfigured route, which reaches no provider at all.

    On the cloud route the account is the operator's and the desktop never sees
    it, so there is nothing of it to name. What the desktop does name is the
    route and the profile it asks for, which is as far as it can know the model:
    the Cloud resolves one profile to one model. A profile the operator moves to
    another model, or an organization they replace, is not visible here and is
    answered by the one refused replay ``send_dropping_refused_turns`` allows.

    No identity carries a secret. An API key enters only as the fingerprint
    ``route_identity`` already derives for the stop barrier, and a route's own
    word leads the value, so the two routes cannot collide: a run that resumes
    on the other route reads back none of the turns it recorded here.
    """
    route = read_llm_route()
    if route == "cloud":
        return f"cloud:{inference_profile}", None
    connection = peek_llm_connection()
    if route != "direct" or connection is None:
        return None, None
    identity = connection_identity(connection)
    return (
        ":".join(
            (identity.kind, identity.provider, connection.model, identity.fingerprint)
        ),
        connection,
    )


@dataclass(slots=True)
class ProviderTurnStore:
    """One run's provider turns, keyed by the id of the item that produced each."""

    identity: str | None
    turns: dict[str, ActionProviderTurnRecord] = field(default_factory=dict)

    def use_identity(self, identity: str | None) -> None:
        """Discard opaque turns when the next request selects another model."""
        if identity != self.identity:
            self.identity = identity
            self.turns.clear()

    def accept(
        self,
        *,
        step_id: str,
        turn: LlmProviderTurn | None,
        fingerprint: str | None,
    ) -> ActionProviderTurnRecord | None:
        """Keep this turn, and return what the caller stores for it.

        ``None`` when there is nothing to hand back: a route that reaches no
        provider is answered with no turn, and a turn sent as one string has no
        conversation prefix to go back behind.
        """
        if turn is None or self.identity is None or fingerprint is None:
            return None
        record = ActionProviderTurnRecord(
            turn=turn, identity=self.identity, fingerprint=fingerprint
        )
        self.turns[step_id] = record
        return record


class SentWindow(Protocol):
    @property
    def conversation(self) -> LlmConversation | None: ...


async def send_dropping_refused_turns[W: SentWindow, R](
    send: Callable[[W], Awaitable[R]],
    *,
    prepared: W,
    prepare: Callable[[], W],
    store: ProviderTurnStore,
) -> tuple[R, W]:
    """Send ``prepared``; on a refused input that carried turns, resend once without.

    A prefix match is everything the desktop can check. What it cannot is who
    the provider now is behind a route: on the cloud route the operator may
    move a profile to another model or organization, and an
    ``encrypted_content`` or a signature from the old one is refused. So when
    the provider refuses an input that carried turns, the store is emptied and
    ``prepare`` rebuilds the same request without them, dropping nothing else:
    a request refused for anything else in its input is refused again and
    raised exactly as it was. Turns produced after that go back as usual.

    Returns the response and the window that produced it, whose fingerprint the
    response's own provider turn is recorded with.
    """

    try:
        return await send(prepared), prepared
    except LlmProxyExecutionError as exc:
        # Only a request that carried one is resent, so a refusal about
        # anything else in the input is raised exactly where it was.
        if exc.error_code != PROXY_INVALID_INPUT or not any(
            isinstance(item, LlmTurnAssistantItem) and item.provider_turn is not None
            for item in prepared.conversation or ()
        ):
            raise
        store.turns.clear()
        logger.warning(
            "A conversation discarded this run's provider turns: the provider "
            "refused the input (%s). Resending without them, which also drops "
            "this run's prompt cache read for one turn.",
            exc.upstream_code or exc.error_code,
        )
    window = prepare()
    return await send(window), window


__all__ = [
    "ProviderTurnStore",
    "SentWindow",
    "read_provider_turn_target",
    "send_dropping_refused_turns",
]
