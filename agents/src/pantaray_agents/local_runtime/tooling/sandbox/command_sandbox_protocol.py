from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from ..models import BrokerNetworkPolicy


class ActionSandboxStorage(BaseModel):
    """An Action's own storage, the exceptions to the private-storage deny."""

    model_config = ConfigDict(extra="forbid", strict=True)

    workspace_root: str
    published_results_root: str


class SandboxCommandSpec(BaseModel):
    """What every sandboxed command is: its files, process, and network rules."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    real_read_roots: list[str]
    real_write_roots: list[str]
    private_storage_roots: list[str]
    # None for a command that belongs to no Action: private storage stays whole.
    action_storage: ActionSandboxStorage | None
    app_runtime_root: str
    cwd: str
    argv: list[str]
    runtime_read_roots: list[str]
    env: dict[str, str]
    temp_dir: str
    temp_storage_limit_bytes: int
    network_policy: BrokerNetworkPolicy
    use_login_environment: bool
    # True only for an Action bash call the user approved to run unsandboxed:
    # the helper then starts argv without the seatbelt profile and keeps every
    # other limit (timeout, output caps, temp quota, process group).
    run_outside_sandbox: bool
    protected_backend_address: str


class BrokerToSandboxCommandRequest(SandboxCommandSpec):
    """Run one-shot: to completion within a time limit and output caps."""

    timeout_ms: int
    stdout_max_bytes: int
    stderr_max_bytes: int


class BrokerToSandboxLongRunningRequest(SandboxCommandSpec):
    """Run until the command ends or the parent stops it, streaming its output.

    No time limit and no output cap: the parent keeps what it needs of the
    stream. The temp-space limit still ends the command. While it runs, the
    parent may write ``SandboxStopRequest`` lines to the helper's input.
    """

    # How long a stop waits after SIGTERM before SIGKILL.
    stop_grace_ms: int


# Told apart by their own fields, which neither model lets the other carry.
SandboxRequest = BrokerToSandboxCommandRequest | BrokerToSandboxLongRunningRequest


class SandboxStopRequest(BaseModel):
    """Parent to helper, long-running only: stop the command.

    The helper sends SIGTERM to its process group, which the command shares,
    waits the request's ``stop_grace_ms``, then SIGKILLs the command. A stop
    after the first, or after the command ended, has no effect.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str


class SandboxLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    event: Literal[
        "started",
        "timeout_sent",
        "kill_sent",
    ]
    at: str


class SandboxOutputChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    stream: Literal["stdout", "stderr"]
    seq: int
    data: str


class SandboxCommandCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    outcome: Literal[
        "exited",
        "signaled",
        "timed_out",
        "canceled",
        "spawn_failed",
    ]
    exit_code: int | None = None
    signal: int | None = None
    stdout_bytes: int
    stderr_bytes: int
    budget_exceeded_kind: (
        Literal["stdout_limit", "stderr_limit", "temp_storage_limit"] | None
    ) = None

    @model_validator(mode="after")
    def _require_budget_reason(self) -> Self:
        if self.outcome == "canceled" and self.budget_exceeded_kind is None:
            raise ValueError("budget termination requires the exceeded resource")
        return self


class SandboxProcessExited(BaseModel):
    """The long-running command ended; the last message of its stream.

    Processes the command left in the group may still run: the parent kills the
    helper's group after this message, as it does after a one-shot receipt.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    ended_by: Literal["command", "stop", "temp_storage_limit"]
    exit_code: int | None
    signal: int | None

    @model_validator(mode="after")
    def _require_exit_code_or_signal(self) -> Self:
        if (self.exit_code is None) == (self.signal is None):
            raise ValueError("an ended command has either an exit code or a signal")
        return self


class SandboxProcessSpawnFailed(BaseModel):
    """The long-running command could not be started; the only message sent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    error: str


SandboxMessage = SandboxLifecycleEvent | SandboxOutputChunk | SandboxCommandCompletion
SandboxLongRunningMessage = (
    SandboxLifecycleEvent
    | SandboxOutputChunk
    | SandboxProcessExited
    | SandboxProcessSpawnFailed
)


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes: ...


def encode_message(message: BaseModel) -> bytes:
    serialized = message.model_dump_json()
    return (serialized + "\n").encode("utf-8")


_REQUEST_ADAPTER = TypeAdapter[SandboxRequest](SandboxRequest)


def decode_request(raw_line: str) -> SandboxRequest:
    return _REQUEST_ADAPTER.validate_json(raw_line)


def decode_message(raw_line: str) -> SandboxMessage:
    payload = json.loads(raw_line)
    errors: list[ValidationError] = []
    for model_type in (
        SandboxLifecycleEvent,
        SandboxOutputChunk,
        SandboxCommandCompletion,
    ):
        try:
            return model_type.model_validate(payload)
        except ValidationError as exc:
            errors.append(exc)
    raise ValueError(f"invalid sandbox message payload: {payload!r}") from errors[-1]


_LONG_RUNNING_MESSAGE_ADAPTER = TypeAdapter[SandboxLongRunningMessage](
    SandboxLongRunningMessage
)


def decode_long_running_message(raw_line: str) -> SandboxLongRunningMessage:
    """Raises ``pydantic.ValidationError`` (a ``ValueError``) on a bad line."""

    return _LONG_RUNNING_MESSAGE_ADAPTER.validate_json(raw_line)


async def iter_decoded_messages(
    stream: AsyncLineReader,
) -> AsyncIterator[SandboxMessage]:
    while True:
        raw_line = await stream.readline()
        if not raw_line:
            return
        yield decode_message(raw_line.decode("utf-8"))
