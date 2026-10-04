from collections.abc import Callable
from dataclasses import replace

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.approval_preparation import (
    build_approval_denied_preparation,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.zanei import (
    RECORDING_UNAVAILABLE_MESSAGE,
)
from pantaray_agents.agents.action_agent.tools import TOOL_REGISTRY
from pantaray_agents.local_runtime.action_conversation.entry_projection import (
    ActionConversationEntryIntegrityError,
    project_action_tool_entry,
    project_action_user_entry,
)
from pantaray_agents.local_runtime.action_conversation.history_queries import (
    ActionHistoryToolRow,
    ActionHistoryUserRow,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_USER_STEP_NAME,
)
from pantaray_agents.schema.action_conversation import ActionStepStatus
from pantaray_agents.schema.agent.action_message import (
    ActionProjectRef,
    ActionUserMessageInput,
    FileAttachmentInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
    serialize_action_user_message,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.tool_result import FormalToolStepOutput


def _modern_user_row(
    supplement: str | None = "Run the focused regression.",
) -> ActionHistoryUserRow:
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Apply the change",
        images=(ImageInput(storage_path="captures/1.png"),),
        supplement=supplement,
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="suggestion-1",
            approved_at="2026-08-30T00:00:00Z",
            summary="Private provenance",
        ),
    )
    return ActionHistoryUserRow(
        step_id="step-1",
        step_number=1,
        accepted_sequence=1,
        user_message_id=message.message_id,
        user_message_json=serialize_action_user_message(message),
        user_request_text=render_action_user_request_text(message),
        adopted_process_id="run-1",
        expected_process_id=None,
        adoption_canceled_at=None,
        step_name=ACTION_USER_STEP_NAME,
    )


def _tool_output(
    *, status: str = "success", output: object = "done", storage: str = "inline_json"
) -> str:
    return FormalToolStepOutput.model_validate(
        {
            "schema_version": 1,
            "status": status,
            "output": output,
            "output_storage_kind": storage,
            "output_owner_kind": "action_step",
        }
    ).model_dump_json()


@pytest.mark.parametrize("supplement", [None, "Run the focused regression."])
def test_projects_proposal_separately_without_internal_suggestion_metadata(
    supplement: str | None,
) -> None:
    entry = project_action_user_entry(_modern_user_row(supplement))

    assert entry.status == "adopted"
    assert entry.step_number == 1
    assert entry.content == supplement
    assert entry.model_dump(mode="json")["approved_suggestion"] == {
        "suggestion_id": "suggestion-1",
        "content": "Apply the change",
    }
    assert entry.images[0].storage_path == "captures/1.png"


_DEMO_REF = ActionProjectRef(
    project_id="project-1",
    display_name="Demo App",
    paths=("/workspace/demo-app",),
    start=6,
    end=14,
)


@pytest.mark.parametrize(
    "message",
    [
        ActionUserMessageInput(
            message_id="message-1",
            content="Check Demo App",
            project_refs=(_DEMO_REF,),
        ),
        ActionUserMessageInput(
            message_id="message-1",
            content="Apply the change",
            supplement="Check Demo App",
            supplement_project_refs=(_DEMO_REF,),
            suggestion_approval=SuggestionApprovalInput(
                suggestion_id="suggestion-1", approved_at="2026-08-30T00:00:00Z"
            ),
        ),
    ],
)
def test_project_refs_follow_the_text_the_entry_shows(
    message: ActionUserMessageInput,
) -> None:
    row = replace(
        _modern_user_row(),
        user_message_json=serialize_action_user_message(message),
        user_request_text=render_action_user_request_text(message),
    )

    entry = project_action_user_entry(row)

    assert entry.content == "Check Demo App"
    assert entry.model_dump(mode="json")["project_refs"] == [
        {"display_name": "Demo App", "start": 6, "end": 14}
    ]


def test_attached_files_show_by_name_and_size_without_the_model_note() -> None:
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Summarize this",
        files=(
            FileAttachmentInput(
                attachment_id="0f8fad5b-d9cb-469f-a165-70867728950e",
                name="report.pdf",
                byte_size=2_048,
            ),
        ),
    )
    row = replace(
        _modern_user_row(),
        user_message_json=serialize_action_user_message(message),
        user_request_text=render_action_user_request_text(message),
    )

    entry = project_action_user_entry(row).model_dump(mode="json")

    assert entry["content"] == "Summarize this"
    assert entry["files"] == [{"name": "report.pdf", "byte_size": 2_048}]


def test_ordinary_user_text_is_not_interpreted_as_proposal_metadata() -> None:
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Apply the change\n\nAdditional user conditions:\nKeep this text.",
    )
    row = replace(
        _modern_user_row(),
        user_message_json=serialize_action_user_message(message),
        user_request_text=render_action_user_request_text(message),
    )
    entry = project_action_user_entry(row)
    assert entry.content == message.content
    assert entry.approved_suggestion is None


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            lambda row: replace(row, user_message_id="other"),
            id="message-identity",
        ),
        pytest.param(
            lambda row: replace(row, user_request_text="other"),
            id="request-text",
        ),
        pytest.param(
            lambda row: replace(row, user_message_json=None),
            id="partial-null",
        ),
        pytest.param(
            lambda row: replace(row, user_message_json="{}"),
            id="invalid-json-shape",
        ),
    ],
)
def test_rejects_corrupt_modern_user(
    row: Callable[[ActionHistoryUserRow], ActionHistoryUserRow],
) -> None:
    with pytest.raises(ActionConversationEntryIntegrityError):
        project_action_user_entry(row(_modern_user_row()))


def test_projects_legacy_user_from_durable_text_without_images() -> None:
    entry = project_action_user_entry(
        ActionHistoryUserRow(
            "legacy-step",
            1,
            1,
            None,
            None,
            "Preserved request",
            "run-1",
            None,
            None,
            ACTION_USER_STEP_NAME,
        )
    )

    assert (
        entry.step_number,
        entry.message_id,
        entry.content,
        entry.images,
        entry.status,
    ) == (
        1,
        None,
        "Preserved request",
        (),
        "adopted",
    )


@pytest.mark.parametrize(
    ("canceled_at", "expected"),
    [(None, "pending"), ("2026-08-30T00:00:00Z", "not_executed")],
)
def test_projects_unadopted_user_status(canceled_at: str | None, expected: str) -> None:
    row = replace(
        _modern_user_row(),
        step_number=None,
        adopted_process_id=None,
        adoption_canceled_at=canceled_at,
    )

    entry = project_action_user_entry(row)
    assert (entry.status, entry.step_number) == (expected, None)
    assert entry.content == "Run the focused regression."
    assert entry.approved_suggestion is not None
    assert entry.approved_suggestion.content == "Apply the change"


@pytest.mark.parametrize(
    ("status", "tool_output", "expected"),
    [
        ("processing", _tool_output(status="processing"), False),
        ("success", None, False),
        ("success", _tool_output(output={"content": "done"}), True),
        (
            "success",
            _tool_output(
                output={
                    "storage": "action_file",
                    "path": "tool-results/output.json",
                    "media_type": "application/json",
                    "byte_size": 12,
                    "character_count": 12,
                    "line_count": 1,
                },
                storage="action_file",
            ),
            True,
        ),
        (
            "success",
            _tool_output(
                output={
                    "storage": "action_file",
                    "path": "tool-results/output.bin",
                    "media_type": "application/octet-stream",
                    "byte_size": 12,
                },
                storage="action_file",
            ),
            False,
        ),
    ],
)
def test_projects_tool_output_availability(
    status: ActionStepStatus, tool_output: str | None, expected: bool
) -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow("step-1", 1, "bash", status, tool_output, None, "run-1")
    )

    assert entry.output_available is expected


def _tool_attachment(**overrides: object) -> dict[str, object]:
    attachment: dict[str, object] = {
        "type": "file",
        "source_kind": "local_image_blob",
        "mime_type": "image/png",
        "path": "screen-abc.png",
        "storage_path": "user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png",
        "ref": "tool_attachment:abc",
        "byte_size": 12,
    }
    attachment.update(overrides)
    return attachment


def test_projects_captured_images_from_the_durable_tool_output() -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1",
            1,
            "capture_screen",
            "success",
            _tool_output(
                output={"status": "captured", "attachments": [_tool_attachment()]}
            ),
            None,
            "run-1",
        )
    )

    assert [image.storage_path for image in entry.images] == [
        "user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png"
    ]


def test_projects_every_drawn_pdf_page_as_its_own_image() -> None:
    """One render step attaches several images; the row shows all of them."""

    drawn = [
        f"user-1/2026-09-19/{d * 8}-{d * 4}-4{d * 3}-8{d * 3}-{d * 12}.webp"
        for d in ("2", "3")
    ]
    output = {
        "kind": "pdf_pages",
        "attachments": [
            _tool_attachment(mime_type="image/webp", storage_path=path)
            for path in drawn
        ],
    }

    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1",
            1,
            "render_pdf_page",
            "success",
            _tool_output(output=output),
            None,
            "run-1",
        )
    )

    assert [image.storage_path for image in entry.images] == drawn


@pytest.mark.parametrize(
    "attachment",
    [
        pytest.param(
            {
                "type": "file",
                "mime_type": "text/plain",
                "path": "notes.txt",
                "ref": "tool_attachment:abc",
                "byte_size": 12,
            },
            id="workspace-read-attachment",
        ),
        pytest.param(
            _tool_attachment(mime_type="application/pdf"),
            id="local-blob-that-is-not-an-image",
        ),
        pytest.param(
            _tool_attachment(storage_path="  "),
            id="unnamed-image",
        ),
    ],
)
def test_exposes_no_image_for_a_non_image_tool_attachment(
    attachment: dict[str, object],
) -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1",
            1,
            "read_file",
            "success",
            _tool_output(output={"kind": "attachment", "attachments": [attachment]}),
            None,
            "run-1",
        )
    )

    assert entry.images == ()


@pytest.mark.parametrize(
    "tool_output",
    ["{}", _tool_output(status="success")],
    ids=["invalid-envelope", "status-mismatch"],
)
def test_rejects_corrupt_terminal_tool_output(tool_output: str) -> None:
    with pytest.raises(ActionConversationEntryIntegrityError):
        project_action_tool_entry(
            ActionHistoryToolRow(
                "step-1", 1, "bash", "timeout", tool_output, None, "run-1"
            )
        )


def test_rejects_processing_tool_with_terminal_output() -> None:
    with pytest.raises(ActionConversationEntryIntegrityError):
        project_action_tool_entry(
            ActionHistoryToolRow(
                "step-1", 1, "bash", "processing", _tool_output(), None, "run-1"
            )
        )


def test_projects_the_row_subject_and_result_preview_together() -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1",
            1,
            "read",
            "success",
            _tool_output(output="first line\nsecond line"),
            '{"tool_id":"read","args":{"path":"tests/test_retrieval.py"}}',
            "run-1",
        )
    )

    assert entry.subject == "tests/test_retrieval.py"
    assert entry.output_preview == "first line second line"


@pytest.mark.parametrize(
    ("status", "tool_output"),
    [
        pytest.param(
            "processing", _tool_output(status="processing"), id="still-running"
        ),
        pytest.param(
            "success",
            _tool_output(
                output={
                    "storage": "action_file",
                    "path": "output-0.bin",
                    "media_type": "application/octet-stream",
                    "byte_size": 12,
                },
                storage="action_file",
            ),
            id="binary-attachment",
        ),
    ],
)
def test_previews_no_output_the_page_cannot_read(
    status: ActionStepStatus, tool_output: str
) -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1", 1, "capture_screen", status, tool_output, None, "run-1"
        )
    )

    assert entry.output_preview is None


@pytest.mark.parametrize(
    ("tool_id", "output", "expected"),
    [
        pytest.param(
            "apply_patch",
            {
                "kind": "approval_denied",
                "approval_status": "denied",
                "approval_session_id": "session-1",
                "tool_request_id": "request-1",
                "tool_id": "apply_patch",
                "executed": False,
                "message": "The requested tool call was denied by the user.",
            },
            "denied",
            id="approval-denied",
        ),
        pytest.param(
            "zanei_timeline",
            {
                "status": "recording_unavailable",
                "message": RECORDING_UNAVAILABLE_MESSAGE,
            },
            "unavailable",
            id="recording-unavailable",
        ),
        pytest.param(
            "apply_patch",
            {"kind": "patch_applied", "executed": True},
            "completed",
            id="applied",
        ),
        pytest.param(
            "zanei_timeline",
            {"events": [], "has_more": False},
            "completed",
            id="read",
        ),
        pytest.param("read", "first line", "completed", id="prose-result"),
    ],
)
def test_projects_what_the_tool_actually_did(
    tool_id: str, output: object, expected: str
) -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1", 1, tool_id, "success", _tool_output(output=output), None, "run-1"
        )
    )

    assert (entry.status, entry.outcome) == ("success", expected)


_RENDERER_PREPARING_BODY = {
    "kind": "renderer_preparing",
    "path": "slides.pptx",
    "message": "Continue with read and try again later.",
}


@pytest.mark.parametrize(
    ("status", "output", "expected"),
    [
        pytest.param("success", _RENDERER_PREPARING_BODY, "preparing", id="preparing"),
        pytest.param(
            "error",
            _RENDERER_PREPARING_BODY,
            "completed",
            id="failed-step-is-not-preparing",
        ),
        pytest.param(
            "success",
            {**_RENDERER_PREPARING_BODY, "kind": "pdf_pages"},
            "completed",
            id="drawn-pages",
        ),
    ],
)
def test_projects_a_page_render_waiting_for_its_renderer(
    status: ActionStepStatus, output: object, expected: str
) -> None:
    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1",
            1,
            "render_pdf_page",
            status,
            _tool_output(status=status, output=output),
            '{"tool_id":"render_pdf_page","args":{"path":"slides.pptx","pages":[1]}}',
            "run-1",
        )
    )

    assert (entry.outcome, entry.subject) == (expected, "slides.pptx")


def test_projects_a_denied_approval_the_gated_tool_actually_persists() -> None:
    """The denial the runtime writes must read as denied, field names included."""

    preparation = build_approval_denied_preparation(
        step_id="step-1",
        tool_def=TOOL_REGISTRY["apply_patch"],
        tool_request_id="request-1",
        approval_session_id="session-1",
        requested_at="2026-09-01T00:00:00Z",
    )

    entry = project_action_tool_entry(
        ActionHistoryToolRow(
            "step-1",
            1,
            "apply_patch",
            preparation.result.status,
            _tool_output(output=preparation.result.output),
            '{"tool_id":"apply_patch","args":{"changes":[{"path":"src/app.py"}]}}',
            "run-1",
        )
    )

    assert (entry.status, entry.outcome, entry.subject) == (
        "success",
        "denied",
        "src/app.py",
    )
