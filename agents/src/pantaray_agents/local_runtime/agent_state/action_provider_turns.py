"""Read back the provider turns one Action may hand to its current connection.

A run keeps the turns it received in memory; after a restart or a follow-up it
reloads what earlier runs recorded, so the items it projects are byte for byte
the ones the provider already read. Only one account's turns come back: the
opaque part of a turn is readable by its issuer alone (migration 0116), and a
turn recorded without the fingerprint of its prefix (before migration 0122) is
never handed back, so it is not read either.
"""

from __future__ import annotations

import sqlite3

from pydantic import TypeAdapter

from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_llm.contracts.conversation import LlmProviderTurn

_PROVIDER_TURN_ADAPTER: TypeAdapter[LlmProviderTurn] = TypeAdapter(LlmProviderTurn)


def read_action_provider_turns_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    identity: str,
) -> dict[str, ActionProviderTurnRecord]:
    """The provider turn each LLM step of this Action produced, by ``step_id``."""

    rows = connection.execute(
        """
        SELECT step_id, provider_turn, provider_turn_fingerprint
        FROM agent_action_steps
        WHERE user_id = ?
          AND action_id = ?
          AND provider_turn IS NOT NULL
          AND provider_turn_identity = ?
          AND provider_turn_fingerprint IS NOT NULL
        """,
        (user_id, action_id, identity),
    ).fetchall()
    return {
        str(row["step_id"]): ActionProviderTurnRecord(
            turn=_PROVIDER_TURN_ADAPTER.validate_json(str(row["provider_turn"])),
            identity=identity,
            fingerprint=str(row["provider_turn_fingerprint"]),
        )
        for row in rows
    }


__all__ = ["read_action_provider_turns_in_connection"]
