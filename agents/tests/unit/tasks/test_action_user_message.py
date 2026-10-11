from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.action import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message import (
    ActionProjectRef,
    ActionWorkRef,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    parse_action_user_message as parse_action_user_message_codec,
)
from pantaray_agents.schema.agent.action_message_codec import (
    parse_stored_action_user_message,
    render_action_user_visible_text,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.tasks.action_user_message import (
    parse_action_user_message,
    render_action_user_request_text,
    serialize_action_user_message,
)


def test_action_user_message_round_trip_preserves_typed_approval_and_images() -> None:
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Apply the approved change",
        supplement="Run the focused regression before finishing.",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="suggestion-1",
            approved_at="2026-08-16T00:00:00Z",
            summary="Keep the edit narrow",
            organization_name="Wakakusa",
            project_name="Pantaray",
        ),
        images=(ImageInput(storage_path="captures/1.png"),),
        language="ja",
    )

    serialized = serialize_action_user_message(message)
    parsed = parse_action_user_message(serialized)

    assert parsed.version == 1
    assert parsed.content == message.content
    assert parsed.supplement == message.supplement
    assert parsed.images == message.images
    assert parsed.language == "ja"
    assert json.loads(serialized)["message_id"] == "message-1"
    assert render_action_user_request_text(message) == (
        "Apply the approved change\n\n"
        "Additional user conditions:\n"
        "Run the focused regression before finishing.\n\n"
        "Suggestion metadata:\n"
        "- Suggestion: suggestion-1\n"
        "- Suggestion summary: Keep the edit narrow\n"
        "- Organization: Wakakusa\n"
        "- Project: Pantaray\n"
        "- Approved at: 2026-08-16T00:00:00Z"
    )
    assert render_action_user_visible_text(
        content=message.content, supplement=message.supplement
    ) == (
        "Apply the approved change\n\n"
        "Additional user conditions:\n"
        "Run the focused regression before finishing."
    )


def test_project_refs_render_one_line_per_project_and_round_trip() -> None:
    demo = ActionProjectRef(
        project_id="project-1",
        display_name="Demo App",
        paths=("/workspace/demo-app", "/workspace/demo-api"),
        start=0,
        end=8,
    )
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Apply the approved change",
        supplement="Demo App first, then Notes and Demo App again",
        supplement_project_refs=(
            demo,
            ActionProjectRef(
                project_id="project-2",
                display_name="Notes",
                paths=(),
                start=21,
                end=26,
            ),
            demo.model_copy(update={"start": 31, "end": 39}),
        ),
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="suggestion-1", approved_at="2026-08-16T00:00:00Z"
        ),
    )

    request_text = render_action_user_request_text(message)

    assert request_text.endswith(
        "- Approved at: 2026-08-16T00:00:00Z\n\n"
        "Referenced workspace projects:\n"
        "- Demo App: /workspace/demo-app, /workspace/demo-api\n"
        "- Notes: no folders registered"
    )
    assert (
        parse_stored_action_user_message(
            message_id="message-1",
            message_json=serialize_action_user_message(message),
            user_request_text=request_text,
        )
        == message
    )


def test_work_refs_list_each_task_and_suggestion_once_and_round_trip() -> None:
    report = ActionWorkRef(
        kind="action", id="action-1", title="Q3 report", start=0, end=9
    )
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Q3 report vs Demo App, Q3 report and Try X",
        project_refs=(
            ActionProjectRef(
                project_id="project-1",
                display_name="Demo App",
                paths=(),
                start=13,
                end=21,
            ),
        ),
        work_refs=(
            report,
            report.model_copy(update={"start": 23, "end": 32}),
            ActionWorkRef(
                kind="suggestion", id="suggestion-1", title="Try X", start=37, end=42
            ),
        ),
    )

    request_text = render_action_user_request_text(message)

    assert request_text.endswith(
        "- Demo App: no folders registered\n\n"
        "Referenced work:\n"
        "- task action-1: Q3 report\n"
        "- suggestion suggestion-1: Try X\n"
        "Read one with memory_sql: a task from agent_actions by action_id, a "
        "suggestion from agent_suggestions by suggestion_id."
    )
    assert (
        parse_stored_action_user_message(
            message_id="message-1",
            message_json=serialize_action_user_message(message),
            user_request_text=request_text,
        )
        == message
    )


def test_a_message_without_work_refs_keeps_its_stored_request_text() -> None:
    # Rows written before work_refs existed must still match their stored text.
    message = ActionUserMessageInput(message_id="message-1", content="Plain")

    assert render_action_user_request_text(message) == "Plain"
    assert (
        parse_action_user_message(
            '{"version":1,"message_id":"message-1","content":"Plain","images":[]}'
        ).work_refs
        == ()
    )


@pytest.mark.parametrize(
    ("work_ref", "reason"),
    [
        # The span does not spell the title.
        ({"start": 0, "end": 4, "title": "Q3 r"}, "action_message_invalid"),
        ({"start": 1, "end": 10, "title": "Q3 report"}, "action_message_invalid"),
        # It overlaps the project named at the same place.
        ({"start": 0, "end": 9, "title": "Q3 report"}, "action_message_invalid"),
    ],
)
def test_work_refs_must_name_their_own_span(
    work_ref: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValidationError) as captured:
        ActionUserMessageInput(
            message_id="message-1",
            content="Q3 report",
            project_refs=(
                ActionProjectRef(
                    project_id="project-1", display_name="Q3", paths=(), start=0, end=2
                ),
            ),
            work_refs=(
                ActionWorkRef.model_validate(
                    {"kind": "action", "id": "action-1", **work_ref}
                ),
            ),
        )

    error = captured.value.errors(include_url=False)[0]
    assert (error["loc"], error["type"]) == (("work_refs",), reason)


def test_supplement_work_refs_point_into_the_supplement() -> None:
    approval = SuggestionApprovalInput(
        suggestion_id="suggestion-1", approved_at="2026-08-16T00:00:00Z"
    )
    ref = ActionWorkRef(kind="action", id="action-1", title="Q3", start=0, end=2)

    message = ActionUserMessageInput(
        message_id="message-1",
        content="The approved suggestion",
        supplement="Q3 too",
        supplement_work_refs=(ref,),
        suggestion_approval=approval,
    )
    assert "- task action-1: Q3" in render_action_user_request_text(message)

    with pytest.raises(ValidationError) as captured:
        ActionUserMessageInput(
            message_id="message-1", content="Q3", supplement_work_refs=(ref,)
        )
    assert captured.value.errors(include_url=False)[0]["type"] == (
        "action_message_not_allowed"
    )


ATTACHMENT_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_attached_files_add_a_note_the_visible_text_does_not_carry() -> None:
    message = ActionUserMessageInput(
        message_id="message-1",
        content="Summarize these",
        files=(
            FileAttachmentInput(
                attachment_id=ATTACHMENT_ID, name="Report.PDF", byte_size=1_258_291
            ),
            FileAttachmentInput(
                attachment_id=ATTACHMENT_ID, name="予算 v2.xlsx", byte_size=2_048
            ),
            FileAttachmentInput(
                attachment_id=ATTACHMENT_ID, name="run.ipynb", byte_size=900
            ),
        ),
    )

    assert render_action_user_request_text(message) == (
        "Summarize these\n\n"
        "Attached files:\n"
        "The user attached these files to this message. Each is saved at the path "
        "shown, relative to your workspace cwd. Open one with the `read` tool at "
        "that path; the pages of a PDF, Word, PowerPoint or Excel file can also "
        "be viewed with `render_pdf_page`. These "
        "formats are readable: do not tell the user they are unsupported, and do "
        "not ask them to paste the contents.\n"
        f"- Report.PDF (PDF, 1.2 MB): attachments/{ATTACHMENT_ID}/Report.PDF\n"
        f"- 予算 v2.xlsx (Excel workbook, 2.0 KB): attachments/{ATTACHMENT_ID}/予算 v2.xlsx\n"
        f"- run.ipynb (Jupyter notebook, 900 B): attachments/{ATTACHMENT_ID}/run.ipynb"
    )
    assert render_action_user_visible_text(
        content=message.content, supplement=None
    ) == ("Summarize these")
    assert parse_action_user_message(serialize_action_user_message(message)) == message


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "legacy.doc",
        ".pdf",
        ".hidden.pdf",
        "a/b.pdf",
        "a\\b.pdf",
        "a:b.pdf",
        "tab\there.pdf",
        "nul\x00.pdf",
        "cafe\u0301.pdf",
        "x" * 252 + ".pdf",
    ],
)
def test_attached_file_name_must_be_one_safe_component_of_a_readable_type(
    name: str,
) -> None:
    with pytest.raises(ValidationError, match="attached file name is not allowed"):
        FileAttachmentInput(attachment_id=ATTACHMENT_ID, name=name, byte_size=1)


@pytest.mark.parametrize(
    "attachment_id",
    ["../escape", "0F8FAD5B-D9CB-469F-A165-70867728950E", "not-a-uuid"],
)
def test_attachment_id_must_be_a_canonical_uuid(attachment_id: str) -> None:
    with pytest.raises(ValidationError):
        FileAttachmentInput(attachment_id=attachment_id, name="a.pdf", byte_size=1)


def test_supplement_project_refs_require_a_supplement() -> None:
    with pytest.raises(ValidationError) as captured:
        ActionUserMessageInput(
            message_id="message-1",
            content="Demo App",
            supplement_project_refs=(
                ActionProjectRef(
                    project_id="project-1",
                    display_name="Demo App",
                    paths=(),
                    start=0,
                    end=8,
                ),
            ),
        )

    assert captured.value.errors(include_url=False)[0]["type"] == (
        "action_message_not_allowed"
    )


def test_action_user_message_rejects_untyped_image_shape() -> None:
    with pytest.raises(ValidationError):
        ActionUserMessageInput.model_validate(
            {
                "message_id": "message-1",
                "content": "Do the work",
                "images": [
                    {
                        "kind": "image",
                        "storage_path": "image.png",
                        "app_name": "must-not-be-accepted",
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "message_json",
    (
        "not-json",
        '{"version":2,"message_id":"m1","content":"Do the work","images":[]}',
        '{"version":1,"message_id":"m1","content":"Do the work","images":[],"extra":1}',
    ),
)
def test_parse_action_user_message_rejects_invalid_durable_shape(
    message_json: str,
) -> None:
    with pytest.raises(ValueError):
        parse_action_user_message_codec(message_json)
    with pytest.raises(MigrationError) as exc:
        parse_action_user_message(message_json)
    assert str(exc.value) == "Action USER message does not match V1 schema"
    assert isinstance(exc.value.__cause__, ValidationError)
