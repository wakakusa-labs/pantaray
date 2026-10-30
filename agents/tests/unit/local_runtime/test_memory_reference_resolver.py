from __future__ import annotations

from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryReferenceInputError,
    MemoryReferenceNotFoundError,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemoryContextEpoch
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    _ReferenceSource,
    follow_memory_reference,
)

_RESOLVER = "pantaray_agents.local_runtime.memory_catalog.resolver."


def _install_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _RESOLVER + "_visible_reference_source",
        lambda **_kwargs: _ReferenceSource(
            node_id="node-source", revision_id="revision-source"
        ),
    )


def _raise_not_found(**_kwargs: object) -> NoReturn:
    raise MemoryReferenceNotFoundError("ref mapping is missing")


def test_broken_reference_enqueues_source_revision_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.in_transaction = False
    repairs: list[dict[str, object]] = []
    _install_source(monkeypatch)
    monkeypatch.setattr(_RESOLVER + "_resolve_memory_reference", _raise_not_found)
    monkeypatch.setattr(
        _RESOLVER + "enqueue_memory_repair", lambda **kwargs: repairs.append(kwargs)
    )

    with pytest.raises(MemoryReferenceNotFoundError):
        follow_memory_reference(
            connection=connection,
            epoch=_epoch(),
            local_ref_id="ref_1",
            pinned_revisions=None,
            enqueue_repair_on_failure=True,
        )

    assert repairs == [
        {
            "connection": connection,
            "user_id": "user-1",
            "node_id": "node-source",
            "detected_revision_id": "revision-source",
            "reason": "reference_resolution",
        }
    ]
    connection.commit.assert_called_once_with()


def test_ref_outside_visible_memory_does_not_enqueue_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.in_transaction = False
    enqueue = MagicMock()

    def raise_invalid_input(**_kwargs: object) -> NoReturn:
        raise MemoryReferenceInputError("ref_unknown is not a ref in current memory")

    monkeypatch.setattr(_RESOLVER + "_visible_reference_source", raise_invalid_input)
    monkeypatch.setattr(_RESOLVER + "enqueue_memory_repair", enqueue)

    with pytest.raises(MemoryReferenceInputError):
        follow_memory_reference(
            connection=connection,
            epoch=_epoch(),
            local_ref_id="ref_unknown",
            pinned_revisions=None,
            enqueue_repair_on_failure=True,
        )

    enqueue.assert_not_called()
    connection.commit.assert_not_called()


def test_read_only_resolution_does_not_enqueue_broken_reference_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.in_transaction = False
    enqueue = MagicMock()
    _install_source(monkeypatch)
    monkeypatch.setattr(_RESOLVER + "_resolve_memory_reference", _raise_not_found)
    monkeypatch.setattr(_RESOLVER + "enqueue_memory_repair", enqueue)

    with pytest.raises(MemoryReferenceNotFoundError):
        follow_memory_reference(
            connection=connection,
            epoch=_epoch(),
            local_ref_id="ref_1",
            pinned_revisions=None,
            enqueue_repair_on_failure=False,
        )

    enqueue.assert_not_called()
    connection.commit.assert_not_called()


def _epoch() -> MemoryContextEpoch:
    return MemoryContextEpoch(
        epoch_id="epoch-1", run_id="run-1", user_id="user-1", items=()
    )
