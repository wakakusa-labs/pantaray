"""The TODO list's HTTP and WS shapes; Electron's `todoContracts.ts` mirrors them.

Requests are validated here once. Responses are views of the store's TODOs: a
row's state is `display_state`, and its conversation is named by id only.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Self, assert_never
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

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
    HookOrigin,
    LocalDate,
    NoConversation,
    RecurrenceOrigin,
    SuggestionConversation,
    SuggestionOrigin,
    Todo,
    TodoAuthor,
    TodoConversation,
    TodoDue,
    TodoOrigin,
    WrittenOrigin,
)
from pantaray_agents.local_runtime.todos.reads import TodoListSort
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    FileAttachmentInput,
    bounded_files,
    bounded_images,
)
from pantaray_agents.schema.agent.image import ImageInput
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
    """`client_todo_id` is the idempotency key; an omitted `starts_on` starts
    it today and `null` leaves it with no start. The text is one line, as the
    core decides. `organization_id` puts a TODO with no project in that
    organization; one with a project is in the project's, so not both."""

    client_todo_id: _Id
    project_id: _Id | None
    organization_id: _Id | None
    # The pickup hands the text to an Action as its first message.
    text: Annotated[str, Field(max_length=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)]
    starts_on: dt.date | None = None
    due: TodoDueBody | None
    # Staged files and stored images, as the Action composer sends them; the
    # pickup hands them to the Action with the text.
    files: tuple[FileAttachmentInput, ...] = ()
    images: tuple[ImageInput, ...] = ()

    _bound_files = field_validator("files", mode="before")(bounded_files)
    _bound_images = field_validator("images", mode="before")(bounded_images)

    @model_validator(mode="after")
    def _one_place(self) -> Self:
        if self.project_id is not None and self.organization_id is not None:
            raise ValueError("project_id and organization_id are not given together")
        return self


class TodoCheckRequest(_Request):
    checked: StrictBool


class TodoDueRequest(_Request):
    due: TodoDueBody | None


class TodoStartRequest(_Request):
    starts_on: dt.date | None  # null: no start, so the AI does not pick it up


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


class WrittenOriginRef(_View):
    kind: Literal["written"] = "written"


class SuggestionOriginRef(_View):
    kind: Literal["suggestion"] = "suggestion"


class RecurrenceOriginRef(_View):
    kind: Literal["recurrence"] = "recurrence"


class HookOriginRef(_View):
    # The conversation that asked for the hook whose fire wrote this TODO; it
    # may have been deleted since.
    origin_action_id: str
    kind: Literal["hook"] = "hook"


type TodoOriginRef = Annotated[
    WrittenOriginRef | SuggestionOriginRef | RecurrenceOriginRef | HookOriginRef,
    Field(discriminator="kind"),
]


class PendingDeletionRequestView(_View):
    request_id: str
    reason: str


class TodoView(_View):
    todo_id: str
    project_id: str | None
    # Its own organization while it has no project; null with a project.
    organization_id: str | None
    text: str
    author: TodoAuthor
    origin: TodoOriginRef
    starts_on: LocalDate | None  # the AI picks it up from this date; null never
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
        organization_id=todo.organization_id,
        text=todo.text,
        author=todo.author,
        origin=_origin_ref(todo.origin),
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


def _origin_ref(origin: TodoOrigin) -> TodoOriginRef:
    match origin:
        case WrittenOrigin():
            return WrittenOriginRef()
        case SuggestionOrigin():
            return SuggestionOriginRef()
        case RecurrenceOrigin():
            return RecurrenceOriginRef()
        case HookOrigin(origin_action_id=origin_action_id):
            return HookOriginRef(origin_action_id=origin_action_id)
        case _:
            assert_never(origin)


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
