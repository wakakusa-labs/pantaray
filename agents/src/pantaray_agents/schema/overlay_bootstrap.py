"""Overlay bootstrap API schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OverlayActionPhase = Literal[
    "idle",
    "requesting",
    "accepted_pending_start",
    "processing",
    "terminal",
]
OverlayReactionState = Literal["accepted", "rejected"] | None
SuggestionInteractionContract = Literal["action_offer", "message_only"] | None
LiveResumeKind = Literal["none", "suggestion", "action"]


class SuggestionTodoView(BaseModel):
    """A shown Suggestion's TODO; ``state`` is null once an Action carries it.

    ``handed_over`` is a TODO Pantaray has nothing left on, shown with no state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    state: Literal["waiting", "partial", "done", "handed_over"] | None


class OverlaySnapshotModel(BaseModel):
    """Hydration-ready overlay snapshot."""

    suggestionId: str = Field(description="Suggestion ID")
    commandId: str | None = Field(default=None, description="Latest action command ID")
    interactionContract: SuggestionInteractionContract = Field(
        default=None, description="Suggestion interaction contract"
    )
    suggestionText: str = Field(default="", description="Rendered suggestion text")
    reactionState: OverlayReactionState = Field(
        default=None, description="Committed user reaction"
    )
    reactionTimestamp: str | None = Field(
        default=None, description="Committed reaction timestamp"
    )
    dismissReason: str | None = Field(
        default=None, description="The user's reason recorded with the dismissal"
    )
    artifacts: list[str] = Field(
        description="Files the Suggestion made, relative to its artifact folder"
    )
    todo: SuggestionTodoView | None = Field(
        description="The TODO the Suggestion became; null when it has none"
    )
    actionPhase: OverlayActionPhase = Field(description="Overlay action phase")
    actionStatus: str | None = Field(default=None, description="Durable action status")
    actionFailureCode: str | None = Field(
        default=None, description="Canonical action failure code"
    )
    actionFailureStage: str | None = Field(
        default=None, description="Canonical action failure stage"
    )
    actionFailureMessagePublic: str | None = Field(
        default=None, description="Canonical action failure message"
    )
    processId: str | None = Field(default=None, description="Current process ID")
    actionId: str | None = Field(default=None, description="Current action ID")
    updatedAt: str | None = Field(default=None, description="Latest update timestamp")
    lastSequence: int = Field(description="Last applied public event sequence")
    isLive: bool = Field(description="Whether the snapshot should live-resume")


class OverlayLiveResumeModel(BaseModel):
    """Live resume hint returned with the bootstrap snapshot."""

    kind: LiveResumeKind = Field(description="Open process kind")
    process_id: str | None = Field(default=None, description="Open process ID")
    action_id: str | None = Field(default=None, description="Open action ID")
    command_id: str | None = Field(default=None, description="Open command ID")
    accepted_at: str | None = Field(default=None, description="Accepted timestamp")


class OverlayBootstrapResponse(BaseModel):
    """Backend-owned overlay bootstrap response."""

    suggestion_id: str = Field(description="Suggestion ID")
    artifacts_root: str = Field(
        description="Absolute folder that snapshot.artifacts are relative to"
    )
    snapshot: OverlaySnapshotModel = Field(description="Replay-derived snapshot")
    last_sequence: int = Field(description="Last durable public event sequence")
    live_resume: OverlayLiveResumeModel = Field(description="Live resume hint")
