"""The hooks' HTTP shapes: the tasks Actions scheduled, and the owner's switch."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from pantaray_agents.local_runtime.hooks.controls import StoppedReason, TurnResult


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class TaskHookResponse(_View):
    """A task an Action scheduled with the user's yes in that conversation. It
    fires while ``enabled`` and not stopped: once ``at``, or ``every`` calendar
    unit or duration (``offset`` after each unit's start), never after
    ``until``."""

    hook_id: str
    at: datetime | None
    every: str | None
    offset: str | None
    until: datetime | None
    message: str
    enabled: bool
    next_fire_at: datetime | None
    created_from_action_id: str
    # The conversation each fire continues; None starts a new one each time.
    target_action_id: str | None
    # None while the latest turn has not ended.
    last_result: TurnResult | None
    failure_count: int
    # Set when failures in a row stopped the task; enabling it clears them.
    stopped_reason: StoppedReason | None


class HookListResponse(_View):
    hooks: tuple[TaskHookResponse, ...]


class HookEnabledRequest(_Request):
    enabled: bool
