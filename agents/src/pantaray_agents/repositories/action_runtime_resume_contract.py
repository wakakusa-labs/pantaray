"""Typed durable inputs for non-approval Action resume."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.action_message_codec import (
    parse_action_user_message,
    render_action_user_request_text,
)
from pantaray_agents.schema.repositories.repository import DBRow


class ActionRuntimeResumeContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActionResumeUserStep:
    step_id: str
    step_number: int
    local_step_number: int
    short_step_id: str
    message: ActionUserMessageInput
    created_at: str


@dataclass(frozen=True, slots=True)
class ActionRuntimeResumeContext:
    checkpoint_row: DBRow | None
    # Every adopted USER step between the checkpoint and the current run, oldest
    # first: runs that ended before writing a checkpoint never answered them.
    intervening_user_steps: tuple[ActionResumeUserStep, ...]


def select_action_runtime_resume_context(
    rows: Sequence[DBRow],
    *,
    expected_user_id: str,
    expected_action_id: str,
    current_user_step_number: int,
) -> ActionRuntimeResumeContext:
    action_rows = [row for row in rows if row.get("action_id") == expected_action_id]
    checkpoint = max(
        (row for row in action_rows if row.get("runtime_state_checkpoint") is not None),
        key=_checkpoint_order_key,
        default=None,
    )
    checkpoint_step = _positive_int(checkpoint.get("step_number")) if checkpoint else 0
    intervening = sorted(
        (
            row
            for row in action_rows
            if row.get("step_type") == "user_request"
            and row.get("adopted_process_id") is not None
            and checkpoint_step
            < _positive_int(row.get("step_number"))
            < current_user_step_number
        ),
        key=lambda row: _positive_int(row.get("step_number")),
    )
    return ActionRuntimeResumeContext(
        checkpoint,
        tuple(
            parse_action_resume_user_step(
                row,
                expected_user_id=expected_user_id,
                expected_action_id=expected_action_id,
            )
            for row in intervening
        ),
    )


def _checkpoint_order_key(row: DBRow) -> tuple[int, bool, str, str, str]:
    return (
        _positive_int(row.get("step_number")),
        row.get("completed_at") is not None,
        str(row.get("completed_at") or ""),
        str(row.get("created_at") or ""),
        str(row.get("step_id") or ""),
    )


def parse_action_resume_user_step(
    row: Mapping[str, object],
    *,
    expected_user_id: str,
    expected_action_id: str,
) -> ActionResumeUserStep:
    if (
        row.get("user_id"),
        row.get("action_id"),
        row.get("step_type"),
        row.get("status"),
    ) != (expected_user_id, expected_action_id, "user_request", "success"):
        raise ActionRuntimeResumeContractError("Action USER identity is inconsistent")
    step_number = _positive_int(row.get("step_number"))
    local_step_number = _positive_int(row.get("local_step_number"))
    short_step_id = _required_text(row.get("short_step_id"))
    if short_step_id != f"S-{local_step_number}-USER":
        raise ActionRuntimeResumeContractError("Action USER short ID is inconsistent")
    _required_text(row.get("adopted_process_id"))

    try:
        message = parse_action_user_message(
            _required_text(row.get("user_message_json"))
        )
    except ValidationError as exc:
        raise ActionRuntimeResumeContractError(
            "Action USER message does not match V1 schema"
        ) from exc
    request_text = _required_text(row.get("user_request_text"))
    if (
        message.message_id != _required_text(row.get("user_message_id"))
        or render_action_user_request_text(message) != request_text
    ):
        raise ActionRuntimeResumeContractError("Action USER message is inconsistent")

    return ActionResumeUserStep(
        step_id=_required_text(row.get("step_id")),
        step_number=step_number,
        local_step_number=local_step_number,
        short_step_id=short_step_id,
        message=message,
        created_at=_required_text(row.get("created_at")),
    )


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRuntimeResumeContractError("Action USER text is incomplete")
    return value


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ActionRuntimeResumeContractError("Action USER number is invalid")
    return value
