from __future__ import annotations

import json
from collections.abc import Callable
from typing import Never

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ActionMessageConflictError,
    ActionNotFoundError,
    DeferredActionMessageResult,
    ExistingActionTarget,
    ExpectedProcessConflictError,
    MessageIdentityConflictError,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
)
from pantaray_agents.routers import action_messages as router_module
from pantaray_agents.routers.local.registry import register_local_routers
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    ACTION_MESSAGE_ID_MAX_CODEPOINTS,
    ACTION_MESSAGE_MAX_FILES,
    ACTION_MESSAGE_MAX_IMAGES,
    ACTION_MESSAGE_MAX_PROJECT_REFS,
    ACTION_PROJECT_REF_MAX_PATHS,
    ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS,
)

IMAGE_UUID = "3f861ab4-7b3c-5d90-b520-3424da5bca75"
IMAGE_STORAGE_PATH = f"user-1/2026-09-08/{IMAGE_UUID}.png"


def _client(
    *,
    resolved_user_id: str = "user-1",
    auth_dependency: Callable[[], str] | None = None,
) -> TestClient:
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = auth_dependency or (
        lambda: resolved_user_id
    )
    return TestClient(app)


def _request(
    *, target: dict[str, object] | None = None, message_id: str = "message-1"
) -> dict[str, object]:
    return {
        "target": target or {"kind": "new"},
        "message": {
            "version": 1,
            "message_id": message_id,
            "content": "Do the work",
            "images": [],
            "language": "ja",
        },
    }


def _started(
    command: SubmitActionMessageCommand, *, inserted: bool
) -> StartedActionMessageResult:
    return StartedActionMessageResult(
        disposition="started",
        action_id="action-1",
        message_id=command.message.message_id,
        user_step_id="step-1",
        action_status="queued",
        process_id="process-1",
        job_id="job-1",
        inserted=inserted,
    )


def test_new_existing_and_same_id_replay_use_canonical_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[SubmitActionMessageCommand] = []

    def submit(
        command: SubmitActionMessageCommand,
    ) -> StartedActionMessageResult | DeferredActionMessageResult:
        calls.append(command)
        if command.message.message_id == "message-pending":
            return DeferredActionMessageResult(
                disposition="pending",
                action_id="action-1",
                message_id=command.message.message_id,
                user_step_id="step-2",
                action_status="queued",
                process_id=None,
                job_id=None,
                inserted=True,
            )
        return _started(command, inserted=len(calls) < 3)

    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)
    existing = _request(
        target={
            "kind": "existing",
            "action_id": "action-1",
            "expected_process_id": None,
        }
    )
    with _client() as client:
        new_response = client.post(
            "/v1/agents/users/user-1/actions/messages", json=_request()
        )
        first = client.post("/v1/agents/users/user-1/actions/messages", json=existing)
        replay = client.post("/v1/agents/users/user-1/actions/messages", json=existing)
        pending = client.post(
            "/v1/agents/users/user-1/actions/messages",
            json=_request(target=existing["target"], message_id="message-pending"),
        )

    expected = {
        "action_id": "action-1",
        "message_id": "message-1",
        "step_id": "step-1",
        "disposition": "started",
        "process_id": "process-1",
        "action_status": "queued",
    }
    assert new_response.status_code == first.status_code == replay.status_code == 200
    assert new_response.json() == first.json() == replay.json() == expected
    assert pending.json() == {
        "action_id": "action-1",
        "message_id": "message-pending",
        "step_id": "step-2",
        "disposition": "pending",
        "process_id": None,
        "action_status": "queued",
    }
    assert isinstance(calls[0].target, NewActionTarget)
    assert calls[0].target.suggestion_id is None
    assert isinstance(calls[1].target, ExistingActionTarget)
    assert calls[1].target.expected_process_id is None
    assert calls[1] == calls[2]
    assert calls[1].message.images == ()
    assert calls[1].message.supplement is None
    assert calls[1].message.suggestion_approval is None


def test_http_text_limits_apply_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[SubmitActionMessageCommand] = []

    def submit(command: SubmitActionMessageCommand) -> StartedActionMessageResult:
        commands.append(command)
        return _started(command, inserted=True)

    message_id = "m" * ACTION_MESSAGE_ID_MAX_CODEPOINTS
    content = "c" * ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS
    body = _request(message_id=f" {message_id} ")
    message = body["message"]
    assert isinstance(message, dict)
    message["content"] = f"\n{content}\t"
    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 200
    assert commands[0].message.message_id == message_id
    assert commands[0].message.content == content


def test_existing_target_requires_explicit_nullable_process_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submit = pytest.fail
    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)
    body = _request(target={"kind": "existing", "action_id": "action-1"})

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert response.json() == {
        "type": "ActionMessageValidationError",
        "field": "target.expected_process_id",
        "reason": "invalid",
        "limit": None,
        "unit": None,
    }


def test_openapi_publishes_required_typed_request_body() -> None:
    with _client() as client:
        document = client.get("/openapi.json").json()

    operation = document["paths"]["/v1/agents/users/{user_id}/actions/messages"]["post"]
    request_body = operation["requestBody"]
    schema = request_body["content"]["application/json"]["schema"]
    assert request_body["required"] is True
    assert {"target", "message"} <= schema["properties"].keys()
    assert "$ref" not in json.dumps(schema)
    assert "#/$defs/" not in json.dumps(schema)
    assert "discriminator" not in json.dumps(schema)
    message = schema["properties"]["message"]["properties"]
    content = message["content"]
    message_id = message["message_id"]
    assert content["maxLength"] == ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS
    assert message_id["maxLength"] == ACTION_MESSAGE_ID_MAX_CODEPOINTS
    existing_target = next(
        branch
        for branch in schema["properties"]["target"]["oneOf"]
        if branch["properties"]["kind"]["const"] == "existing"
    )
    action_id = existing_target["properties"]["action_id"]
    assert action_id["maxLength"] == ACTION_MESSAGE_ID_MAX_CODEPOINTS
    for text_schema in (content, message_id, action_id):
        assert text_schema["minLength"] == 1
        assert text_schema["pattern"] == r"\S"
    images = message["images"]
    assert images["maxItems"] == ACTION_MESSAGE_MAX_IMAGES
    assert images["items"]["properties"]["kind"]["const"] == "image"
    responses = document["components"]["schemas"]
    started = responses["ActionMessageHttpStartedResponse"]["properties"]
    deferred = responses["ActionMessageHttpDeferredResponse"]["properties"]
    assert started["disposition"]["const"] == "started"
    assert started["process_id"]["type"] == "string"
    assert set(deferred["disposition"]["enum"]) == {"pending", "not_executed"}
    assert deferred["process_id"]["type"] == "null"
    assert "user-wide idempotency key" in operation["description"]
    assert (
        "same `message_id` and identical target and message" in operation["description"]
    )
    assert "409 `MessageIdentityConflict`" in operation["description"]
    operation_responses = operation["responses"]
    detail_ref = {"$ref": "#/components/schemas/ActionMessageHttpErrorDetail"}
    server_detail_ref = {
        "$ref": "#/components/schemas/ActionMessageHttpServerErrorDetail"
    }
    assert operation_responses["default"]["content"]["application/json"]["schema"][
        "anyOf"
    ] == [detail_ref, server_detail_ref]
    assert operation_responses["401"]["content"]["application/json"]["schema"] == (
        detail_ref
    )
    assert operation_responses["403"]["content"]["application/json"]["schema"] == (
        detail_ref
    )
    assert operation_responses["503"]["content"]["application/json"]["schema"] == (
        server_detail_ref
    )
    assert operation_responses["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActionMessageHttpNotFoundFailure"
    }
    assert (
        responses["ActionMessageHttpNotFoundFailure"]["properties"]["type"]["const"]
        == "ActionNotFound"
    )
    assert operation_responses["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActionMessageHttpConflictFailure"
    }
    assert set(
        responses["ActionMessageHttpConflictFailure"]["properties"]["type"]["enum"]
    ) == {"ActionConflict", "MessageIdentityConflict", "ExpectedProcessConflict"}


@pytest.mark.parametrize("version", [True, 1.0])
def test_message_version_requires_exact_json_integer(
    monkeypatch: pytest.MonkeyPatch, version: bool | float
) -> None:
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["version"] = version
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert response.json() == {
        "type": "ActionMessageValidationError",
        "field": "message.version",
        "reason": "invalid",
        "limit": None,
        "unit": None,
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            _request(),
            {
                "field": "message.content",
                "reason": "too_long",
                "limit": ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
                "unit": "unicode_code_points",
            },
        ),
        (
            _request(),
            {
                "field": "message.images",
                "reason": "too_many",
                "limit": ACTION_MESSAGE_MAX_IMAGES,
                "unit": None,
            },
        ),
    ],
)
def test_message_limits_reject_before_canonical_submit(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object],
    expected: dict[str, object],
) -> None:
    message = body["message"]
    assert isinstance(message, dict)
    if expected["field"] == "message.content":
        message["content"] = "\N{GRINNING FACE}" * (
            ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS + 1
        )
    else:
        message["images"] = [
            {"kind": "image", "storage_path": IMAGE_STORAGE_PATH}
            for _ in range(ACTION_MESSAGE_MAX_IMAGES + 1)
        ]
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert response.json() == {"type": "ActionMessageValidationError", **expected}


# The leading emoji is one code point (two UTF-16 units): "Demo App" starts at 8.
PROJECT_REF_CONTENT = "\N{ROCKET} Check Demo App and Docs"


def _project_ref(
    name: str = "Demo App", start: int = 8, **overrides: object
) -> dict[str, object]:
    ref: dict[str, object] = {
        "project_id": "project-1",
        "display_name": name,
        "paths": ["/workspace/demo-app"],
        "start": start,
        "end": start + len(name),
    }
    ref.update(overrides)
    return ref


def test_project_refs_reach_the_canonical_command_as_code_point_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[SubmitActionMessageCommand] = []

    def submit(command: SubmitActionMessageCommand) -> StartedActionMessageResult:
        commands.append(command)
        return _started(command, inserted=True)

    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    # The spans point into the trimmed content the backend stores.
    message["content"] = f"  {PROJECT_REF_CONTENT}\n"
    message["project_refs"] = [
        _project_ref(),
        _project_ref("Docs", 21, project_id="project-2", paths=[]),
    ]

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 200, response.text
    refs = commands[0].message.project_refs
    assert [(ref.display_name, ref.start, ref.end) for ref in refs] == [
        ("Demo App", 8, 16),
        ("Docs", 21, 25),
    ]
    assert refs[0].paths == ("/workspace/demo-app",)


@pytest.mark.parametrize(
    ("project_refs", "field", "reason"),
    [
        ([_project_ref(start=9)], "message.project_refs", "invalid"),
        ([_project_ref("Docs", 21), _project_ref()], "message.project_refs", "invalid"),
        ([_project_ref(), _project_ref()], "message.project_refs", "invalid"),
        (
            [_project_ref(paths=["workspace/demo-app"])],
            "message.project_refs.0.paths.0",
            "invalid",
        ),
        (
            [_project_ref(paths=["/workspace"] * (ACTION_PROJECT_REF_MAX_PATHS + 1))],
            "message.project_refs.0.paths",
            "too_many",
        ),
        (
            [
                _project_ref(
                    display_name="x" * (ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS + 1)
                )
            ],
            "message.project_refs.0.display_name",
            "too_long",
        ),
        (
            [_project_ref()] * (ACTION_MESSAGE_MAX_PROJECT_REFS + 1),
            "message.project_refs",
            "too_many",
        ),
    ],
)
def test_invalid_project_refs_reject_before_canonical_submit(
    monkeypatch: pytest.MonkeyPatch,
    project_refs: list[dict[str, object]],
    field: str,
    reason: str,
) -> None:
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["content"] = PROJECT_REF_CONTENT
    message["project_refs"] = project_refs

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert (response.json()["field"], response.json()["reason"]) == (field, reason)


def test_scoped_image_references_reach_the_canonical_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[SubmitActionMessageCommand] = []

    def submit(command: SubmitActionMessageCommand) -> StartedActionMessageResult:
        commands.append(command)
        return _started(command, inserted=True)

    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["images"] = [
        {"kind": "image", "storage_path": IMAGE_STORAGE_PATH},
        {"kind": "image", "storage_path": f"user-1/2026-09-08/{IMAGE_UUID}.webp"},
    ]

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 200
    submitted = commands[0].message.images
    assert [image.kind for image in submitted] == ["image", "image"]
    assert submitted[0].storage_path == IMAGE_STORAGE_PATH


@pytest.mark.parametrize(
    "storage_path",
    [
        f"other-user/2026-09-08/{IMAGE_UUID}.png",
        f"user-1/2026-09-08/../../{IMAGE_UUID}.png",
        f"user-1/2025-99-99/{IMAGE_UUID}.png",
        "user-1/2026-09-08/not-a-uuid.png",
        f"user-1/2026-09-08/{IMAGE_UUID}.bmp",
        f"user-1/2026-09-08/{IMAGE_UUID}.svg",
    ],
)
def test_out_of_scope_image_paths_reject_before_canonical_submit(
    monkeypatch: pytest.MonkeyPatch, storage_path: str
) -> None:
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["images"] = [{"kind": "image", "storage_path": storage_path}]

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert response.json() == {
        "type": "ActionMessageValidationError",
        "field": "message.images",
        "reason": "invalid",
        "limit": None,
        "unit": None,
    }


ATTACHMENT_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _file(**overrides: object) -> dict[str, object]:
    return {"attachment_id": ATTACHMENT_ID, "name": "report.pdf", "byte_size": 3} | (
        overrides
    )


def test_attached_files_reach_the_canonical_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[SubmitActionMessageCommand] = []

    def submit(command: SubmitActionMessageCommand) -> StartedActionMessageResult:
        commands.append(command)
        return _started(command, inserted=True)

    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["files"] = [_file()]

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 200
    (submitted,) = commands[0].message.files
    assert (submitted.attachment_id, submitted.name, submitted.byte_size) == (
        ATTACHMENT_ID,
        "report.pdf",
        3,
    )


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ([_file(attachment_id="../escape")], ("message.files.0.attachment_id", None)),
        ([_file(name="notes.txt")], ("message.files.0.name", None)),
        ([_file(name="a/b.pdf")], ("message.files.0.name", None)),
        ([_file(byte_size=0)], ("message.files.0.byte_size", None)),
        (
            [_file() for _ in range(ACTION_MESSAGE_MAX_FILES + 1)],
            ("message.files", ACTION_MESSAGE_MAX_FILES),
        ),
    ],
)
def test_invalid_files_reject_before_canonical_submit(
    monkeypatch: pytest.MonkeyPatch,
    files: list[dict[str, object]],
    expected: tuple[str, int | None],
) -> None:
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["files"] = files

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert (response.json()["field"], response.json()["limit"]) == expected


def test_unavailable_staged_file_is_a_files_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_command: SubmitActionMessageCommand) -> Never:
        raise ActionFileAttachmentUnavailableError("private staging detail")

    monkeypatch.setattr(router_module, "submit_canonical_action_message", fail)
    body = _request()
    message = body["message"]
    assert isinstance(message, dict)
    message["files"] = [_file()]

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert response.json() == {
        "type": "ActionMessageValidationError",
        "field": "message.files",
        "reason": "invalid",
        "limit": None,
        "unit": None,
    }


@pytest.mark.parametrize(
    ("field", "expected_path"),
    [
        ("goal_title", "goal_title"),
        ("supplement", "message.supplement"),
        ("existing", "message.existing"),
    ],
)
def test_legacy_and_internal_fields_are_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    expected_path: str,
) -> None:
    body = _request()
    if field == "goal_title":
        body[field] = "private"
    else:
        message = body["message"]
        assert isinstance(message, dict)
        message[field] = "private"
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)

    with _client() as client:
        response = client.post("/v1/agents/users/user-1/actions/messages", json=body)

    assert response.status_code == 422
    assert response.json() == {
        "type": "ActionMessageValidationError",
        "field": expected_path,
        "reason": "extra_field",
        "limit": None,
        "unit": None,
    }


def test_invalid_json_is_typed_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)
    with _client() as client:
        response = client.post(
            "/v1/agents/users/user-1/actions/messages",
            content=b'{"target":',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "type": "ActionMessageValidationError",
        "field": "$",
        "reason": "invalid",
        "limit": None,
        "unit": None,
    }


def test_path_user_mismatch_precedes_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)
    with _client(resolved_user_id="other-user") as client:
        response = client.post(
            "/v1/agents/users/user-1/actions/messages", json=_request()
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "user_id mismatch"}


def test_auth_503_hides_private_detail() -> None:
    def unavailable() -> Never:
        raise HTTPException(status_code=503, detail="private dependency detail")

    with _client(auth_dependency=unavailable) as client:
        response = client.post(
            "/v1/agents/users/user-1/actions/messages", json=_request()
        )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "HTTP_EXCEPTION_SERVER_ERROR"
    assert "private dependency detail" not in response.text


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_type"),
    [
        (ActionNotFoundError, 404, "ActionNotFound"),
        (ActionMessageConflictError, 409, "ActionConflict"),
        (MessageIdentityConflictError, 409, "MessageIdentityConflict"),
        (ExpectedProcessConflictError, 409, "ExpectedProcessConflict"),
    ],
)
def test_typed_domain_failures_map_to_public_status(
    monkeypatch: pytest.MonkeyPatch,
    error: Callable[[str], Exception],
    expected_status: int,
    expected_type: str,
) -> None:
    def fail(_command: SubmitActionMessageCommand) -> Never:
        raise error("private domain detail")

    monkeypatch.setattr(router_module, "submit_canonical_action_message", fail)
    with _client() as client:
        response = client.post(
            "/v1/agents/users/user-1/actions/messages", json=_request()
        )

    assert response.status_code == expected_status
    assert response.json() == {"type": expected_type}


@pytest.mark.parametrize("mode", ["prompt_each_time", "always_allow"])
def test_new_target_forwards_initial_approval_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    commands: list[SubmitActionMessageCommand] = []

    def submit(command: SubmitActionMessageCommand) -> StartedActionMessageResult:
        commands.append(command)
        return _started(command, inserted=True)

    monkeypatch.setattr(router_module, "submit_canonical_action_message", submit)
    with _client() as client:
        response = client.post(
            "/v1/agents/users/user-1/actions/messages",
            json=_request(target={"kind": "new", "approval_mode": mode}),
        )
    assert response.status_code == 200
    assert isinstance(commands[0].target, NewActionTarget)
    assert commands[0].target.approval_mode == mode


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "new", "approval_mode": "invalid"},
        {
            "kind": "existing",
            "action_id": "action-1",
            "expected_process_id": None,
            "approval_mode": "always_allow",
        },
    ],
)
def test_invalid_initial_mode_is_rejected_before_submission(
    monkeypatch: pytest.MonkeyPatch, target: dict[str, object]
) -> None:
    monkeypatch.setattr(router_module, "submit_canonical_action_message", pytest.fail)
    with _client() as client:
        response = client.post(
            "/v1/agents/users/user-1/actions/messages", json=_request(target=target)
        )
    assert response.status_code == 422
