import base64

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.action_conversation.cursor_codec import (
    OpaqueCursorError,
)
from pantaray_agents.local_runtime.action_conversation.history_cursor import (
    ConversationHistoryCursor,
    decode_conversation_history_cursor,
    encode_conversation_history_cursor,
)
from pantaray_agents.schema.conversation_history import (
    ConversationHistoryItem,
    ConversationHistoryPage,
    SuggestionHistoryItem,
)


def _conversation(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "conversation",
        "action_id": "action-1",
        "title": "Do the work",
        "updated_at": "2026-08-30T00:00:00.000Z",
        "status": "running",
        "latest_completion_event_id": None,
        "status_line": None,
        "artifact_names": (),
    }
    payload.update(updates)
    return payload


def test_page_discriminates_minimal_strict_items() -> None:
    page = ConversationHistoryPage.model_validate(
        {
            "items": (
                _conversation(),
                {
                    "kind": "suggestion",
                    "suggestion_id": "suggestion-1",
                    "title": "Review this",
                    "updated_at": "2026-08-29T23:00:00.000Z",
                    "status": "approval_pending",
                    "artifact_names": ("outline.md",),
                },
            ),
            "next_cursor": "opaque",
        }
    )

    assert isinstance(page.items[0], ConversationHistoryItem)
    assert isinstance(page.items[1], SuggestionHistoryItem)
    with pytest.raises(ValidationError):
        page.next_cursor = "changed"
    with pytest.raises(ValidationError):
        SuggestionHistoryItem(
            kind="suggestion",
            suggestion_id="suggestion-1",
            title="Review this",
            updated_at="2026-08-30T00:00:00.000Z",
            status="running",  # type: ignore[arg-type]
            artifact_names=(),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"user_id": "private"},
        {"final_output": "private"},
        {"tool_output": "private"},
        {"kind": "tool"},
        {"action_id": " action-1"},
        {"updated_at": "not-a-timestamp"},
        {"updated_at": "2026-08-30T00:00:00.123000Z"},
        {"updated_at": "2026-08-30T09:00:00.123+09:00"},
        {"status": 1},
        {"status_line": " "},
        {"status_line": "x" * 121},
        {"artifact_names": (" ",)},
    ],
)
def test_public_item_rejects_private_and_noncanonical_fields(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ConversationHistoryItem.model_validate(_conversation(**updates))


def test_cursor_round_trip_preserves_search_and_exact_key() -> None:
    updated_at = "2026-08-30T00:00:00.123Z"
    payload = ConversationHistoryCursor(
        user_id="user-1",
        search_text="Ꭰ",
        updated_at=updated_at,
        kind="conversation",
        stable_id="action-1",
    )
    encoded = encode_conversation_history_cursor(payload)
    decoded = decode_conversation_history_cursor(encoded)

    assert "=" not in encoded
    assert decoded == payload
    assert decoded.updated_at == updated_at


def test_history_item_serializes_indexed_millisecond_key_without_rewriting() -> None:
    item = ConversationHistoryItem.model_validate(
        _conversation(updated_at="2026-08-30T00:00:00.123Z")
    )

    assert item.model_dump(mode="json")["updated_at"] == "2026-08-30T00:00:00.123Z"


@pytest.mark.parametrize(
    "payload_json",
    [
        b'{"user_id":"user-1","search_text":"","updated_at":"2026-08-30T00:00:00.123Z","kind":"conversation","stable_id":1}',
        b'{"user_id":"user-1","search_text":" Work ","updated_at":"2026-08-30T00:00:00.123Z","kind":"conversation","stable_id":"action-1"}',
        b'{"user_id":"user-1","search_text":"","updated_at":"2026-08-30T00:00:00.123Z","kind":"conversation","stable_id":"action-1","extra":true}',
        b'{"user_id":"user-1","search_text":"","updated_at":"2026-08-30T00:00:00.123000Z","kind":"conversation","stable_id":"action-1"}',
        b'{"user_id":"user-1","search_text":"","updated_at":"2026-08-30T09:00:00.123+09:00","kind":"conversation","stable_id":"action-1"}',
        b'{"user_id":"user-1","search_text":"","updated_at":"not-a-timestamp","kind":"conversation","stable_id":"action-1"}',
        b'{"user_id":"user-1","search_text":"","updated_at":"0001-01-01T00:00:00.000+23:59","kind":"conversation","stable_id":"action-1"}',
    ],
)
def test_cursor_rejects_untrusted_payload_shapes(payload_json: bytes) -> None:
    encoded = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode()

    with pytest.raises(OpaqueCursorError, match="malformed or non-canonical"):
        decode_conversation_history_cursor(encoded)


def test_cursor_rejects_malformed_and_noncanonical_encoding() -> None:
    payload = ConversationHistoryCursor(
        user_id="user-1",
        search_text="",
        updated_at="2026-08-30T00:00:00.000Z",
        kind="suggestion",
        stable_id="suggestion-1",
    )

    for value in (encode_conversation_history_cursor(payload) + "=", "not*a*cursor"):
        with pytest.raises(OpaqueCursorError, match="malformed or non-canonical"):
            decode_conversation_history_cursor(value)
