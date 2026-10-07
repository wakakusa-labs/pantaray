"""The provider turns one run may hand back, and the account that may read them.

OpenAI asks callers to send a reasoning item back on the next turn of a tool-use
chain. The turn arrives beside the Action turn, is recorded on the THINK row that
produced it, and is handed to the projection, which places it on that THINK's
assistant item.

Whose account and model issued a turn decides whether it can go back at all,
and that lives here rather than in the projection, which stays a pure function
of the history it is given: the stop barrier protects the account, while a
model may change during a run. Where a turn may go -- only behind the prefix it
was produced behind -- is the projection's to decide, from the fingerprint each
turn is recorded with.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pantaray_agents.local_runtime.runtime.connection_store import (
    LlmConnection,
    peek_llm_connection,
    read_llm_route,
)
from pantaray_agents.local_runtime.runtime.route_identity import connection_identity
from pantaray_agents.repositories.runtime_ports import ActionRepositoryPort
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_llm.contracts.conversation import LlmProviderTurn


def read_provider_turn_target(
    *, inference_profile: str
) -> tuple[str | None, LlmConnection | None]:
    """Identity and direct connection for one prepared Action turn.

    ``None`` on the unconfigured route, which reaches no provider at all.

    On the cloud route the account is the operator's and the desktop never sees
    it, so there is nothing of it to name. What the desktop does name is the
    route and the profile it asks for, which is as far as it can know the model:
    the Cloud resolves one profile to one model. A profile the operator moves to
    another model, or an organization they replace, is not visible here and is
    answered by the one refused replay the send path already allows.

    No identity carries a secret. An API key enters only as the fingerprint
    ``route_identity`` already derives for the stop barrier, and a route's own
    word leads the value, so the two routes cannot collide: an Action that
    resumes on the other route reads back none of the turns it recorded here.
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
class ActionProviderTurnStore:
    """One run's provider turns, keyed by the ``step_id`` of the THINK row."""

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
        """Keep this THINK's turn, and return what its step row stores.

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


async def load_action_provider_turns(
    repository: ActionRepositoryPort,
    *,
    user_id: str,
    action_id: str,
    inference_profile: str,
) -> ActionProviderTurnStore:
    """Open a run's store, carrying over what earlier runs of it recorded."""

    identity, _ = read_provider_turn_target(inference_profile=inference_profile)
    if identity is None:
        return ActionProviderTurnStore(identity=None)
    result = await repository.get_action_provider_turns(
        user_id=user_id, action_id=action_id, identity=identity
    )
    if result.error or result.data is None:
        raise RuntimeError(
            f"Action provider turns could not be read: {result.error or 'no data'}"
        )
    return ActionProviderTurnStore(identity=identity, turns=dict(result.data))


__all__ = [
    "ActionProviderTurnStore",
    "load_action_provider_turns",
    "read_provider_turn_target",
]
