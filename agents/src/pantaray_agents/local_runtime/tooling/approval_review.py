"""Ask the user's model whether a pending approval may go ahead without the user.

The review reads only what the user wrote, the task text the user did not write
(labelled as such), and the operation the agent asks to run. Tool output,
assistant text, and web or document contents never reach it: the request type
has no place for them, so text the agent read cannot argue its own approval.

A review that fails, times out, or answers outside its schema gives no verdict,
and the caller asks the user, so the review can only ever save the user a prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pantaray_agents.agents.core.mixins.llm_usage import (
    LlmUsage,
    TokenSink,
    llm_usage_from_metadata,
)
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.utils.llm_types import types
from pantaray_agents.utils.prompt_loader import prompt_loader
from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.profiles import APPROVAL_REVIEW_PROFILE_ID

if TYPE_CHECKING:
    from pantaray_agents.dependencies import LLMClientProtocol

logger = logging.getLogger(__name__)

APPROVAL_REVIEW_PROMPT_NAME = "approval_review"

# What makes a command need review: running without the sandbox, writing outside
# the registered folders, or running with the user's login environment.
type CommandReviewKind = Literal[
    "outside_sandbox", "outside_write", "login_environment"
]


@dataclass(frozen=True, slots=True)
class RequestTextNotByUser:
    """Task text the Action started from that the user did not write themselves."""

    source: Literal["suggestion", "todo"]
    text: str


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """A command or file tool call waiting for approval."""

    tool: str
    kinds: frozenset[CommandReviewKind]
    command: str
    # None for a file tool, which runs in no folder.
    cwd: str | None
    additional_write_folders: tuple[str, ...]
    justification: str | None


@dataclass(frozen=True, slots=True)
class PendingIntegrationWrite:
    """A change in a connected service, made through the user's account."""

    toolkit: str
    action: str
    # The connected account it acts through: its id and the name the user sees.
    account_id: str
    account_label: str
    arguments: JSONValue
    justification: str | None


type PendingOperation = PendingCommand | PendingIntegrationWrite


@dataclass(frozen=True, slots=True)
class ApprovalReviewRequest:
    user_messages: tuple[str, ...]
    request_text_not_by_user: RequestTextNotByUser | None
    operation: PendingOperation
    workspace_folders: tuple[str, ...]
    # Operations the user declined earlier in this Action, newest first.
    declined_by_user: tuple[PendingOperation, ...]


class ApprovalReviewVerdict(BaseModel):
    """The review's answer; ``reason`` is one sentence shown to the user."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["allow", "ask_user"]
    reason: str = Field(min_length=1)


def build_approval_review_payload(request: ApprovalReviewRequest) -> str:
    """The review input as one JSON document, so no field can pose as another."""

    not_by_user = request.request_text_not_by_user
    payload: dict[str, JSONValue] = {
        "user_messages": list(request.user_messages),
        "request_text_not_by_user": (
            None
            if not_by_user is None
            else {"source": not_by_user.source, "text": not_by_user.text}
        ),
        "operation": _render_operation(request.operation),
        "workspace_folders": list(request.workspace_folders),
        "declined_by_user": [
            _render_operation(declined) for declined in request.declined_by_user
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_operation(operation: PendingOperation) -> dict[str, JSONValue]:
    match operation:
        case PendingCommand():
            return {
                "tool": operation.tool,
                "requires": sorted(operation.kinds),
                "command": operation.command,
                "cwd": operation.cwd,
                "additional_write_folders": list(operation.additional_write_folders),
                "justification": operation.justification,
            }
        case PendingIntegrationWrite():
            return {
                "requires": ["integration_write"],
                "toolkit": operation.toolkit,
                "action": operation.action,
                "account": {
                    "id": operation.account_id,
                    "label": operation.account_label,
                },
                "arguments": operation.arguments,
                "justification": operation.justification,
            }


def _parse_verdict(text: str) -> ApprovalReviewVerdict | None:
    """The verdict in the model's answer, or None when the answer is not one."""

    try:
        return ApprovalReviewVerdict.model_validate_json(text)
    except ValidationError:
        logger.warning("Approval review answered outside its schema")
        return None


async def review_approval(
    client: LLMClientProtocol, request: ApprovalReviewRequest, *, sink: TokenSink
) -> ApprovalReviewVerdict | None:
    """The review's verdict, or None when it gave none and the user must decide.

    The caller runs this inside the Action's trace, which routes the request to
    the user's selected model, and its tokens count toward the Action's usage.
    """

    prompt = prompt_loader.load_config(APPROVAL_REVIEW_PROMPT_NAME)
    timeout_seconds = load_local_runtime_tunables().approval_review.timeout_seconds
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.aio.models.generate_content(
                contents=[
                    prompt.prompt.format(
                        review_request=build_approval_review_payload(request)
                    )
                ],
                config=types.GenerateContentConfig(
                    inference_profile=APPROVAL_REVIEW_PROFILE_ID,
                    system_instruction=prompt.system_instruction,
                    response_mime_type="application/json",
                    response_schema=ApprovalReviewVerdict,
                ),
            )
    except TimeoutError:
        logger.warning("Approval review timed out after %s seconds", timeout_seconds)
        return None
    except LlmProxyExecutionError as exc:
        _record_usage(sink, llm_usage_from_metadata(exc.usage_metadata))
        logger.warning("Approval review failed: %s", exc.error_code)
        return None
    except ValidationError:
        # The direct route decodes the answer with the response schema itself.
        logger.warning("Approval review answered outside its schema")
        return None
    _record_usage(
        sink, llm_usage_from_metadata(getattr(response, "usage_metadata", None))
    )
    return _parse_verdict(response.text)


def _record_usage(sink: TokenSink, usage: LlmUsage) -> None:
    # A spent budget stops the Action at its next model call, not this review.
    sink.record(usage, stage=APPROVAL_REVIEW_PROMPT_NAME, may_raise=False)


__all__ = [
    "APPROVAL_REVIEW_PROMPT_NAME",
    "ApprovalReviewRequest",
    "ApprovalReviewVerdict",
    "CommandReviewKind",
    "PendingCommand",
    "PendingIntegrationWrite",
    "PendingOperation",
    "RequestTextNotByUser",
    "build_approval_review_payload",
    "review_approval",
]
