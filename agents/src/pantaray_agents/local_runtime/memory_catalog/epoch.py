from __future__ import annotations

import secrets
import uuid
from dataclasses import replace

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .errors import MemoryContextExpiredError, MemoryReferenceDepthError
from .models import (
    MemoryContextEpoch,
    MemoryContextItem,
    MemoryFragment,
    MemoryNode,
    MemoryReferenceDepth,
    MemorySource,
    ResolvedContextItem,
)

MAX_MEMORY_REFERENCE_DEPTH = 1


def build_memory_context_epoch(
    *,
    run_id: str,
    user_id: str,
    visible: tuple[tuple[MemoryNode, MemoryFragment, str], ...],
) -> MemoryContextEpoch:
    if not run_id.strip() or not user_id.strip():
        raise ValueError("run_id and user_id must not be empty")
    observed_at = now_utc_iso()
    items: list[ResolvedContextItem] = []
    for node, fragment, label in visible:
        if node.user_id != user_id or fragment.user_id != user_id:
            raise ValueError("context fragments must belong to the epoch user")
        if node.lifecycle != "active" or node.integrity != "healthy":
            raise ValueError("only active healthy memory can enter a context epoch")
        if fragment.revision_id != node.current_revision_id:
            raise ValueError("only a node current revision can enter a context epoch")
        handle = f"ctx_{secrets.token_urlsafe(24)}"
        prompt_item = MemoryContextItem(
            context_handle=handle,
            source=node.source,
            label=label,
            source_path=fragment.source_path,
            heading_path=fragment.heading_path,
            content=fragment.content_text,
            observed_at=observed_at,
        )
        items.append(
            ResolvedContextItem(
                item=prompt_item,
                user_id=user_id,
                fragment_id=fragment.fragment_id,
                revision_id=fragment.revision_id,
                node_id=node.node_id,
                reference_depth=0,
            )
        )
    return MemoryContextEpoch(
        epoch_id=f"epoch_{uuid.uuid4().hex}",
        run_id=run_id,
        user_id=user_id,
        items=tuple(items),
    )


def resolve_context_handle(
    *, epoch: MemoryContextEpoch, user_id: str, context_handle: str
) -> ResolvedContextItem:
    if epoch.user_id != user_id:
        raise MemoryContextExpiredError("context epoch belongs to another user")
    matches = [
        item for item in epoch.items if item.item.context_handle == context_handle
    ]
    if len(matches) != 1:
        raise MemoryContextExpiredError("context handle is absent or expired")
    return matches[0]


def require_reference_source(
    *, epoch: MemoryContextEpoch, context_handle: str
) -> ResolvedContextItem:
    source = resolve_context_handle(
        epoch=epoch,
        user_id=epoch.user_id,
        context_handle=context_handle,
    )
    if source.reference_depth >= MAX_MEMORY_REFERENCE_DEPTH:
        raise MemoryReferenceDepthError(
            "references returned by get_memory_reference cannot be traversed again"
        )
    return source


def append_memory_context_item(
    *,
    epoch: MemoryContextEpoch,
    source: MemorySource,
    label: str,
    source_path: str,
    heading_path: str | None,
    content: str,
    fragment_id: str,
    revision_id: str,
    node_id: str,
    reference_depth: MemoryReferenceDepth,
) -> tuple[MemoryContextEpoch, ResolvedContextItem]:
    existing = next(
        (item for item in epoch.items if item.fragment_id == fragment_id), None
    )
    item = ResolvedContextItem(
        item=MemoryContextItem(
            context_handle=f"ctx_{secrets.token_urlsafe(24)}",
            source=source,
            label=label,
            source_path=source_path,
            heading_path=heading_path,
            content=content,
            observed_at=now_utc_iso(),
        ),
        user_id=epoch.user_id,
        fragment_id=fragment_id,
        revision_id=revision_id,
        node_id=node_id,
        reference_depth=reference_depth,
    )
    if existing is not None:
        item = _refresh_context_item(existing, item)
        return replace(
            epoch,
            items=tuple(
                item if prior.fragment_id == fragment_id else prior
                for prior in epoch.items
            ),
        ), item
    return (
        MemoryContextEpoch(
            epoch_id=epoch.epoch_id,
            run_id=epoch.run_id,
            user_id=epoch.user_id,
            items=(*epoch.items, item),
        ),
        item,
    )


def merge_memory_context_epochs(
    *, base: MemoryContextEpoch, added: MemoryContextEpoch
) -> MemoryContextEpoch:
    if (base.run_id, base.user_id) != (added.run_id, added.user_id):
        raise ValueError("memory context epochs belong to different runs")
    items = list(base.items)
    positions = {item.fragment_id: index for index, item in enumerate(items)}
    for added_item in added.items:
        position = positions.get(added_item.fragment_id)
        if position is None:
            positions[added_item.fragment_id] = len(items)
            items.append(added_item)
            continue
        existing = items[position]
        items[position] = _refresh_context_item(existing, added_item)
    return MemoryContextEpoch(
        epoch_id=base.epoch_id,
        run_id=base.run_id,
        user_id=base.user_id,
        items=tuple(items),
    )


def _refresh_context_item(
    existing: ResolvedContextItem, current: ResolvedContextItem
) -> ResolvedContextItem:
    return replace(
        current,
        item=replace(current.item, context_handle=existing.item.context_handle),
        reference_depth=min(existing.reference_depth, current.reference_depth),
    )
