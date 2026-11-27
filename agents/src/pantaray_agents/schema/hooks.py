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
    """A task an Action scheduled with the user's yes in that conversation,
    which it continues. It fires only while ``approved``, ``enabled``, without
    an ``error`` and not stopped."""

    hook_id: str
    text: str
    # False once the file no longer holds the content the user said yes to.
    approved: bool
    enabled: bool
    error: str | None
    next_fire_at: datetime | None
    created_from_action_id: str
    # None while the latest turn has not ended.
    last_result: TurnResult | None
    failure_count: int
    # Set when failures in a row stopped the task; enabling it clears them.
    stopped_reason: StoppedReason | None


class HookListResponse(_View):
    hooks: tuple[TaskHookResponse, ...]


class HookEnabledRequest(_Request):
    enabled: bool
