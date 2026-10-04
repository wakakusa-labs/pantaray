from __future__ import annotations

import secrets
import uuid
from dataclasses import replace
from pathlib import PurePosixPath

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
    has_reference_markup,
    remove_reference_ids,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .epoch import resolve_context_handle
from .errors import MemoryLinkValidationError, MemoryPublicationConflictError
from .fragments import artifact_content_sha256, normalize_markdown
from .models import (
    AppliedDraftCommand,
    DraftLink,
    MemoryContextEpoch,
    MemoryDocument,
    MemoryDraftCheckpoint,
    ResolvedLinkCommand,
)
from .reference_edits import (
    REFERENCE_START_PATTERN,
    insert_memory_reference,
    validate_reference_note,
    validate_removed_reference_markup,
)


def create_memory_draft(
    *,
    user_id: str,
    owner_node_id: str,
    base_revision_id: str | None,
    documents: tuple[MemoryDocument, ...],
    carried_links: tuple[DraftLink, ...] = (),
) -> MemoryDraftCheckpoint:
    validate_memory_documents(documents)
    _validate_link_tag_bijection(
        locations=_link_tag_locations(documents), links=carried_links
    )
    normalized_documents = tuple(
        MemoryDocument(item.source_path, normalize_markdown(item.content))
        for item in documents
    )
    return MemoryDraftCheckpoint(
        draft_session_id=f"draft_{uuid.uuid4().hex}",
        user_id=user_id,
        owner_node_id=owner_node_id,
        base_revision_id=base_revision_id,
        draft_revision=_draft_revision(normalized_documents),
        documents=normalized_documents,
        links=carried_links,
        applied_commands=(),
    )


def replace_text_draft(
    *, draft: MemoryDraftCheckpoint, body: str
) -> MemoryDraftCheckpoint:
    if has_reference_markup(body):
        raise MemoryLinkValidationError(
            "draft_final_answer cannot accept raw ref markup; use link_memory"
        )
    documents = (MemoryDocument("body.md", normalize_markdown(body)),)
    return replace(
        draft,
        documents=documents,
        links=tuple(replace(link, state="removed") for link in draft.links),
        draft_revision=_draft_revision(documents),
    )


def replace_draft_documents(
    *,
    draft: MemoryDraftCheckpoint,
    documents: tuple[MemoryDocument, ...],
) -> MemoryDraftCheckpoint:
    """Replace text and retire deleted refs together; surviving refs keep their targets."""
    validate_memory_documents(documents)
    locations = _link_tag_locations(documents)
    removed_ids = {
        link.local_ref_id
        for link in draft.links
        if link.state != "removed" and link.local_ref_id not in locations
    }
    validate_removed_reference_markup(
        before=draft.documents, after=documents, removed_ids=removed_ids
    )
    links = tuple(
        replace(link, state="removed") if link.local_ref_id in removed_ids else link
        for link in draft.links
    )
    _validate_link_tag_bijection(locations=locations, links=links)
    normalized_documents = tuple(
        MemoryDocument(item.source_path, normalize_markdown(item.content))
        for item in documents
    )
    return replace(
        draft,
        documents=normalized_documents,
        links=links,
        draft_revision=_draft_revision(normalized_documents),
    )


def move_memory_document(
    *,
    draft: MemoryDraftCheckpoint,
    tool_invocation_id: str,
    source_path: str,
    destination_path: str,
    expected_draft_revision: str,
) -> MemoryDraftCheckpoint:
    if _applied_command(draft, tool_invocation_id) is not None:
        return draft
    _require_current_draft_revision(draft, expected_draft_revision)
    if source_path == destination_path:
        raise MemoryLinkValidationError("source and destination paths must differ")
    if any(item.source_path == destination_path for item in draft.documents):
        raise MemoryLinkValidationError("destination memory file already exists")
    matches = [item for item in draft.documents if item.source_path == source_path]
    if len(matches) != 1:
        raise MemoryLinkValidationError("source memory file is absent or ambiguous")
    documents = tuple(
        MemoryDocument(
            destination_path if item.source_path == source_path else item.source_path,
            item.content,
        )
        for item in draft.documents
    )
    links = tuple(
        replace(link, source_path=destination_path)
        if link.source_path == source_path
        else link
        for link in draft.links
    )
    revision = _draft_revision(documents)
    return replace(
        draft,
        documents=documents,
        links=links,
        draft_revision=revision,
        applied_commands=(
            *draft.applied_commands,
            AppliedDraftCommand(tool_invocation_id, None, revision),
        ),
    )


def delete_memory_document(
    *,
    draft: MemoryDraftCheckpoint,
    tool_invocation_id: str,
    source_path: str,
    expected_draft_revision: str,
) -> MemoryDraftCheckpoint:
    if _applied_command(draft, tool_invocation_id) is not None:
        return draft
    _require_current_draft_revision(draft, expected_draft_revision)
    if len(draft.documents) == 1:
        raise MemoryLinkValidationError("a memory draft must retain at least one file")
    if not any(item.source_path == source_path for item in draft.documents):
        raise MemoryLinkValidationError("memory file does not exist")
    documents = tuple(
        item for item in draft.documents if item.source_path != source_path
    )
    links = tuple(
        replace(link, state="removed") if link.source_path == source_path else link
        for link in draft.links
    )
    revision = _draft_revision(documents)
    return replace(
        draft,
        documents=documents,
        links=links,
        draft_revision=revision,
        applied_commands=(
            *draft.applied_commands,
            AppliedDraftCommand(tool_invocation_id, None, revision),
        ),
    )


def seal_link_command(
    *,
    epoch: MemoryContextEpoch,
    draft: MemoryDraftCheckpoint,
    tool_invocation_id: str,
    target_handle: str,
    source_path: str,
    exact_text: str,
    occurrence: int,
    note: str,
    expected_draft_revision: str,
) -> ResolvedLinkCommand:
    if draft.user_id != epoch.user_id:
        raise MemoryLinkValidationError("draft and context epoch users differ")
    target = resolve_context_handle(
        epoch=epoch,
        user_id=draft.user_id,
        context_handle=target_handle,
    )
    validate_reference_note(note)
    if occurrence < 1:
        raise MemoryLinkValidationError("anchor occurrence must be >= 1")
    if not tool_invocation_id.strip():
        raise MemoryLinkValidationError("tool_invocation_id must not be empty")
    return ResolvedLinkCommand(
        tool_invocation_id=tool_invocation_id,
        epoch_id=epoch.epoch_id,
        draft_session_id=draft.draft_session_id,
        user_id=draft.user_id,
        target_fragment_id=target.fragment_id,
        source_path=source_path,
        exact_text=exact_text,
        occurrence=occurrence,
        note=note.strip(),
        local_ref_id=f"ref_{secrets.token_hex(12)}",
        expected_draft_revision=expected_draft_revision,
    )


def link_memory(
    *, draft: MemoryDraftCheckpoint, command: ResolvedLinkCommand
) -> MemoryDraftCheckpoint:
    replay = _applied_command(draft, command.tool_invocation_id)
    if replay is not None:
        return draft
    if (
        command.draft_session_id != draft.draft_session_id
        or command.user_id != draft.user_id
    ):
        raise MemoryLinkValidationError(
            "resolved link command belongs to another draft"
        )
    if command.expected_draft_revision != draft.draft_revision:
        raise MemoryPublicationConflictError(
            "draft revision changed before link_memory"
        )
    new_documents, anchor_text = insert_memory_reference(
        documents=draft.documents,
        source_path=command.source_path,
        exact_text=command.exact_text,
        occurrence=command.occurrence,
        local_ref_id=command.local_ref_id,
        note=command.note,
    )
    new_revision = _draft_revision(new_documents)
    created_at = now_utc_iso()
    link = DraftLink(
        local_ref_id=command.local_ref_id,
        target_fragment_id=command.target_fragment_id,
        source_path=command.source_path,
        source_anchor_text=anchor_text,
        source_anchor_occurrence=command.occurrence,
        reference_note=command.note,
        created_at=created_at,
        state="pending",
    )
    return replace(
        draft,
        documents=new_documents,
        draft_revision=new_revision,
        links=(*draft.links, link),
        applied_commands=(
            *draft.applied_commands,
            AppliedDraftCommand(
                command.tool_invocation_id, command.local_ref_id, new_revision
            ),
        ),
    )


def unlink_memory(
    *,
    draft: MemoryDraftCheckpoint,
    tool_invocation_id: str,
    local_ref_id: str,
    expected_draft_revision: str,
) -> MemoryDraftCheckpoint:
    if _applied_command(draft, tool_invocation_id) is not None:
        return draft
    _require_current_draft_revision(draft, expected_draft_revision)
    active = [
        link
        for link in draft.links
        if link.local_ref_id == local_ref_id and link.state != "removed"
    ]
    if len(active) != 1:
        raise MemoryLinkValidationError("local_ref_id is absent or ambiguous")
    documents = tuple(
        MemoryDocument(
            item.source_path, remove_reference_ids(item.content, {local_ref_id})
        )
        for item in draft.documents
    )
    new_revision = _draft_revision(documents)
    links = tuple(
        replace(link, state="removed") if link.local_ref_id == local_ref_id else link
        for link in draft.links
    )
    return replace(
        draft,
        documents=documents,
        draft_revision=new_revision,
        links=links,
        applied_commands=(
            *draft.applied_commands,
            AppliedDraftCommand(tool_invocation_id, local_ref_id, new_revision),
        ),
    )


def validate_memory_documents(documents: tuple[MemoryDocument, ...]) -> None:
    if not documents:
        raise ValueError("memory draft requires at least one document")
    paths = [item.source_path for item in documents]
    if len(paths) != len(set(paths)) or any(not path.strip() for path in paths):
        raise ValueError("memory draft document paths must be nonempty and unique")
    for path in paths:
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or "\0" in path
            or "\\" in path
            or any(part in {"", ".", ".."} for part in pure.parts)
            or str(pure) != path
        ):
            raise ValueError(
                "memory draft document paths must be canonical relative POSIX paths"
            )


def validate_memory_draft_checkpoint(draft: MemoryDraftCheckpoint) -> None:
    if not draft.draft_session_id.strip() or not draft.user_id.strip():
        raise MemoryLinkValidationError("memory draft identity is incomplete")
    if not draft.owner_node_id.strip():
        raise MemoryLinkValidationError("memory draft owner is missing")
    validate_memory_documents(draft.documents)
    _validate_link_tag_bijection(
        locations=_link_tag_locations(draft.documents), links=draft.links
    )
    if draft.draft_revision != _draft_revision(draft.documents):
        raise MemoryLinkValidationError("memory draft revision does not match its body")
    command_ids = [item.tool_invocation_id for item in draft.applied_commands]
    if any(not item.strip() for item in command_ids) or len(command_ids) != len(
        set(command_ids)
    ):
        raise MemoryLinkValidationError(
            "memory draft applied command IDs must be nonempty and unique"
        )


def _link_tag_locations(
    documents: tuple[MemoryDocument, ...],
) -> dict[str, tuple[str, str | None]]:
    locations: dict[str, tuple[str, str | None]] = {}
    for document in documents:
        cursor = 0
        for occurrence in extract_markdown_references(document.content):
            if REFERENCE_START_PATTERN.search(
                document.content, cursor, occurrence.match_start
            ):
                raise MemoryLinkValidationError(
                    "ref markup is malformed; remove the complete tag or preserve it intact"
                )
            if occurrence.local_ref_id in locations:
                raise MemoryLinkValidationError("draft ref IDs must be unique")
            locations[occurrence.local_ref_id] = (document.source_path, occurrence.note)
            cursor = occurrence.match_end
        if REFERENCE_START_PATTERN.search(document.content, cursor):
            raise MemoryLinkValidationError(
                "ref markup is malformed; remove the complete tag or preserve it intact"
            )
    return locations


def _validate_link_tag_bijection(
    *, locations: dict[str, tuple[str, str | None]], links: tuple[DraftLink, ...]
) -> None:
    active_links = [link for link in links if link.state != "removed"]
    active_ids = [link.local_ref_id for link in active_links]
    if set(locations) != set(active_ids) or len(active_ids) != len(set(active_ids)):
        raise MemoryLinkValidationError(
            "draft tags and link mappings must be one-to-one"
        )
    if any(
        locations[link.local_ref_id] != (link.source_path, link.reference_note)
        for link in active_links
    ):
        raise MemoryLinkValidationError(
            "draft tag placement and link metadata must match"
        )


def _draft_revision(documents: tuple[MemoryDocument, ...]) -> str:
    return f"sha256:{artifact_content_sha256(documents)}"


def _applied_command(
    draft: MemoryDraftCheckpoint, tool_invocation_id: str
) -> AppliedDraftCommand | None:
    return next(
        (
            command
            for command in draft.applied_commands
            if command.tool_invocation_id == tool_invocation_id
        ),
        None,
    )


def _require_current_draft_revision(
    draft: MemoryDraftCheckpoint, expected_draft_revision: str
) -> None:
    if expected_draft_revision != draft.draft_revision:
        raise MemoryPublicationConflictError(
            "draft revision changed before memory tool execution"
        )
