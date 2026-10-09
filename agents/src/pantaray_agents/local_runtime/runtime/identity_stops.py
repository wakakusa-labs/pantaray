"""Work above this layer that a route identity change must stop.

The stop barrier cancels the Action runs itself. Other work the old identity
may still be running (the chat's turn) registers how to stop it here, so the
barrier calls it without knowing what it is; it runs after the Action cancels
and before the swap, in the order registered.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol


class OwnerWorkStop(Protocol):
    def __call__(self, *, owner_id: str) -> Awaitable[None]: ...


_STOPS: list[OwnerWorkStop] = []


def stop_on_identity_change(stop: OwnerWorkStop) -> None:
    _STOPS.append(stop)


async def stop_registered_work(*, owner_id: str) -> None:
    for stop in _STOPS:
        await stop(owner_id=owner_id)


__all__ = ["OwnerWorkStop", "stop_on_identity_change", "stop_registered_work"]
