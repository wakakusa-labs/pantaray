from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ..models import BrokerNetworkPolicy


class ActionSandboxStorage(BaseModel):
    """An Action's own storage, the exceptions to the private-storage deny."""

    model_config = ConfigDict(extra="forbid", strict=True)

    plan_path: str
    workspace_root: str
    published_results_root: str


class BrokerToSandboxCommandRequest(BaseModel):
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
    timeout_ms: int
    stdout_max_bytes: int
    stderr_max_bytes: int
    temp_dir: str
    temp_storage_limit_bytes: int
    network_policy: BrokerNetworkPolicy
    use_login_environment: bool
    # True only for an Action bash call the user approved to run unsandboxed:
    # the helper then starts argv without the seatbelt profile and keeps every
    # other limit (timeout, output caps, temp quota, process group).
    run_outside_sandbox: bool
    protected_backend_address: str


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


SandboxMessage = SandboxLifecycleEvent | SandboxOutputChunk | SandboxCommandCompletion


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes: ...


def encode_message(message: BaseModel) -> bytes:
    serialized = message.model_dump_json()
    return (serialized + "\n").encode("utf-8")


def decode_request(raw_line: str) -> BrokerToSandboxCommandRequest:
    return BrokerToSandboxCommandRequest.model_validate_json(raw_line)


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


async def iter_decoded_messages(
    stream: AsyncLineReader,
) -> AsyncIterator[SandboxMessage]:
    while True:
        raw_line = await stream.readline()
        if not raw_line:
            return
        yield decode_message(raw_line.decode("utf-8"))
