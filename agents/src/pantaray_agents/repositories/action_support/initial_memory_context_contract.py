"""Typed repository contract for ActionAgent initial memory context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pantaray_agents.schema.agent.action import MemoryContextEpochCheckpoint

type InitialMemorySourceType = Literal["long_term_insight", "facts", "agent_experience"]


@dataclass(frozen=True, slots=True)
class InitialMemoryArtifactFile:
    storage_path: str
    sha256: str
    byte_size: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class InitialMemoryArtifact:
    source_type: InitialMemorySourceType
    source_record_id: str
    artifact_id: str
    logical_updated_at: str
    files: tuple[InitialMemoryArtifactFile, ...]


@dataclass(frozen=True, slots=True)
class InitialInsightBrief:
    insight_id: str
    insight_profile_brief: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class InitialFactsBrief:
    fact_id: str
    facts_profile_brief: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class InitialMemoryContext:
    insight: InitialInsightBrief | None
    facts: InitialFactsBrief | None
    artifacts: tuple[InitialMemoryArtifact, ...]
    context_epoch: MemoryContextEpochCheckpoint | None


__all__ = [
    "InitialFactsBrief",
    "InitialInsightBrief",
    "InitialMemoryArtifact",
    "InitialMemoryArtifactFile",
    "InitialMemoryContext",
    "InitialMemorySourceType",
]
