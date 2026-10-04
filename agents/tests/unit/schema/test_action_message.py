from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pantaray_agents.agents.suggestion_agent.writer import check_written_answer
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ExistingActionTarget,
    NewActionTarget,
    SubmitActionMessageCommand,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    ACTION_MESSAGE_ID_MAX_CODEPOINTS,
    ACTION_MESSAGE_MAX_IMAGES,
    ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS,
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.websocket.client_messages import ExecuteActionMessage
from pantaray_agents.tasks.action_user_message import parse_action_user_message


def _message_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "message_id": "message-1",
        "content": "Do the work",
        "images": (),
    }
    payload.update(overrides)
    return payload


def test_internal_message_accepts_exact_unicode_code_point_limits() -> None:
    identifier = "e\N{COMBINING ACUTE ACCENT}" * (ACTION_MESSAGE_ID_MAX_CODEPOINTS // 2)
    message = ActionUserMessageInput(
        message_id=identifier,
        content="\N{GRINNING FACE}" * ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    )

    command = SubmitActionMessageCommand(user_id="user-1", message=message)

    assert len(command.message.message_id) == ACTION_MESSAGE_ID_MAX_CODEPOINTS
    assert len(command.message.content) == ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS


def test_suggestion_supplement_uses_its_named_code_point_limit() -> None:
    approval = SuggestionApprovalInput(
        suggestion_id="suggestion-1",
        approved_at="2026-08-16T00:00:00Z",
    )
    accepted = SubmitActionMessageCommand(
        user_id="user-1",
        message=ActionUserMessageInput(
            message_id="message-1",
            content="Do the work",
            suggestion_approval=approval,
            supplement="\N{GRINNING FACE}" * ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS,
        ),
    )

    with pytest.raises(ValidationError) as captured:
        SubmitActionMessageCommand(
            user_id="user-1",
            message=ActionUserMessageInput(
                message_id="message-2",
                content="Do the work",
                suggestion_approval=approval,
                supplement="x" * (ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS + 1),
            ),
        )

    assert len(accepted.message.supplement or "") == (
        ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS
    )
    assert captured.value.errors(include_url=False)[0]["type"] == (
        "action_message_too_long"
    )


def test_non_suggestion_message_rejects_supplement() -> None:
    with pytest.raises(ValidationError) as captured:
        ActionUserMessageInput(
            message_id="message-1",
            content="Do the work",
            supplement="Only touch the requested file.",
        )

    assert captured.value.errors(include_url=False)[0]["type"] == (
        "action_message_not_allowed"
    )


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        ("message_id", "m" * (ACTION_MESSAGE_ID_MAX_CODEPOINTS + 1), "too_long"),
        ("content", "x" * (ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1), "too_long"),
        ("content", " \t ", "blank"),
    ],
)
def test_internal_message_rejects_invalid_bounded_text(
    field: str, value: str, error_type: str
) -> None:
    with pytest.raises(ValidationError) as captured:
        if error_type == "blank":
            ActionUserMessageInput.model_validate(_message_payload(**{field: value}))
        else:
            message = ActionUserMessageInput.model_validate(
                _message_payload(**{field: value})
            )
            SubmitActionMessageCommand(user_id="user-1", message=message)

    detail = captured.value.errors(include_url=False)[0]
    assert detail["loc"] == ((field,) if error_type == "blank" else ("message",))
    assert detail["type"] == f"action_message_{error_type}"


def test_internal_raw_image_limit_precedes_item_validation() -> None:
    accepted = SubmitActionMessageCommand(
        user_id="user-1",
        message=ActionUserMessageInput(
            message_id="message-1",
            content="Do the work",
            images=tuple(
                ImageInput(storage_path=f"captures/{index}.png")
                for index in range(ACTION_MESSAGE_MAX_IMAGES)
            ),
        ),
    )
    oversized = ActionUserMessageInput.model_construct(
        message_id="message-1",
        content="Do the work",
        images=tuple({} for _ in range(ACTION_MESSAGE_MAX_IMAGES + 1)),
    )
    with pytest.raises(ValidationError) as captured:
        SubmitActionMessageCommand(user_id="user-1", message=oversized)

    detail = captured.value.errors(include_url=False)[0]
    assert len(accepted.message.images) == ACTION_MESSAGE_MAX_IMAGES
    assert detail["type"] == "action_message_too_many"
    assert detail["ctx"] == {
        "limit": ACTION_MESSAGE_MAX_IMAGES,
        "unit": "image_references",
    }


def test_durable_v1_reader_preserves_pre_limit_message() -> None:
    payload = _message_payload(
        content="x" * (ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1),
        images=[
            {"kind": "image", "storage_path": f"captures/{index}.png"}
            for index in range(ACTION_MESSAGE_MAX_IMAGES + 1)
        ],
    )

    message = parse_action_user_message(json.dumps(payload))

    assert len(message.content) == ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1
    assert len(message.images) == ACTION_MESSAGE_MAX_IMAGES + 1


def test_internal_durable_json_decodes_image_array_to_tuple() -> None:
    payload = _message_payload(
        images=[{"kind": "image", "storage_path": "captures/1.png"}]
    )

    message = ActionUserMessageInput.model_validate_json(json.dumps(payload))

    assert isinstance(message.images, tuple)
    assert message.images[0].storage_path == "captures/1.png"


def test_internal_python_input_requires_image_tuple() -> None:
    with pytest.raises(ValidationError) as captured:
        ActionUserMessageInput.model_validate(
            _message_payload(images=[ImageInput(storage_path="captures/1.png")])
        )

    assert captured.value.errors(include_url=False)[0]["type"] == "tuple_type"


def test_internal_command_and_targets_use_bounded_ids() -> None:
    too_long = "x" * (ACTION_MESSAGE_ID_MAX_CODEPOINTS + 1)
    message = ActionUserMessageInput(message_id="message-1", content="Do the work")

    with pytest.raises(ValidationError):
        SubmitActionMessageCommand(user_id=too_long, message=message)
    with pytest.raises(ValidationError):
        NewActionTarget(suggestion_id=too_long)
    with pytest.raises(ValidationError):
        ExistingActionTarget(action_id=too_long, expected_process_id=None)
    with pytest.raises(ValidationError):
        ExistingActionTarget(action_id="action-1", expected_process_id=too_long)

    target = ExistingActionTarget(
        action_id=" action-1 ",
        expected_process_id=" process-1 ",
    )
    assert target.action_id == "action-1"
    assert target.expected_process_id == "process-1"


def test_existing_target_requires_explicit_nullable_process_fence() -> None:
    with pytest.raises(ValidationError) as captured:
        ExistingActionTarget.model_validate({"action_id": "action-1"})

    assert captured.value.errors(include_url=False)[0]["loc"] == (
        "expected_process_id",
    )
    assert (
        ExistingActionTarget(
            action_id="action-1", expected_process_id=None
        ).expected_process_id
        is None
    )


def test_internal_command_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError) as captured:
        SubmitActionMessageCommand.model_validate(
            {
                "user_id": "user-1",
                "message": _message_payload(),
                "extra": True,
            }
        )

    assert "extra_forbidden" in {
        detail["type"] for detail in captured.value.errors(include_url=False)
    }


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"approval_mode": "unknown"}, "approval_mode"),
        (
            {
                "images": [{"kind": "image", "storage_path": "image.png"}]
                * (ACTION_MESSAGE_MAX_IMAGES + 1)
            },
            "images",
        ),
    ],
)
def test_execute_action_rejects_invalid_consent_or_image_count(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError) as captured:
        ExecuteActionMessage.model_validate(
            {
                "suggestion_id": "suggestion-1",
                "command_id": "11111111-1111-4111-8111-111111111111",
                "approval_mode": "prompt_each_time",
                "images": [],
                **overrides,
            }
        )
    assert captured.value.errors(include_url=False)[0]["loc"] == (field,)


def test_execute_action_websocket_bounds_suggestion_supplement() -> None:
    with pytest.raises(ValidationError) as captured:
        ExecuteActionMessage.model_validate(
            {
                "suggestion_id": "suggestion-1",
                "command_id": "11111111-1111-4111-8111-111111111111",
                "approval_mode": "prompt_each_time",
                "images": [],
                "supplement": "x" * (ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS + 1),
            }
        )

    assert captured.value.errors(include_url=False)[0]["loc"] == ("supplement",)


def test_suggestion_generation_rejects_unapprovable_answer() -> None:
    with pytest.raises(ValueError, match="more than"):
        check_written_answer("x" * (ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1))
