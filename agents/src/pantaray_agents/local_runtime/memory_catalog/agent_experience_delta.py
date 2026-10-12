"""Semantic shape of one Agent Experience tree change.

A unified Memory run may record several independent lessons, so a change is a
set of added, modified and removed entries rather than a single operation. The
host owns the index file; entries are the agent's.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
    AgentExperienceContent,
    experience_id_from_path,
    parse_agent_experience_markdown,
    render_agent_experience_index,
)
from .errors import MemoryCatalogIntegrityError
from .models import MemoryDocument


@dataclass(frozen=True, slots=True)
class AgentExperienceTreeChange:
    added: tuple[str, ...]
    modified: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)


def parse_agent_experience_tree(
    documents: tuple[MemoryDocument, ...],
) -> tuple[AgentExperienceContent, ...]:
    paths = _documents_by_path(documents)
    _validate_tree_paths(paths)
    entries = _parse_entries(documents)
    expected_index = render_agent_experience_index(entries)
    if paths[AGENT_EXPERIENCE_INDEX_PATH] != expected_index:
        raise MemoryCatalogIntegrityError("Agent Experience index is inconsistent")
    return entries


def rebuild_agent_experience_index(
    documents: tuple[MemoryDocument, ...],
) -> tuple[MemoryDocument, ...]:
    """Regenerate the host-owned index from the entry files the agent edited."""

    _validate_tree_paths(_documents_by_path(documents))
    index = render_agent_experience_index(_parse_entries(documents))
    return tuple(
        MemoryDocument(item.source_path, index)
        if item.source_path == AGENT_EXPERIENCE_INDEX_PATH
        else item
        for item in documents
    )


def applies_when_chars(documents: tuple[MemoryDocument, ...]) -> int:
    """Characters of every lesson's `Applies when`, the part Action is handed."""

    return sum(len(entry.applies_when) for entry in _parse_entries(documents))


def validate_agent_experience_tree_change(
    *,
    base_documents: tuple[MemoryDocument, ...],
    published_documents: tuple[MemoryDocument, ...],
    allowed_new_experience_ids: tuple[str, ...],
) -> AgentExperienceTreeChange:
    """Compare two well-formed trees and check what the run was allowed to write."""

    base_entries = {
        entry.experience_id: entry
        for entry in parse_agent_experience_tree(base_documents)
    }
    entries = parse_agent_experience_tree(published_documents)
    before = _entry_documents_by_path(base_documents)
    after = _entry_documents_by_path(published_documents)
    added = tuple(
        sorted(experience_id_from_path(path) for path in set(after) - set(before))
    )
    removed = tuple(
        sorted(experience_id_from_path(path) for path in set(before) - set(after))
    )
    modified = tuple(
        sorted(
            experience_id_from_path(path)
            for path in set(before) & set(after)
            if before[path] != after[path]
        )
    )
    unexpected = set(added) - set(allowed_new_experience_ids)
    if unexpected:
        raise MemoryCatalogIntegrityError(
            "Agent Experience entry uses an ID this run does not own"
        )
    superseded = {
        superseded_id
        for entry in entries
        for superseded_id in entry.supersedes_experience_ids
    }
    # A lesson leaves the tree only when a surviving entry replaces it, so a
    # plain delete cannot silently drop durable memory.
    if set(removed) - superseded:
        raise MemoryCatalogIntegrityError(
            "Agent Experience entry was removed without a superseding entry"
        )
    if superseded & {entry.experience_id for entry in entries}:
        raise MemoryCatalogIntegrityError(
            "Agent Experience supersedes an entry that is still active"
        )
    # What an entry newly claims to replace must be an entry this change
    # removed; older claims name entries that left in earlier revisions.
    claimed = {
        superseded_id
        for entry in entries
        for superseded_id in entry.supersedes_experience_ids
        if superseded_id
        not in _superseded_before(base_entries.get(entry.experience_id))
    }
    if claimed - set(removed):
        raise MemoryCatalogIntegrityError(
            "Agent Experience supersedes an ID that was not in the tree: "
            + ", ".join(sorted(claimed - set(removed)))
        )
    return AgentExperienceTreeChange(added=added, modified=modified, removed=removed)


def _superseded_before(entry: AgentExperienceContent | None) -> tuple[str, ...]:
    return () if entry is None else entry.supersedes_experience_ids


def _parse_entries(
    documents: tuple[MemoryDocument, ...],
) -> tuple[AgentExperienceContent, ...]:
    return tuple(
        parse_agent_experience_markdown(
            document.content,
            expected_experience_id=experience_id_from_path(document.source_path),
        )
        for document in sorted(documents, key=lambda item: item.source_path)
        if document.source_path != AGENT_EXPERIENCE_INDEX_PATH
    )


def _documents_by_path(
    documents: tuple[MemoryDocument, ...],
) -> dict[str, str]:
    result = {item.source_path: item.content for item in documents}
    if len(result) != len(documents):
        raise MemoryCatalogIntegrityError("Agent Experience paths are not unique")
    return result


def _entry_documents_by_path(
    documents: tuple[MemoryDocument, ...],
) -> dict[str, str]:
    return {
        item.source_path: item.content
        for item in documents
        if item.source_path != AGENT_EXPERIENCE_INDEX_PATH
    }


def _validate_tree_paths(documents: dict[str, str]) -> None:
    if AGENT_EXPERIENCE_INDEX_PATH not in documents:
        raise MemoryCatalogIntegrityError("Agent Experience index is absent")
    for path in documents:
        if path == AGENT_EXPERIENCE_INDEX_PATH:
            continue
        experience_id_from_path(path)


__all__ = [
    "AgentExperienceTreeChange",
    "applies_when_chars",
    "parse_agent_experience_tree",
    "rebuild_agent_experience_index",
    "validate_agent_experience_tree_change",
]
