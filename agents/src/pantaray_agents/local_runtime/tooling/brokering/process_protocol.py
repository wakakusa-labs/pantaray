"""Arguments and validated requests of the tools that reach a run's processes.

A process is one a ``bash`` call of the same run started with
``run_in_background``; these tools only read or stop it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..sandbox import runtime_policy

# The opaque id the starting bash call returned, such as "p1".
ProcessId = Annotated[str, Field(min_length=1, pattern=r"\S")]


class ReadProcessOutputToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    process_id: ProcessId
    wait_seconds: float = Field(
        default=runtime_policy.PROCESS_READ_WAIT_DEFAULT_SECONDS,
        ge=0,
        le=runtime_policy.PROCESS_READ_WAIT_MAX_SECONDS,
    )


class StopProcessToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    process_id: ProcessId


class _ValidatedProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str


class ValidatedReadProcessOutputRequest(_ValidatedProcessRequest):
    args: ReadProcessOutputToolArgs


class ValidatedStopProcessRequest(_ValidatedProcessRequest):
    args: StopProcessToolArgs


__all__ = [
    "ReadProcessOutputToolArgs",
    "StopProcessToolArgs",
    "ValidatedReadProcessOutputRequest",
    "ValidatedStopProcessRequest",
]
