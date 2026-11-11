"""The TODO list's HTTP and WS shapes; Electron's `todoContracts.ts` mirrors them.

Requests are validated here once. Responses are views of the store's TODOs: a
row's state is `display_state`, and its conversation is named by id only.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Self, assert_never
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from pantaray_agents.local_runtime.todos.core import DELETED_TODO_RETENTION
from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    AnyProject,
    NoOrganizationScope,
    NoProject,
    OneProject,
    OrganizationScope,
    TodoProjectFilter,
    TodoScope,
)
from pantaray_agents.local_runtime.todos.display import (
    TodoDisplayState,
    display_state,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    LocalDate,
    NoConversation,
    SuggestionConversation,
    Todo,
    TodoAuthor,
    TodoConversation,
    TodoDue,
)
from pantaray_agents.local_runtime.todos.reads import TodoListSort
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.utils.timestamps import parse_iso8601_utc

type _Id = Annotated[str, Field(min_length=1, max_length=128)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TodoDueBody(_Request):
    date: dt.date
    repeat: Literal["none", "weekly"]

    def due(self) -> TodoDue:
        return TodoDue(date=self.date.isoformat(), repeat=self.repeat)


class TodoScopeQuery(_Request):
    """`organization_id` is given exactly when `scope` is ``organization``."""

    scope: Literal["all", "organization", "no_organization"]
    organization_id: _Id | None = None

    @model_validator(mode="after")
    def _organization_id_matches_scope(self) -> Self:
        if (self.scope == "organization") != (self.organization_id is not None):
            raise ValueError(
                "organization_id is required exactly for scope=organization"
            )
        return self

    def todo_scope(self) -> TodoScope:
        if self.organization_id is not None:
            return OrganizationScope(organization_id=self.organization_id)
        return NoOrganizationScope() if self.scope == "no_organization" else AllScope()


class TodoProjectQuery(TodoScopeQuery):
    """`project_id` is given exactly when `project` is ``project``."""

    project: Literal["any", "project", "no_project"]
    project_id: _Id | None = None

    @model_validator(mode="after")
    def _project_id_matches_filter(self) -> Self:
        if (self.project == "project") != (self.project_id is not None):
            raise ValueError("project_id is required exactly for project=project")
        return self

    def project_filter(self) -> TodoProjectFilter:
        if self.project_id is not None:
            return OneProject(project_id=self.project_id)
        return NoProject() if self.project == "no_project" else AnyProject()


class TodoListQuery(TodoProjectQuery):
    sort: TodoListSort = "added"


class TodoCreateRequest(_Request):
    """`client_todo_id` is the idempotency key; no `starts_on` starts it
    today. The text is one line, as the core decides."""

    client_todo_id: _Id
    project_id: _Id | None
    # The pickup hands the text to an Action as its first message.
    text: Annotated[str, Field(max_length=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)]
    starts_on: dt.date | None = None
    due: TodoDueBody | None


class TodoCheckRequest(_Request):
    checked: StrictBool


class TodoDueRequest(_Request):
    due: TodoDueBody | None


class TodoStartRequest(_Request):
    starts_on: dt.date


class TodoDeletionAnswerRequest(_Request):
    answer: Literal["approve", "keep"]


class NoConversationRef(_View):
    kind: Literal["none"] = "none"


class SuggestionConversationRef(_View):
    suggestion_id: str
    kind: Literal["suggestion"] = "suggestion"


class ActionConversationRef(_View):
    action_id: str
    kind: Literal["action"] = "action"


type TodoConversationRef = Annotated[
    NoConversationRef | SuggestionConversationRef | ActionConversationRef,
    Field(discriminator="kind"),
]


class PendingDeletionRequestView(_View):
    request_id: str
    reason: str


class TodoView(_View):
    todo_id: str
    project_id: str | None
    text: str
    author: TodoAuthor
    origin: Literal["written", "suggestion", "recurrence"]
    starts_on: LocalDate  # the AI picks it up from this date
    due: TodoDue | None
    state: Annotated[TodoDisplayState, Field(discriminator="kind")]
    deletion_request: PendingDeletionRequestView | None  # only while pending
    conversation: TodoConversationRef


class TodoCheckResult(_View):
    todo: TodoView
    # The next instance of a checked weekly TODO.
    next_todo: TodoView | None


class TodoListPage(_View):
    today: LocalDate  # the backend owns the clock
    # Open TODOs first in the order asked for, then checked ones, last checked first.
    items: tuple[TodoView, ...]


class DeletedTodoView(_View):
    todo_id: str
    project_id: str | None
    text: str
    deleted_by: Literal["user", "ai_approved_by_user"]
    deleted_at: str
    purge_on: LocalDate  # the local date from which it can no longer be restored


class TodoChangedMessage(_View):
    """WS ``todo_changed``: the owner's TODOs changed; the screen reads them again."""

    revision: int


def todo_view(
    todo: Todo, conversation: TodoConversation, *, today: LocalDate
) -> TodoView:
    request = todo.deletion_request
    return TodoView(
        todo_id=todo.todo_id,
        project_id=todo.project_id,
        text=todo.text,
        author=todo.author,
        origin=todo.origin.kind,
        starts_on=todo.starts_on,
        due=todo.due,
        state=display_state(todo, conversation, today),
        deletion_request=(
            PendingDeletionRequestView(
                request_id=request.request_id, reason=request.reason
            )
            if request is not None and request.state == "pending"
            else None
        ),
        conversation=_conversation_ref(conversation),
    )


def deleted_todo_view(todo: Todo, zone: ZoneInfo) -> DeletedTodoView:
    deletion = todo.deletion
    assert deletion is not None
    purge_at = parse_iso8601_utc(deletion.at) + DELETED_TODO_RETENTION
    return DeletedTodoView(
        todo_id=todo.todo_id,
        project_id=todo.project_id,
        text=todo.text,
        deleted_by=deletion.by,
        deleted_at=deletion.at,
        purge_on=purge_at.astimezone(zone).date().isoformat(),
    )


def _conversation_ref(conversation: TodoConversation) -> TodoConversationRef:
    match conversation:
        case NoConversation():
            return NoConversationRef()
        case SuggestionConversation(suggestion_id=suggestion_id):
            return SuggestionConversationRef(suggestion_id=suggestion_id)
        case ActionConversation(action_id=action_id):
            return ActionConversationRef(action_id=action_id)
        case _:
            assert_never(conversation)
