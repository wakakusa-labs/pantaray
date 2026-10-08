"""The chat's own tools: start a task, add to one, take up a suggestion.

Each goes through the Action's single entry (``submit_action_message``, or the
shared ``accept_suggestion``) under a key built from the turn's key and the
call's place among the turn's calls of that tool. A turn that runs again after
a crash calls them again under the same keys, so nothing is sent twice: a call
whose key already went in is told what it sent and where, and the model calls
again if this is a different request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from pantaray_agents.agents.chat_agent.turn import ChatTurnPlan
from pantaray_agents.local_runtime.chat.store import (
    chat_turn_message_id,
    read_user_message,
)
from pantaray_agents.local_runtime.chat.work_list import (
    SubmittedMessage,
    read_attachment_holder,
    read_latest_run_process,
    read_submitted_message,
)
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ActionMessageConflictError,
    ActionNotFoundError,
    ExistingActionTarget,
    MessageIdentityConflictError,
    NewActionTarget,
    SubmitActionMessageCommand,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.suggestion_acceptance import (
    SuggestionAccepted,
    accept_suggestion,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
)
from pantaray_agents.local_runtime.tooling.repository import (
    load_effective_approval_preference,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.tools.contract import (
    JsonSchema,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
    tool_error_response,
)

type _Run = Callable[[dict[str, JSONValue], str], Awaitable[ReactToolResult]]

_MESSAGE: dict[str, JSONValue] = {
    "type": "string",
    "description": (
        "The user's request in their own words, as close to what they wrote as "
        "you can; add only what the task cannot know otherwise. What you want to "
        "ask the user goes in your reply to them, not here."
    ),
}
_ATTACHMENTS: dict[str, JSONValue] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Ids of the user's chat messages whose attached images and files the task "
        "needs; [] for none."
    ),
}
# A file goes to one task only: handing it over moves it there.
_HANDED_OVER = (
    "Not done: nothing was sent. A file from those messages already went to one "
    "of your tasks, which keeps it. If this is for that task, send it again "
    "with attachments_from []; otherwise ask the user to attach the file again."
)
_SUCCESS: JsonSchema = {
    "type": "object",
    "required": ["action_id"],
    "properties": {"action_id": {"type": "string"}},
}


def chat_tools(plan: ChatTurnPlan) -> tuple[ReactToolDefinition, ...]:
    """The routing tools of one turn, keyed by that turn."""

    async def start(args: dict[str, JSONValue], key: str) -> ReactToolResult:
        return _submit(
            plan,
            key,
            "start_action",
            NewActionTarget(),
            str(args["message"]),
            _strings(args["attachments_from"]),
        )

    async def send(args: dict[str, JSONValue], key: str) -> ReactToolResult:
        action_id = str(args["action_id"])
        target = ExistingActionTarget(
            action_id=action_id,
            expected_process_id=read_latest_run_process(
                user_id=plan.user_id, action_id=action_id
            ),
        )
        return _submit(
            plan,
            key,
            "send_to_action",
            target,
            str(args["message"]),
            _strings(args["attachments_from"]),
        )

    async def accept(args: dict[str, JSONValue], key: str) -> ReactToolResult:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        preference = load_effective_approval_preference(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=plan.user_id,
            applies_to=APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
        )
        supplement = args.get("supplement")
        attached = _attachments(
            plan, "accept_suggestion", _strings(args["attachments_from"])
        )
        if isinstance(attached, ReactToolResult):
            return attached
        images, files = attached
        outcome = await accept_suggestion(
            user_id=plan.user_id,
            suggestion_id=str(args["suggestion_id"]),
            command_id=key,
            approval_mode=preference.approval_mode,
            language=None,
            supplement=supplement if isinstance(supplement, str) else None,
            supplement_project_refs=(),
            images=images,
            files=files,
        )
        if isinstance(outcome, SuggestionAccepted):
            return _started("accept_suggestion", outcome.action.action_id)
        sent = read_submitted_message(user_id=plan.user_id, message_id=key)
        if sent is not None:
            return _already_sent("accept_suggestion", sent)
        if outcome.reason == "attachment_unavailable":
            return _refused(
                "accept_suggestion", "ATTACHMENT_ALREADY_HANDED_OVER", _HANDED_OVER
            )
        return _refused(
            "accept_suggestion",
            f"SUGGESTION_{outcome.reason.upper()}",
            "Not taken up: the suggestion is gone, already answered, or not one "
            "that starts a task.",
        )

    return (
        _tool(
            plan,
            "start_action",
            "Start working on a task the user asks of you, as your own task: "
            "making, changing, sending or running something, or research too long "
            "to answer here. Not for answering a question yourself.",
            {"message": _MESSAGE, "attachments_from": _ATTACHMENTS},
            start,
        ),
        _tool(
            plan,
            "send_to_action",
            "Add the user's instruction to one of your tasks in the work list, "
            "when it is about that task. A running task takes it in at its next "
            "step, and a finished one starts again with it.",
            {
                "action_id": {"type": "string"},
                "message": _MESSAGE,
                "attachments_from": _ATTACHMENTS,
            },
            send,
        ),
        _tool(
            plan,
            "accept_suggestion",
            "Take up one of your open suggestions when the user agrees to it. "
            "This starts the suggested work as your task by itself; it becomes "
            "one more task in your work list.",
            {
                "suggestion_id": {"type": "string"},
                "supplement": {
                    "type": ["string", "null"],
                    "description": "What the user added to their yes, or null.",
                },
                "attachments_from": _ATTACHMENTS,
            },
            accept,
        ),
    )


def _tool(
    plan: ChatTurnPlan,
    name: str,
    description: str,
    properties: dict[str, JSONValue],
    run: _Run,
) -> ReactToolDefinition:
    place = [1]

    async def execute(call: ReactToolCall, _step: int) -> ReactToolResult:
        assert isinstance(call.tool_args, dict)
        key = chat_turn_message_id(plan.key, f"{name}/{place[0]}")
        result = await run(call.tool_args, key)
        # A place is used up only by what went in: a re-run that skips a
        # refused call then lands on the same keys as the run it repeats.
        if read_submitted_message(user_id=plan.user_id, message_id=key) is not None:
            place[0] += 1
        return result

    return ReactToolDefinition(
        name=name,
        description=description,
        request_schema={
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        response_schema=react_tool_response_schema(success_schema=_SUCCESS),
        execute=execute,
        # One per model turn: the model sees each result before it sends the
        # next, so a request is not sent twice in one breath.
        concurrency=ToolConcurrency("solo_turn"),
    )


def _submit(
    plan: ChatTurnPlan,
    key: str,
    tool: str,
    target: NewActionTarget | ExistingActionTarget,
    message: str,
    attachments_from: list[str],
) -> ReactToolResult:
    attached = _attachments(plan, tool, attachments_from)
    if isinstance(attached, ReactToolResult):
        return attached
    images, files = attached
    holders = {
        file.attachment_id: read_attachment_holder(
            user_id=plan.user_id, attachment_id=file.attachment_id
        )
        for file in files
    }
    target_id = target.action_id if isinstance(target, ExistingActionTarget) else None
    # A file the target task already holds needs no second hand-off.
    files = tuple(
        f for f in files if target_id is None or holders[f.attachment_id] != target_id
    )
    taken = sorted({h for f in files if (h := holders[f.attachment_id]) is not None})
    if taken:
        return _refused(
            tool,
            "ATTACHMENT_ALREADY_HANDED_OVER",
            "Not done: nothing was sent. A file from those messages already went "
            f"to your task {taken[0]}, which keeps it. To add this to that task, "
            "call send_to_action for it; otherwise ask the user to attach the "
            "file again.",
        )
    try:
        command = SubmitActionMessageCommand(
            user_id=plan.user_id,
            target=target,
            message=ActionUserMessageInput(
                message_id=key,
                content=message,
                images=images,
                files=files,
            ),
        )
    except ValidationError:
        return _refused(
            tool, "INVALID_MESSAGE", "Not done: the message is empty or too long."
        )
    try:
        result = submit_action_message(command)
    except MessageIdentityConflictError:
        sent = read_submitted_message(user_id=plan.user_id, message_id=key)
        if sent is None:
            raise
        return _already_sent(tool, sent)
    except ActionNotFoundError:
        return _refused(tool, "UNKNOWN_TASK", "Not done: no task of yours has that id.")
    except ActionFileAttachmentUnavailableError:
        return _refused(tool, "ATTACHMENT_ALREADY_HANDED_OVER", _HANDED_OVER)
    except ActionMessageConflictError:
        return _refused(
            tool,
            "TASK_CONFLICT",
            "Not done: that task cannot take a message right now.",
        )
    if result.disposition == "not_executed":
        return _stopped(tool)
    return _started(tool, result.action_id)


def _stopped(tool: str) -> ReactToolResult:
    return _refused(
        tool,
        "TASK_STOPPED",
        "Not done: the user stopped this task, and an instruction sent while it "
        "is stopped is not run when they resume it. Tell the user; once they "
        "resume it, send it again.",
    )


def _attachments(
    plan: ChatTurnPlan, tool: str, item_ids: list[str]
) -> tuple[tuple[ImageInput, ...], tuple[FileAttachmentInput, ...]] | ReactToolResult:
    """The images and files of the user's messages ``item_ids``."""

    images: list[ImageInput] = []
    files: list[FileAttachmentInput] = []
    for item_id in item_ids:
        attached = read_user_message(user_id=plan.user_id, item_id=item_id)
        if attached is None:
            return _refused(
                tool,
                "UNKNOWN_MESSAGE",
                f"Not done: {item_id} is not a message of the user.",
            )
        images.extend(attached.images)
        files.extend(attached.files)
    return tuple(images), tuple(files)


def _already_sent(tool: str, sent: SubmittedMessage) -> ReactToolResult:
    # This place in the turn already sent something before a restart, and the
    # turn, asked again, may have reordered or reworded what it sends.
    if sent.dropped:
        return _stopped(tool)
    return _refused(
        tool,
        "ALREADY_SENT_IN_THIS_TURN",
        f"Not sent again: before a restart, this turn already gave your task "
        f'{sent.action_id} this:\n"""\n{sent.text}\n"""\nIf that is this request, it '
        "is in hand, and send_to_action adds anything it lacks; if not, call "
        f"{tool} again.",
    )


# What a success did, in words: the model reports this, not its own guess.
_DONE = {
    "start_action": "Started: this is now your task, and it is working on it.",
    "send_to_action": "Added: your task takes this in.",
    "accept_suggestion": "Taken up: the suggestion is now your task, working on it.",
}


def _started(tool: str, action_id: str) -> ReactToolResult:
    return ReactToolResult(
        tool_name=tool,
        status="success",
        output={"action_id": action_id, "done": _DONE[tool]},
    )


def _refused(tool: str, code: str, message: str) -> ReactToolResult:
    return tool_error_response(tool_name=tool, error_code=code, message=message)


def _strings(value: JSONValue) -> list[str]:
    assert isinstance(value, list)
    return [str(item) for item in value]


__all__ = ["chat_tools"]
