from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from pantaray_agents.schema.action_conversation import (
    ActionConversationPage,
    ActionConversationSummary,
    ActionMessageAcceptedEventData,
    ActionMessageAdoptedEventData,
    ActionRun,
    ActionStepEventData,
    PublicActionError,
    ToolEntry,
    UserEntry,
    parse_action_step_event,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    ACTION_MESSAGE_MAX_IMAGES,
)
from pantaray_agents.schema.agent.image import ImageInput

STARTED_AT = "2026-08-28T00:00:00.000000Z"
COMPLETED_AT = "2026-08-28T00:01:00.000000Z"
ACTION_ERROR = {"code": "action_failed", "message": "Could not complete"}


@pytest.mark.parametrize(
    "invalid",
    [
        {"status": "processing"},
        {"content": "private"},
        {"tool_args": {"content": "private"}},
        {"step_kind": "unknown"},
    ],
)
def test_assistant_event_is_identity_only_and_requires_durable_success(invalid):
    payload = {
        "action_id": "action-1",
        "process_id": "process-1",
        "step_kind": "assistant",
        "step_id": "step-1",
        "step_number": 2,
        "status": "success",
    }
    assert parse_action_step_event(payload).step_kind == "assistant"
    with pytest.raises(ValidationError):
        parse_action_step_event({**payload, **invalid})


def _step_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_id": "action-1",
        "process_id": "process-1",
        "step_kind": "tool",
        "step_id": "step-tool-1",
        "step_number": 1,
        "tool_id": "history_fetch",
        "label": "Fetch history",
        "status": "processing",
        "started_at": STARTED_AT,
        "completed_at": None,
    }
    payload.update(overrides)
    return payload


def _user_payload(number: int = 1, **overrides: object) -> dict[str, object]:
    status = overrides.get("status", "adopted")
    payload: dict[str, object] = {
        "step_kind": "user",
        "approved_suggestion": None,
        "step_id": f"step-user-{number}",
        "step_number": number if status == "adopted" else None,
        "message_id": f"message-{number}",
        "accepted_sequence": number,
        "content": "Do the work",
        "images": (),
        "project_refs": (),
        "files": (),
        "status": "adopted",
    }
    payload.update(overrides)
    return payload


def _tool_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "step_kind": "tool",
        "step_id": "step-tool-1",
        "step_number": 2,
        "label": "Read a file",
        "status": "success",
        "outcome": "completed",
        "subject": "src/app.py",
        "output_preview": None,
        "output_available": True,
        "images": (),
    }
    payload.update(overrides)
    return payload


def _run_payload(status: str = "success", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "process-1",
        "status": status,
        "started_at": STARTED_AT,
        "completed_at": (
            COMPLETED_AT if status in {"success", "error", "canceled"} else None
        ),
        "completion_event_id": (
            "event-1" if status in {"success", "error", "canceled"} else None
        ),
        "entries": (),
        "final_output": "Done" if status == "success" else None,
        "error": ACTION_ERROR if status in {"error", "canceled"} else None,
    }
    payload.update(overrides)
    return payload


def _summary_payload(status: str = "success", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_id": "action-1",
        "suggestion_id": None,
        "approved_suggestion": None,
        "status": status,
        "latest_run_id": "process-1",
        "resumable": False,
    }
    payload.update(overrides)
    return payload


def _page_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": _summary_payload(),
        "runs": (_run_payload(),),
        "unadopted_messages": (),
        "next_cursor": "opaque-cursor",
    }
    payload.update(overrides)
    return payload


def test_message_events_expose_only_approved_identity() -> None:
    accepted = ActionMessageAcceptedEventData(
        action_id="action-1",
        message_id="message-1",
        step_id="step-user-1",
    )
    adopted = ActionMessageAdoptedEventData(
        action_id="action-1",
        message_id="message-1",
        step_id="step-user-1",
        process_id="process-1",
    )

    assert accepted.model_dump() == {
        "action_id": "action-1",
        "message_id": "message-1",
        "step_id": "step-user-1",
    }
    assert adopted.model_dump() == {
        **accepted.model_dump(),
        "process_id": "process-1",
    }


@pytest.mark.parametrize("status", ["processing", "success", "error", "timeout"])
def test_action_step_accepts_every_public_status(status: str) -> None:
    completed_at = None if status == "processing" else COMPLETED_AT

    event = ActionStepEventData.model_validate(
        _step_payload(status=status, completed_at=completed_at)
    )

    assert event.status == status
    assert event.completed_at == completed_at


def test_action_step_normalizes_timezone_aware_timestamps_to_utc_z() -> None:
    event = ActionStepEventData.model_validate(
        _step_payload(
            status="success",
            started_at="2026-08-28T09:00:00+09:00",
            completed_at="2026-08-28T09:01:00+09:00",
        )
    )

    assert event.started_at == STARTED_AT
    assert event.completed_at == COMPLETED_AT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", "not-a-timestamp"),
        ("started_at", "2026-08-28T00:00:00"),
        ("completed_at", "not-a-timestamp"),
        ("completed_at", "2026-08-28T00:01:00"),
    ],
)
def test_action_step_rejects_invalid_or_timezone_naive_timestamps(
    field: str,
    value: str,
) -> None:
    payload = _step_payload(status="success", completed_at=COMPLETED_AT)
    payload[field] = value

    with pytest.raises(ValidationError):
        ActionStepEventData.model_validate(payload)


@pytest.mark.parametrize("status", ["success", "error", "timeout"])
def test_terminal_action_step_requires_completed_at(status: str) -> None:
    with pytest.raises(ValidationError) as captured:
        ActionStepEventData.model_validate(
            _step_payload(status=status, completed_at=None)
        )

    assert "action_step_completion_state_invalid" in str(captured.value)


def test_processing_action_step_rejects_completed_at() -> None:
    with pytest.raises(ValidationError) as captured:
        ActionStepEventData.model_validate(_step_payload(completed_at=COMPLETED_AT))

    assert "action_step_completion_state_invalid" in str(captured.value)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ActionMessageAcceptedEventData.model_validate(
            {
                "action_id": " action-1",
                "message_id": "message-1",
                "step_id": "step-user-1",
            }
        ),
        lambda: ActionMessageAdoptedEventData.model_validate(
            {
                "action_id": "action-1",
                "message_id": "message-1 ",
                "step_id": "step-user-1",
                "process_id": "process-1",
            }
        ),
        lambda: ActionStepEventData.model_validate(
            _step_payload(process_id=" process-1")
        ),
        lambda: ActionStepEventData.model_validate(_step_payload(tool_id="\t")),
    ],
)
def test_event_identities_reject_blank_or_outer_whitespace(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError) as captured:
        factory()

    assert "action_conversation_identity_not_canonical" in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in _step_payload().items() if key != "step_kind"},
        _step_payload(step_kind="llm"),
    ],
)
def test_action_step_requires_tool_discriminant(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ActionStepEventData.model_validate(payload)


@pytest.mark.parametrize("step_number", [0, -1, "1"])
def test_action_step_requires_strict_positive_step_number(step_number: object) -> None:
    with pytest.raises(ValidationError):
        ActionStepEventData.model_validate(_step_payload(step_number=step_number))


@pytest.mark.parametrize("private_field", ["content", "images"])
def test_message_event_rejects_private_body_fields(private_field: str) -> None:
    payload: dict[str, object] = {
        "action_id": "action-1",
        "message_id": "message-1",
        "step_id": "step-user-1",
        private_field: "private",
    }

    with pytest.raises(ValidationError) as captured:
        ActionMessageAcceptedEventData.model_validate(payload)

    assert "extra_forbidden" in str(captured.value)


@pytest.mark.parametrize(
    "private_field",
    [
        "thinking",
        "tool_input",
        "tool_output",
        "prompt",
        "raw_response",
        "completion_chunk",
        "command_id",
    ],
)
def test_action_step_rejects_private_and_legacy_fields(private_field: str) -> None:
    with pytest.raises(ValidationError) as captured:
        ActionStepEventData.model_validate(_step_payload(**{private_field: "private"}))

    assert "extra_forbidden" in str(captured.value)


def test_event_validation_errors_hide_private_identifiers() -> None:
    with pytest.raises(ValidationError) as captured:
        ActionMessageAcceptedEventData.model_validate(
            {
                "action_id": ["raw-private-action-id"],
                "message_id": ["raw-private-message-id"],
                "step_id": ["raw-private-step-id"],
            }
        )

    assert "raw-private" not in str(captured.value)


def test_event_models_are_frozen() -> None:
    accepted = ActionMessageAcceptedEventData(
        action_id="action-1",
        message_id="message-1",
        step_id="step-user-1",
    )

    with pytest.raises(ValidationError):
        accepted.action_id = "changed"


@pytest.mark.parametrize("status", ["processing", "success", "error", "timeout"])
def test_read_tool_accepts_each_public_status(status: str) -> None:
    tool = ToolEntry.model_validate(
        _tool_payload(status=status, output_available=False)
    )

    assert tool.status == status


def test_page_json_accepts_paused_multi_steer_fragment_and_history() -> None:
    run = _run_payload(
        "approval_pending",
        entries=(
            *(_user_payload(number) for number in (6, 5)),
            *(_user_payload(number, message_id=None) for number in (4, 3)),
            _tool_payload(),
        ),
    )
    page = ActionConversationPage.model_validate_json(
        json.dumps(
            _page_payload(
                action=_summary_payload("processing"),
                runs=(run,),
                unadopted_messages=tuple(
                    _user_payload(number, status="pending") for number in (7, 9)
                ),
            )
        )
    )

    assert isinstance(page.runs[0].entries[4], ToolEntry)
    assert page.runs[0].entries[2].message_id is None


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        ("running", {"completion_event_id": "event-1"}),
        ("approval_pending", {"completion_event_id": "event-1"}),
        ("success", {"completion_event_id": None}),
        ("error", {"completion_event_id": None}),
        ("canceled", {"completion_event_id": None}),
        ("running", {"completed_at": COMPLETED_AT}),
        ("running", {"final_output": "unexpected"}),
        ("approval_pending", {"error": ACTION_ERROR}),
        ("success", {"completed_at": None}),
        ("success", {"final_output": None}),
        ("success", {"error": ACTION_ERROR}),
        ("error", {"completed_at": None}),
        ("error", {"error": None}),
        ("error", {"final_output": "unexpected"}),
        ("canceled", {"error": None}),
    ],
)
def test_read_run_rejects_open_or_conflicting_outcomes(
    status: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="run_outcome_invalid"):
        ActionRun.model_validate(_run_payload(status, **overrides))


def test_success_run_requires_nonblank_final_output() -> None:
    with pytest.raises(ValidationError, match="action_conversation_blank"):
        ActionRun.model_validate(_run_payload(final_output=" \t "))


@pytest.mark.parametrize("suggestion_id", [None, "another-suggestion"])
def test_summary_preserves_approval_without_loading_its_run_and_rejects_wrong_owner(
    suggestion_id: str | None,
) -> None:
    proposal = {"suggestion_id": "suggestion-1", "content": "Review the changes?"}
    payload = _page_payload(
        action=_summary_payload(
            suggestion_id="suggestion-1", approved_suggestion=proposal
        ),
        runs=(),
    )
    page = ActionConversationPage.model_validate(payload)
    assert page.model_dump(mode="json")["action"]["approved_suggestion"] == proposal
    payload["action"] = _summary_payload(
        suggestion_id=suggestion_id, approved_suggestion=proposal
    )
    with pytest.raises(ValidationError, match="approved_suggestion_relation_invalid"):
        ActionConversationPage.model_validate(payload)


@pytest.mark.parametrize("comment", [None, "Include the risks."])
def test_approval_entry_keeps_proposal_separate_from_optional_user_comment(
    comment: str | None,
) -> None:
    proposal = {"suggestion_id": "suggestion-1", "content": "Review the changes?"}
    entry = UserEntry.model_validate(
        _user_payload(content=comment, approved_suggestion=proposal)
    )
    payload = entry.model_dump(mode="json")
    assert payload["content"] == comment
    assert payload["approved_suggestion"] == proposal


@pytest.mark.parametrize(
    "overrides",
    [
        {"content": None, "approved_suggestion": None},
        {"approved_suggestion": {"suggestion_id": "", "content": "Proposal"}},
        {"approved_suggestion": {"suggestion_id": "suggestion-1", "content": " "}},
        {
            "approved_suggestion": {
                "suggestion_id": "suggestion-1",
                "content": "Proposal",
                "organization_name": "private provenance",
            }
        },
    ],
)
def test_approval_entry_rejects_empty_content_or_private_provenance(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        UserEntry.model_validate(_user_payload(**overrides))


@pytest.mark.parametrize("status", ["adopted", "not_executed"])
@pytest.mark.parametrize("suggestion_id", [None, "another-suggestion"])
def test_page_rejects_approval_from_another_action(
    status: str,
    suggestion_id: str | None,
) -> None:
    entry = _user_payload(
        status=status,
        content=None,
        approved_suggestion={"suggestion_id": "suggestion-1", "content": "Proposal"},
    )
    payload = _page_payload(
        action=_summary_payload(suggestion_id="suggestion-1"),
        runs=(_run_payload(entries=(entry,) if status == "adopted" else ()),),
        unadopted_messages=() if status == "adopted" else (entry,),
    )
    ActionConversationPage.model_validate(payload)
    payload["action"] = _summary_payload(suggestion_id=suggestion_id)
    with pytest.raises(ValidationError, match="approved_suggestion_relation_invalid"):
        ActionConversationPage.model_validate(payload)


@pytest.mark.parametrize("status", ["pending", "not_executed"])
def test_unadopted_user_status_requires_message_identity(status: str) -> None:
    with pytest.raises(ValidationError, match="message_identity_missing"):
        UserEntry.model_validate(_user_payload(status=status, message_id=None))


@pytest.mark.parametrize(
    ("status", "step_number"),
    [("adopted", None), ("pending", 1), ("not_executed", 1)],
)
def test_user_timeline_position_matches_adoption_status(
    status: str, step_number: int | None
) -> None:
    with pytest.raises(ValidationError, match="user_timeline_position_invalid"):
        UserEntry.model_validate(_user_payload(status=status, step_number=step_number))


def test_page_rejects_user_in_wrong_adoption_group() -> None:
    page = _page_payload(unadopted_messages=(_user_payload(status="adopted"),))
    with pytest.raises(ValidationError, match="user_placement_invalid"):
        ActionConversationPage.model_validate(page)


@pytest.mark.parametrize("status", ["queued", "processing"])
def test_active_summary_requires_latest_run_identity(status: str) -> None:
    with pytest.raises(ValidationError, match="action_conversation_latest_run_missing"):
        ActionConversationSummary.model_validate(
            _summary_payload(status, latest_run_id=None)
        )


@pytest.mark.parametrize(
    ("action_status", "run_status"),
    [
        ("success", "error"),
        ("error", "canceled"),
        ("canceled", "success"),
        ("success", "running"),
        ("processing", "success"),
    ],
)
def test_page_rejects_terminal_latest_run_status_mismatch(
    action_status: str,
    run_status: str,
) -> None:
    with pytest.raises(ValidationError, match="latest_status_mismatch"):
        ActionConversationPage.model_validate(
            _page_payload(
                action=_summary_payload(action_status),
                runs=(_run_payload(run_status),),
            )
        )


@pytest.mark.parametrize(
    "page",
    [
        *(
            _page_payload(
                action=_summary_payload(latest_run_id=latest_run_id),
                runs=(_run_payload("error", run_id="process-older"),),
            )
            for latest_run_id in (None, "process-latest")
        ),
        _page_payload(
            action=_summary_payload("queued"),
            runs=(
                _run_payload("running"),
                _run_payload("error", run_id="older", completed_at=STARTED_AT),
            ),
            unadopted_messages=(_user_payload(status="pending"),),
        ),
        _page_payload(
            runs=(
                _run_payload(started_at=COMPLETED_AT),
                _run_payload("error", run_id="process-older"),
            ),
            unadopted_messages=(_user_payload(status="not_executed"),),
        ),
    ],
)
def test_page_accepts_valid_summary_run_combinations(page: dict[str, object]) -> None:
    ActionConversationPage.model_validate(page)


@pytest.mark.parametrize(
    ("page", "error_code"),
    [
        (
            _page_payload(
                action=_summary_payload("queued", latest_run_id="process-1"),
                runs=(_run_payload("approval_pending"),),
            ),
            "action_conversation_latest_status_mismatch",
        ),
        (
            _page_payload(runs=(_run_payload(), _run_payload("error"))),
            "action_conversation_run_identity_duplicate",
        ),
        (
            _page_payload(
                action=_summary_payload("processing", latest_run_id="process-latest"),
                runs=(_run_payload("approval_pending", run_id="process-older"),),
            ),
            "action_conversation_nonlatest_run_invalid",
        ),
        (
            _page_payload(unadopted_messages=(_user_payload(status="pending"),)),
            "action_conversation_user_placement_invalid",
        ),
        (
            _page_payload(
                runs=(
                    _run_payload(),
                    _run_payload("error", run_id="process-older"),
                )
            ),
            "action_conversation_nonlatest_run_invalid",
        ),
        (
            _page_payload(
                runs=(
                    _run_payload("error", run_id="process-older-1"),
                    _run_payload("error", run_id="process-older-2"),
                    _run_payload(started_at=COMPLETED_AT),
                ),
            ),
            "action_conversation_nonlatest_run_invalid",
        ),
    ],
)
def test_page_rejects_cross_run_inconsistency(
    page: dict[str, object], error_code: str
) -> None:
    with pytest.raises(ValidationError, match=error_code):
        ActionConversationPage.model_validate(page)


@pytest.mark.parametrize(
    ("field", "error_code"),
    [
        ("step_id", "action_conversation_step_identity_duplicate"),
        ("message_id", "action_conversation_message_identity_duplicate"),
        ("accepted_sequence", "action_conversation_accepted_sequence_duplicate"),
    ],
)
def test_page_rejects_duplicate_user_identity(field: str, error_code: str) -> None:
    adopted = _user_payload()
    unadopted = _user_payload(2, status="not_executed")
    unadopted[field] = adopted[field]
    page = _page_payload(
        runs=(_run_payload(entries=(adopted,)),),
        unadopted_messages=(unadopted,),
    )

    with pytest.raises(ValidationError, match=error_code):
        ActionConversationPage.model_validate(page)


def test_read_models_reuse_canonical_identity_and_timestamp_types() -> None:
    with pytest.raises(ValidationError):
        ActionRun.model_validate(_run_payload(completion_event_id=" event-1"))
    timestamp = "2026-08-28T09:00:00+09:00"
    run = ActionRun.model_validate(
        _run_payload(started_at=timestamp, completed_at=timestamp)
    )
    assert run.started_at == STARTED_AT
    assert run.completed_at == STARTED_AT

    with pytest.raises(ValidationError):
        ActionConversationSummary.model_validate(_summary_payload(action_id=" x"))
    with pytest.raises(ValidationError):
        PublicActionError(code=" action_failed", message="Could not complete")
    with pytest.raises(ValidationError):
        ActionRun.model_validate(_run_payload(started_at="2026-08-28T00:00:00"))
    with pytest.raises(ValidationError, match="action_conversation_run_time_invalid"):
        ActionRun.model_validate(_run_payload(completed_at="2026-08-27T23:59:59Z"))


def test_run_requires_step_number_and_step_id_descending_order() -> None:
    tied_newer = _tool_payload(step_id="step-z", step_number=2)
    tied_older = _user_payload(2, step_id="step-a")
    oldest = _tool_payload(step_id="step-oldest", step_number=1)
    ordered = _run_payload(entries=(tied_newer, tied_older, oldest))

    ActionRun.model_validate(ordered)
    with pytest.raises(ValidationError, match="run_entry_order_invalid"):
        ActionRun.model_validate(_run_payload(entries=(tied_older, tied_newer, oldest)))
    with pytest.raises(ValidationError, match="run_entry_order_invalid"):
        ActionRun.model_validate(
            _run_payload(entries=(_user_payload(status="pending"),))
        )


def test_processing_tool_cannot_advertise_terminal_output() -> None:
    with pytest.raises(ValidationError, match="tool_output_invalid"):
        ToolEntry.model_validate(
            _tool_payload(status="processing", output_available=True)
        )


def test_historical_read_does_not_apply_new_ingress_limits() -> None:
    user = UserEntry(
        step_kind="user",
        approved_suggestion=None,
        step_id="step-user-legacy",
        step_number=1,
        message_id=None,
        accepted_sequence=1,
        content="x" * (ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1),
        images=tuple(
            ImageInput(storage_path=f"captures/{index}.png")
            for index in range(ACTION_MESSAGE_MAX_IMAGES + 1)
        ),
        project_refs=(),
        files=(),
        status="adopted",
    )

    assert len(user.content) == ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1
    assert len(user.images) == ACTION_MESSAGE_MAX_IMAGES + 1


@pytest.mark.parametrize(
    "payload",
    [
        _run_payload(
            entries=(
                {
                    key: value
                    for key, value in _tool_payload().items()
                    if key != "step_kind"
                },
            )
        ),
        _run_payload(entries=(_tool_payload(step_kind="llm"),)),
    ],
)
def test_read_entry_discriminant_is_required_and_exact(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ActionRun.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (UserEntry, _user_payload(accepted_sequence=0)),
        (UserEntry, _user_payload(accepted_sequence="1")),
        (UserEntry, _user_payload(step_number=0)),
        (ToolEntry, _tool_payload(step_number=0)),
        (ToolEntry, _tool_payload(output_available="true")),
    ],
)
def test_read_entries_reject_nonpositive_values_and_coercion(
    model: type[UserEntry] | type[ToolEntry],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("cursor", ["opaque-cursor", None])
def test_cursor_is_opaque_nullable_and_nonblank(cursor: str | None) -> None:
    page = ActionConversationPage.model_validate(_page_payload(next_cursor=cursor))
    assert page.next_cursor == cursor

    with pytest.raises(ValidationError):
        ActionConversationPage.model_validate(_page_payload(next_cursor=" \t "))


@pytest.mark.parametrize(
    "private_field",
    """thinking tool_input tool_output prompt provider_response raw_response
    completion_chunk submit_final_answer command_id user_message_json""".split(),
)
def test_read_models_reject_private_and_legacy_fields(private_field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolEntry.model_validate(_tool_payload(**{private_field: "private"}))


def test_read_validation_hides_private_input_and_models_are_frozen() -> None:
    with pytest.raises(ValidationError) as captured:
        UserEntry.model_validate(
            _user_payload(
                step_id=["raw-private-step-id"],
                content=["raw-private-content"],
            )
        )
    assert "raw-private" not in str(captured.value)

    error = PublicActionError(code="action_failed", message="Could not complete")
    with pytest.raises(ValidationError):
        error.code = "changed"


@pytest.mark.parametrize("status", ["queued", "processing", "success", "error"])
def test_only_a_canceled_action_can_be_resumable(status: str) -> None:
    with pytest.raises(ValidationError) as failure:
        ActionConversationSummary.model_validate(
            _summary_payload(status, resumable=True), strict=True
        )

    assert failure.value.errors()[0]["type"] == (
        "action_conversation_resumable_status_invalid"
    )
