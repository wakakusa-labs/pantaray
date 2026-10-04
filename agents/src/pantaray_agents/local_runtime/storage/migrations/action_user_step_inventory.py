from __future__ import annotations

import re
import sqlite3
from typing import NamedTuple

from pantaray_agents.utils.timestamps import normalize_iso8601_utc_z_microseconds

from .specs import MigrationError

_DATETIME_MAX_FRACTION_DIGITS = 6
_TIMESTAMP_PATTERN = re.compile(
    rf"\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}"
    rf"(?:[.,]\d{{1,{_DATETIME_MAX_FRACTION_DIGITS}}})?"
    r"(?:Z|[+-]\d{2}:\d{2})"
)


class ActionUserStepEvidence(NamedTuple):
    step_id: str
    action_id: str
    user_id: str
    accepted_sequence: int
    step_number: int
    local_step_number: int
    short_step_id: str
    created_at: str
    initial_user_message_id: str
    suggestion_id: str | None
    user_message_id: str | None
    user_message_json: str | None
    user_request_text: str


def load_action_user_step_inventory(
    connection: sqlite3.Connection,
    *,
    action_ids: frozenset[str] | None = None,
) -> tuple[ActionUserStepEvidence, ...]:
    normalized_action_ids = None if action_ids is None else tuple(sorted(action_ids))
    if normalized_action_ids == ():
        return ()

    # A raw-TEXT index cannot serve the canonical UTC sort computed at cutover.
    query = """
        SELECT
            steps.step_id, steps.action_id, steps.user_id, steps.status,
            steps.step_number, steps.local_step_number, steps.short_step_id,
            steps.created_at, steps.user_message_id, steps.user_message_json,
            steps.user_request_text,
            actions.action_id, actions.user_id, actions.initial_user_message_id,
            actions.suggestion_id, steps.goal_handle
        FROM agent_action_steps AS steps
        LEFT JOIN agent_actions AS actions ON actions.action_id = steps.action_id
        WHERE steps.step_type = 'user_request'
        """
    parameters: tuple[str, ...] = ()
    if normalized_action_ids is not None:
        placeholders = ", ".join("?" for _ in normalized_action_ids)
        query += f" AND steps.action_id IN ({placeholders})"
        parameters = normalized_action_ids
    rows = connection.execute(query, parameters).fetchall()
    ordered_rows = sorted(
        (
            (
                _required_identifier(row[1], field="step_action_id"),
                _positive_integer(row[4], field="step_number"),
                _required_timestamp(row[7], field="created_at"),
                _required_identifier(row[0], field="step_id"),
                row,
            )
            for row in rows
        ),
        key=lambda item: item[:4],
    )
    accepted_sequences: dict[str, int] = {}
    inventory: list[ActionUserStepEvidence] = []
    for action_id, step_number, created_at, step_id, row in ordered_rows:
        user_id = _required_identifier(row[2], field="step_user_id")
        owning_action_id = _required_identifier(row[11], field="action_id")
        owning_user_id = _required_identifier(row[12], field="action_user_id")
        if (action_id, user_id) != (owning_action_id, owning_user_id):
            raise MigrationError(f"USER step ownership is inconsistent: {step_id}")
        if row[3] != "success":
            raise MigrationError(f"Action USER step is not successful: {step_id}")
        local_step_number = _positive_integer(row[5], field="local_step_number")
        short_step_id = _required_identifier(row[6], field="short_step_id")
        goal_handle = _required_identifier(row[15], field="goal_handle")
        if (
            goal_handle != "S"
            or short_step_id != f"{goal_handle}-{local_step_number}-USER"
        ):
            raise MigrationError(f"USER step short identity is inconsistent: {step_id}")
        user_message_id = _optional_identifier(row[8], field="user_message_id")
        user_message_json = _optional_text(row[9], field="user_message_json")
        if (user_message_id is None) != (user_message_json is None):
            raise MigrationError(f"USER step typed message pair is invalid: {step_id}")
        accepted_sequence = accepted_sequences.get(action_id, 0) + 1
        accepted_sequences[action_id] = accepted_sequence
        inventory.append(
            ActionUserStepEvidence(
                step_id=step_id,
                action_id=action_id,
                user_id=user_id,
                accepted_sequence=accepted_sequence,
                step_number=step_number,
                local_step_number=local_step_number,
                short_step_id=short_step_id,
                created_at=created_at,
                initial_user_message_id=_required_identifier(
                    row[13], field="initial_user_message_id"
                ),
                suggestion_id=_optional_identifier(row[14], field="suggestion_id"),
                user_message_id=user_message_id,
                user_message_json=user_message_json,
                user_request_text=_required_text(row[10], field="user_request_text"),
            )
        )
    return tuple(inventory)


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MigrationError(f"Action USER step inventory has invalid {field}")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    return None if value is None else _required_text(value, field=field)


def _optional_identifier(value: object, *, field: str) -> str | None:
    return None if value is None else _required_identifier(value, field=field)


def _required_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MigrationError(f"Action USER step inventory has invalid {field}")
    return value


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"Action USER step inventory has invalid {field}")
    return value


def _required_timestamp(value: object, *, field: str) -> str:
    text = _required_text(value, field=field)
    if _TIMESTAMP_PATTERN.fullmatch(text) is None:
        raise MigrationError(f"Action USER step inventory has invalid {field}")
    try:
        return normalize_iso8601_utc_z_microseconds(text)
    except ValueError as exc:
        raise MigrationError(f"Action USER step inventory has invalid {field}") from exc


__all__ = ["ActionUserStepEvidence", "load_action_user_step_inventory"]
