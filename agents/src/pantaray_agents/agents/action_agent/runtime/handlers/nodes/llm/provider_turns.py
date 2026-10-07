"""Open one Action run's provider turns, keyed by the THINK rows that stored them."""

from __future__ import annotations

from pantaray_agents.conversation.provider_turns import (
    ProviderTurnStore,
    read_provider_turn_target,
)
from pantaray_agents.repositories.runtime_ports import ActionRepositoryPort


async def load_action_provider_turns(
    repository: ActionRepositoryPort,
    *,
    user_id: str,
    action_id: str,
    inference_profile: str,
) -> ProviderTurnStore:
    """Open a run's store, carrying over what earlier runs of it recorded."""

    identity, _ = read_provider_turn_target(inference_profile=inference_profile)
    if identity is None:
        return ProviderTurnStore(identity=None)
    result = await repository.get_action_provider_turns(
        user_id=user_id, action_id=action_id, identity=identity
    )
    if result.error or result.data is None:
        raise RuntimeError(
            f"Action provider turns could not be read: {result.error or 'no data'}"
        )
    return ProviderTurnStore(identity=identity, turns=dict(result.data))


__all__ = ["load_action_provider_turns"]
