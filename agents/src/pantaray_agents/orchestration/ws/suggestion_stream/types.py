"""Shared types and normalization helpers for suggestion stream handling."""

from __future__ import annotations

from typing import Literal, TypedDict

from pantaray_agents.orchestration.common.types import JSONValue
from pantaray_agents.utils.ws_observability import CircuitBreaker

SuggestionTerminalStatus = Literal[
    "processing", "success", "error", "timeout", "canceled"
]
SuggestionDeliveryState = Literal["held", "released", "expired", "superseded"]


class SuggestionTerminalRow(TypedDict, total=False):
    status: SuggestionTerminalStatus
    has_suggestion: bool | None
    answer: str | None
    error: JSONValue | None
    interaction_contract: str | None
    delivery_state: SuggestionDeliveryState | None


SUGGESTION_DB_DEPENDENCY = "suggestion_repository"
SUGGESTION_DB_GET_OP = "get_suggestion"
SUGGESTION_DB_FAILURE_THRESHOLD = 5
SUGGESTION_DB_CIRCUIT_OPEN_SECONDS = 30.0
SUGGESTION_DB_FAILURE_LOG_INTERVAL_SECONDS = 60.0

suggestion_db_circuit = CircuitBreaker(
    failure_threshold=SUGGESTION_DB_FAILURE_THRESHOLD,
    open_interval_seconds=SUGGESTION_DB_CIRCUIT_OPEN_SECONDS,
)


class SuggestionRowFetchError(RuntimeError):
    """Raised when suggestion DB polling keeps failing."""


def coerce_json_value(raw: object) -> JSONValue | None:
    if raw is None or isinstance(raw, str | int | float | bool):
        return raw
    if isinstance(raw, list):
        coerced_items: list[JSONValue] = []
        for item in raw:
            coerced_item = coerce_json_value(item)
            if coerced_item is None and item is not None:
                return None
            coerced_items.append(coerced_item)
        return coerced_items
    if isinstance(raw, dict):
        payload: dict[str, JSONValue] = {}
        for key, value in raw.items():
            coerced_value = coerce_json_value(value)
            if coerced_value is None and value is not None:
                return None
            payload[str(key)] = coerced_value
        return payload
    return None


def coerce_suggestion_terminal_row(raw: object) -> SuggestionTerminalRow | None:
    if not isinstance(raw, dict) or not raw:
        return None
    status_raw = raw.get("status")
    if not isinstance(status_raw, str):
        return None
    status = status_raw.strip().lower()
    if status == "processing":
        normalized_status: SuggestionTerminalStatus = "processing"
    elif status == "success":
        normalized_status = "success"
    elif status == "error":
        normalized_status = "error"
    elif status == "timeout":
        normalized_status = "timeout"
    elif status == "canceled":
        normalized_status = "canceled"
    else:
        return None

    row: SuggestionTerminalRow = {"status": normalized_status}

    has_suggestion = raw.get("has_suggestion")
    if has_suggestion is None or isinstance(has_suggestion, bool):
        row["has_suggestion"] = has_suggestion

    answer = raw.get("answer")
    if answer is None or isinstance(answer, str):
        row["answer"] = answer

    error_raw = raw.get("error")
    error_value = coerce_json_value(error_raw)
    if error_value is not None or error_raw is None:
        row["error"] = error_value

    interaction_contract = raw.get("interaction_contract")
    if interaction_contract is None or isinstance(interaction_contract, str):
        row["interaction_contract"] = interaction_contract

    delivery_state = raw.get("delivery_state")
    if delivery_state in (None, "held", "released", "expired", "superseded"):
        row["delivery_state"] = delivery_state

    return row
