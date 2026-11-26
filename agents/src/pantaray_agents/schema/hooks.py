"""The hooks' HTTP shapes: every hook's state, and the owner's switches."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.local_runtime.hooks.controls import StoppedReason, TurnResult
from pantaray_agents.local_runtime.hooks.listing import TaskOrigin


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class BuiltinHookResponse(_View):
    kind: Literal["builtin"] = "builtin"
    hook_id: str
    enabled: bool
    next_fire_at: datetime | None


class TaskHookResponse(_View):
    """A task file. It fires only while ``approved``, ``enabled``, without an
    ``error`` and not stopped; ``text`` is what approving it approves."""

    kind: Literal["task"] = "task"
    hook_id: str
    text: str
    content_hash: str
    approved: bool
    enabled: bool
    error: str | None
    next_fire_at: datetime | None
    # The conversation the task's turns go to.
    action_id: str | None
    # user: a file the user wrote; agent: one an Action scheduled with the
    # user's yes in that conversation, which it continues.
    origin: TaskOrigin
    created_from_action_id: str | None
    # None while the latest turn has not ended.
    last_result: TurnResult | None
    failure_count: int
    # Set when failures in a row stopped the task; enabling it clears them.
    stopped_reason: StoppedReason | None


class HookListResponse(_View):
    hooks: tuple[
        Annotated[BuiltinHookResponse | TaskHookResponse, Field(discriminator="kind")],
        ...,
    ]


class HookApprovalRequest(_Request):
    """The hash of the content the owner read, from the list."""

    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class HookEnabledRequest(_Request):
    enabled: bool
