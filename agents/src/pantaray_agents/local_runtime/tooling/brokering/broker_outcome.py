"""Raw and finalized broker execution outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import UnprojectedToolOutput
from pantaray_agents.tools.files.read_contract import ReadToolResult

from ..models import ToolOutputStorageKind


@dataclass(frozen=True, slots=True)
class UnprojectedBrokerToolOutcome:
    status: Literal["success", "error"]
    output: UnprojectedToolOutput
    tool_invocation_id: str | None = None
    search_text: str | None = None
    stdout_text: str | None = None
    stderr_text: str | None = None
    file_paths: tuple[str, ...] = ()
    file_reference_paths: tuple[str, ...] = ()
    attachments: tuple[dict[str, JSONValue], ...] = ()


def read_tool_outcome(result: ReadToolResult) -> UnprojectedBrokerToolOutcome:
    """A read/list/glob/grep result as the outcome the Action stores."""

    return UnprojectedBrokerToolOutcome(
        status="success",
        output=result.output,
        search_text=result.search_text,
        file_paths=result.file_paths,
        file_reference_paths=result.file_reference_paths,
        attachments=result.attachments,
    )


@dataclass(frozen=True, slots=True)
class BrokerPreflightOutcome:
    """Validation-only result that is never persisted or exposed as tool history."""

    status: Literal["success"]
    output: JSONValue


@dataclass(frozen=True, slots=True)
class BrokerToolFailure:
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class BrokerToolOutcome:
    status: Literal["success", "error"]
    output: JSONValue
    output_storage_kind: ToolOutputStorageKind
    failure: BrokerToolFailure | None = None
    tool_invocation_id: str | None = None
    search_text: str | None = None
    stdout_text: str | None = None
    stderr_text: str | None = None
    file_paths: tuple[str, ...] = ()
    attachments: tuple[dict[str, JSONValue], ...] = ()


def project_broker_tool_outcome(
    outcome: UnprojectedBrokerToolOutcome,
    *,
    output: JSONValue,
    tool_invocation_id: str | None,
    search_text: str | None,
    stdout_text: str | None,
    stderr_text: str | None,
    output_storage_kind: ToolOutputStorageKind,
) -> BrokerToolOutcome:
    """Build a projected broker result without retaining raw output bytes."""

    return BrokerToolOutcome(
        status=outcome.status,
        output=output,
        output_storage_kind=output_storage_kind,
        failure=_broker_tool_failure(outcome),
        tool_invocation_id=tool_invocation_id,
        search_text=search_text,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        file_paths=outcome.file_paths,
        attachments=outcome.attachments,
    )


def _broker_tool_failure(
    outcome: UnprojectedBrokerToolOutcome,
) -> BrokerToolFailure | None:
    if outcome.status != "error":
        return None
    if not isinstance(outcome.output, dict):
        return _unknown_failure()
    error = outcome.output.get("error")
    if not isinstance(error, dict):
        return _unknown_failure()
    error_type = error.get("type")
    message = error.get("message")
    return BrokerToolFailure(
        error_type=error_type if isinstance(error_type, str) else "UnknownToolError",
        message=(
            message
            if isinstance(message, str)
            else "Tool returned an unstructured error."
        ),
    )


def _unknown_failure() -> BrokerToolFailure:
    return BrokerToolFailure(
        error_type="UnknownToolError",
        message="Tool returned an unstructured error.",
    )


__all__ = [
    "BrokerPreflightOutcome",
    "BrokerToolFailure",
    "BrokerToolOutcome",
    "UnprojectedBrokerToolOutcome",
    "project_broker_tool_outcome",
    "read_tool_outcome",
]
