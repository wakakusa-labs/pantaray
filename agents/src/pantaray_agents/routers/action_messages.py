from __future__ import annotations

import asyncio
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_REQUEST_TEXT,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ActionMessageConflictError,
    ActionNotFoundError,
    ExistingActionTarget,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    submit_action_message as submit_canonical_action_message,
)
from pantaray_agents.schema.agent.action_message import (
    ActionMessageHttpConflictFailure,
    ActionMessageHttpDefaultErrorDetail,
    ActionMessageHttpDeferredResponse,
    ActionMessageHttpErrorDetail,
    ActionMessageHttpExistingTarget,
    ActionMessageHttpNotFoundFailure,
    ActionMessageHttpRequest,
    ActionMessageHttpResponse,
    ActionMessageHttpServerErrorDetail,
    ActionMessageHttpStartedResponse,
    ActionMessageValidationError,
    ActionMessageValidationReason,
    ActionMessageValidationUnit,
    ActionResumeHttpRequest,
    ActionUserMessageInput,
)
from pantaray_agents.security.storage_paths import validate_image_storage_path

router = APIRouter(prefix="/v1/agents/users", tags=["Action Agent"])

_VALIDATION_REASON_BY_TYPE: dict[str, ActionMessageValidationReason] = {
    "action_message_blank": "blank",
    "action_message_too_long": "too_long",
    "action_message_not_allowed": "not_allowed",
    "action_message_too_many": "too_many",
    "extra_forbidden": "extra_field",
}
_DISCRIMINATOR_LOCATORS = frozenset({"new", "existing"})


class _ImageStoragePathError(ValueError):
    """A submitted image references a path outside the caller's user scope."""


def _inline_local_schema_references(schema: JsonSchemaValue) -> JsonSchemaValue:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)

    def inline(value: object) -> object:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            assert isinstance(reference, str)
            return inline(definitions[reference.removeprefix("#/$defs/")])
        return {
            key: inline(item)
            for key, item in value.items()
            if key not in {"$defs", "discriminator"}
        }

    result = inline(schema)
    assert isinstance(result, dict)
    return result


@router.post(
    "/{user_id}/actions/messages",
    description=(
        "`message_id` is a user-wide idempotency key. After a lost response, "
        "retry with the same `message_id` and identical target and message "
        "(including `expected_process_id` for an existing target). Reusing it "
        "with a different target or message returns 409 `MessageIdentityConflict`."
    ),
    response_model=ActionMessageHttpResponse,
    responses={
        "default": {"model": ActionMessageHttpDefaultErrorDetail},
        status.HTTP_401_UNAUTHORIZED: {"model": ActionMessageHttpErrorDetail},
        status.HTTP_403_FORBIDDEN: {"model": ActionMessageHttpErrorDetail},
        status.HTTP_404_NOT_FOUND: {"model": ActionMessageHttpNotFoundFailure},
        status.HTTP_409_CONFLICT: {"model": ActionMessageHttpConflictFailure},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ActionMessageValidationError},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ActionMessageHttpServerErrorDetail
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _inline_local_schema_references(
                        ActionMessageHttpRequest.model_json_schema()
                    )
                }
            },
        }
    },
)
async def submit_action_message(
    user_id: str,
    request: Request,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionMessageHttpResponse | JSONResponse:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id mismatch",
        )
    try:
        body = ActionMessageHttpRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        failure = _validation_failure(exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=failure.model_dump(mode="json"),
        )

    try:
        command = _canonical_command(user_id=user_id, request=body)
    except _ImageStoragePathError:
        return _message_field_failure("message.images")
    try:
        result = await asyncio.to_thread(submit_canonical_action_message, command)
    except ActionFileAttachmentUnavailableError:
        return _message_field_failure("message.files")
    except ActionNotFoundError:
        return _not_found_failure()
    except ActionMessageConflictError as exc:
        return _conflict_failure(exc)
    return _response(result)


@router.post(
    "/{user_id}/actions/{action_id}/resume",
    description=(
        "Continue the run the user stopped. This is an ordinary follow-up turn: "
        "it opens a new run from the stopped run's checkpoint and carries a "
        "fixed note the model reads, so the conversation shows no message for "
        "it. `message_id` is the same user-wide idempotency key "
        "`POST .../actions/messages` takes. The Action must still be `canceled` "
        "with a restorable checkpoint; otherwise the call returns 409."
    ),
    response_model=ActionMessageHttpResponse,
    responses={
        "default": {"model": ActionMessageHttpDefaultErrorDetail},
        status.HTTP_401_UNAUTHORIZED: {"model": ActionMessageHttpErrorDetail},
        status.HTTP_403_FORBIDDEN: {"model": ActionMessageHttpErrorDetail},
        status.HTTP_404_NOT_FOUND: {"model": ActionMessageHttpNotFoundFailure},
        status.HTTP_409_CONFLICT: {"model": ActionMessageHttpConflictFailure},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ActionMessageValidationError},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ActionMessageHttpServerErrorDetail
        },
    },
)
async def resume_action(
    user_id: str,
    action_id: str,
    request: Request,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionMessageHttpResponse | JSONResponse:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id mismatch",
        )
    try:
        body = ActionResumeHttpRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_validation_failure(exc).model_dump(mode="json"),
        )
    command = SubmitActionMessageCommand(
        user_id=user_id,
        target=ExistingActionTarget(action_id=action_id, expected_process_id=None),
        message=ActionUserMessageInput(
            message_id=body.message_id,
            content=ACTION_RESUME_REQUEST_TEXT,
        ),
        origin="resume",
    )
    try:
        result = await asyncio.to_thread(submit_canonical_action_message, command)
    except ActionNotFoundError:
        return _not_found_failure()
    except ActionMessageConflictError as exc:
        return _conflict_failure(exc)
    return _response(result)


def _canonical_command(
    *, user_id: str, request: ActionMessageHttpRequest
) -> SubmitActionMessageCommand:
    target = request.target
    canonical_target = (
        ExistingActionTarget(
            action_id=target.action_id,
            expected_process_id=target.expected_process_id,
        )
        if isinstance(target, ActionMessageHttpExistingTarget)
        else NewActionTarget(
            approval_mode=target.approval_mode,
            reply_to_suggestion_id=target.reply_to_suggestion_id,
        )
    )
    message = request.message
    for image in message.images:
        try:
            validate_image_storage_path(
                user_id=user_id, storage_path=image.storage_path
            )
        except ValueError as exc:
            raise _ImageStoragePathError(str(exc)) from exc
    return SubmitActionMessageCommand(
        user_id=user_id,
        target=canonical_target,
        message=ActionUserMessageInput(
            version=message.version,
            message_id=message.message_id,
            content=message.content,
            images=message.images,
            language=message.language,
            project_refs=message.project_refs,
            files=message.files,
        ),
    )


def _response(result: SubmitActionMessageResult) -> ActionMessageHttpResponse:
    if isinstance(result, StartedActionMessageResult):
        return ActionMessageHttpStartedResponse(
            action_id=result.action_id,
            message_id=result.message_id,
            step_id=result.user_step_id,
            disposition=result.disposition,
            process_id=result.process_id,
            action_status=result.action_status,
        )
    return ActionMessageHttpDeferredResponse(
        action_id=result.action_id,
        message_id=result.message_id,
        step_id=result.user_step_id,
        disposition=result.disposition,
        process_id=result.process_id,
        action_status=result.action_status,
    )


def _message_field_failure(field: str) -> JSONResponse:
    failure = ActionMessageValidationError(
        field=field,
        reason="invalid",
        limit=None,
        unit=None,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=failure.model_dump(mode="json"),
    )


def _not_found_failure() -> JSONResponse:
    failure = ActionMessageHttpNotFoundFailure(type="ActionNotFound")
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=failure.model_dump(mode="json"),
    )


def _conflict_failure(exc: ActionMessageConflictError) -> JSONResponse:
    failure = ActionMessageHttpConflictFailure(type=exc.failure_type)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=failure.model_dump(mode="json"),
    )


def _validation_failure(exc: ValidationError) -> ActionMessageValidationError:
    error = exc.errors(include_input=False, include_url=False)[0]
    error_type = str(error["type"])
    context = error.get("ctx")
    limit: int | None = None
    unit: ActionMessageValidationUnit | None = None
    if isinstance(context, Mapping):
        raw_limit = context.get("limit")
        raw_unit = context.get("unit")
        limit = raw_limit if isinstance(raw_limit, int) else None
        if raw_unit == "unicode_code_points":
            unit = raw_unit
    locators = tuple(str(value) for value in error["loc"])
    if (
        len(locators) > 1
        and locators[0] == "target"
        and locators[1] in _DISCRIMINATOR_LOCATORS
    ):
        locators = locators[:1] + locators[2:]
    return ActionMessageValidationError(
        field=".".join(locators) or "$",
        reason=_VALIDATION_REASON_BY_TYPE.get(error_type, "invalid"),
        limit=limit,
        unit=unit,
    )
