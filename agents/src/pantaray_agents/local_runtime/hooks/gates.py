"""The named gates a hook can require, each a check of current runtime state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path

from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.admission import admission_is_open
from pantaray_agents.local_runtime.runtime.session_store import read_configured

from .builtin import GATE_NAMES


def owner_is_settled() -> bool:
    """Whether the worker's owner id already names the owner the work belongs to.

    Until Electron main applies ``configure`` the helper has not been told about
    the cloud session, so ``current_owner_id`` still answers the logged-out
    owner; while an identity change is at the barrier that owner is being
    replaced. Work created in either state is bound to an owner the worker stops
    claiming for milliseconds later -- ``claim_next_pending_job`` selects by
    ``user_id`` and nothing reclaims another owner's queue -- so the job stays
    queued for good while what it recorded is spent all the same: the summary
    cursor advanced past the window, the memory trigger marked dispatched. This
    is the enqueue side of the gate ``claimable_specs`` already holds on the
    claim side (design 6.2).
    """
    return admission_is_open() and read_configured()


async def _recording_is_ready(
    *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> bool:
    """Whether the user records with a current source.

    Paused capture records nothing new, and the short Insight is the only
    Suggestion trigger (`suggestion_from_insight`), so holding it is what stops
    suggestions while recording is off: the tail recorded before the user
    turned it off must not become a suggestion after.
    """
    async with context_source_control.gate.turn():
        if context_source_control.gate.current(user_id) is None:
            return False
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        source = store.get_source(connection, user_id)
    return source is not None and source.kind == "ready" and not source.capture_paused


def build_gates(
    *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> Mapping[str, Callable[[], bool]]:
    gates: dict[str, Callable[[], bool]] = {
        "owner_settled": owner_is_settled,
        "recording_ready": lambda: asyncio.run(
            _recording_is_ready(
                db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
            )
        ),
    }
    assert gates.keys() == GATE_NAMES
    return gates
