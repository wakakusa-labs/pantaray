from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from .search_policy import MemoryCatalogSource as MemorySource
from .search_policy import MemorySearchSource

MemoryLifecycle = Literal["preparing", "active", "tombstoned"]
MemoryIntegrity = Literal["healthy", "corrupt"]
MemoryBodyKind = Literal["inline", "artifact_tree"]
MemoryBlockKind = Literal[
    "record_root",
    "document_root",
    "heading",
    "paragraph",
    "list_item",
    "block_quote",
    "code_block",
    "table",
    "other",
]
DraftLinkState = Literal["carried", "pending", "removed"]
MemoryIntentKind = Literal[
    "fact",
    "long_term_insight",
    "agent_experience",
    "fact_repair",
    "long_term_insight_repair",
    "agent_experience_repair",
]
MemoryReferenceDepth = Literal[0, 1]
MemorySearchMatchedBy = Literal["keywords", "query", "both"]


@dataclass(frozen=True, slots=True)
class MemoryNode:
    user_id: str
    node_id: str
    source: MemorySource
    source_record_id: str
    lifecycle: MemoryLifecycle
    integrity: MemoryIntegrity
    current_revision_id: str | None


@dataclass(frozen=True, slots=True)
class ArtifactIntent:
    user_id: str
    revision_id: str
    node_id: str
    base_revision_id: str | None
    manifest_sha256: str
    artifact_root_path: str
    intent_kind: MemoryIntentKind
    domain_payload_json: str
    draft: MemoryDraftCheckpoint


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    user_id: str
    revision_id: str
    node_id: str
    body_kind: MemoryBodyKind
    inline_body: str | None
    artifact_root_path: str | None
    fragment_schema_version: int
    content_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryFragment:
    user_id: str
    fragment_id: str
    revision_id: str
    source_path: str
    block_kind: MemoryBlockKind
    block_index: int
    heading_path: str | None
    content_text: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class MemoryLink:
    user_id: str
    source_revision_id: str
    local_ref_id: str
    source_fragment_id: str
    target_fragment_id: str
    reference_note: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryEvidenceEdge:
    user_id: str
    derived_revision_id: str
    source_revision_id: str
    evidence_role: str


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    source_path: str
    content: str


@dataclass(frozen=True, slots=True)
class MemoryContextItem:
    context_handle: str
    source: MemorySource
    label: str
    source_path: str
    heading_path: str | None
    content: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class ResolvedContextItem:
    item: MemoryContextItem
    user_id: str
    fragment_id: str
    revision_id: str
    node_id: str
    reference_depth: MemoryReferenceDepth


@dataclass(frozen=True, slots=True)
class MemoryContextEpoch:
    epoch_id: str
    run_id: str
    user_id: str
    items: tuple[ResolvedContextItem, ...]


class MemorySearchResult(TypedDict):
    source: MemorySearchSource
    record_id: str
    content: str
    source_path: str
    heading_path: str | None
    observed_at: str
    matched_by: MemorySearchMatchedBy
    context_handle: NotRequired[str]


@dataclass(frozen=True, slots=True)
class DraftLink:
    local_ref_id: str
    target_fragment_id: str
    source_path: str
    source_anchor_text: str
    source_anchor_occurrence: int
    reference_note: str
    created_at: str
    state: DraftLinkState


@dataclass(frozen=True, slots=True)
class AppliedDraftCommand:
    tool_invocation_id: str
    local_ref_id: str | None
    draft_revision: str


@dataclass(frozen=True, slots=True)
class MemoryDraftCheckpoint:
    draft_session_id: str
    user_id: str
    owner_node_id: str
    base_revision_id: str | None
    draft_revision: str
    documents: tuple[MemoryDocument, ...]
    links: tuple[DraftLink, ...]
    applied_commands: tuple[AppliedDraftCommand, ...]


@dataclass(frozen=True, slots=True)
class ResolvedLinkCommand:
    tool_invocation_id: str
    epoch_id: str
    draft_session_id: str
    user_id: str
    target_fragment_id: str
    source_path: str
    exact_text: str
    occurrence: int
    note: str
    local_ref_id: str
    expected_draft_revision: str


class ResolvedMemoryLink(TypedDict):
    local_ref_id: str
    reference_note: str
    source_path: str
    source_heading_path: str | None
    target_fragment_id: str
    target_source: MemorySource
    target_content: str
    target_path: str
    target_heading_path: str | None
    target_revision_id: str
    target_is_current: bool
    target_lifecycle: MemoryLifecycle
    target_integrity: MemoryIntegrity
    current_target_revision_id: str
    target_context_handle: NotRequired[str]
