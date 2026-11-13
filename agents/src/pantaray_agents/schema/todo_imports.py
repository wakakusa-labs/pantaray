"""A TODO import's HTTP shapes; Electron's `todoImportContracts.ts` mirrors them.

The view is the draft as the workspace sees it now, the same one the import's
Action reads from `write_todo_draft`: each group's names, marked new or
existing, and its TODOs, marked when they repeat an open one.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from pantaray_agents.local_runtime.todos.db import OrganizationScope
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    DraftIssueReason,
    ImportState,
    NewOrganization,
    NewProject,
    ResolvedGroup,
    TodoImportDraft,
    TodoImportResult,
    TodoImportView,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    ACTION_MESSAGE_MAX_FILES,
    ACTION_MESSAGE_MAX_IMAGES,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.todos import TodoScopeQuery, _Request, _View

# The client names the import, so resending the start opens no second one.
type _ImportId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]


class TodoImportStartRequest(TodoScopeQuery):
    """The user's pasted text, the files and images the composer staged, or any
    of them."""

    import_id: _ImportId
    # The import's Action takes the text as its first message.
    text: (
        Annotated[str, Field(max_length=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)] | None
    ) = None
    files: Annotated[
        tuple[FileAttachmentInput, ...], Field(max_length=ACTION_MESSAGE_MAX_FILES)
    ] = ()
    # As `action:attachImage` wrote them, for the Action to see as images.
    images: Annotated[
        tuple[ImageInput, ...], Field(max_length=ACTION_MESSAGE_MAX_IMAGES)
    ] = ()

    @model_validator(mode="after")
    def _requires_something_to_import(self) -> Self:
        if not (self.text or "").strip() and not self.files and not self.images:
            raise ValueError("text, files or images are required")
        return self


class TodoImportDraftRequest(_Request):
    """The whole draft, written over the revision the user saw."""

    expected_revision: PositiveInt
    draft: TodoImportDraft


class TodoImportRegisterRequest(_Request):
    """The revision the user confirmed."""

    expected_revision: PositiveInt


class DraftNameView(_View):
    name: str
    new: bool


class DraftLineView(_View):
    text: str
    due: dt.date | None
    starts_on: dt.date | None
    # The project already has an open TODO of this text, or an earlier line does.
    duplicate: bool
    keep_duplicate: bool


class DraftGroupView(_View):
    organization: DraftNameView | None
    project: DraftNameView | None
    todos: tuple[DraftLineView, ...]


class DraftIssueView(_View):
    reason: DraftIssueReason
    group_index: int | None
    todo_index: int | None


class TodoImportHttpView(_View):
    import_id: str
    scope: Literal["all", "organization", "no_organization"]
    organization_id: str | None
    revision: int
    state: ImportState
    # The Action that drafts it; None only while a start that failed is retried.
    action_id: str | None
    groups: tuple[DraftGroupView, ...]
    # Why the draft no longer registers, after the workspace changed under it.
    issue: DraftIssueView | None
    result: TodoImportResult | None


class OpenTodoImportView(_View):
    """An import neither registered nor canceled, for the list to reopen."""

    import_id: str
    # The first line of what the user sent: the text, or the files named.
    request: str
    started_at: str


type TodoImportRefusalReason = (
    DraftIssueReason
    | Literal[
        "not_found",
        "stale_revision",
        "registered",
        "canceled",
        # A staged file is gone, an image is not the user's, or another start
        # already used this import id.
        "files_unavailable",
        "images_invalid",
        "start_conflict",
    ]
)


class TodoImportRefused(_View):
    """The body of a refused import request; the indexes locate a draft issue."""

    type: Literal["TodoImportRefused"] = "TodoImportRefused"
    reason: TodoImportRefusalReason
    group_index: int | None = None
    todo_index: int | None = None


def draft_group_views(groups: tuple[ResolvedGroup, ...]) -> tuple[DraftGroupView, ...]:
    return tuple(
        DraftGroupView(
            organization=None
            if group.organization is None
            else DraftNameView(
                name=group.organization.name,
                new=isinstance(group.organization, NewOrganization),
            ),
            project=None
            if group.project is None
            else DraftNameView(
                name=group.project.name, new=isinstance(group.project, NewProject)
            ),
            todos=tuple(
                DraftLineView(
                    text=line.todo.text,
                    due=line.todo.due,
                    starts_on=line.todo.starts_on,
                    duplicate=line.duplicate,
                    keep_duplicate=line.todo.keep_duplicate,
                )
                for line in group.todos
            ),
        )
        for group in groups
    )


def draft_issue_view(issue: DraftIssue) -> DraftIssueView:
    return DraftIssueView(
        reason=issue.reason, group_index=issue.group_index, todo_index=issue.todo_index
    )


def todo_import_http_view(
    view: TodoImportView, *, action_id: str | None
) -> TodoImportHttpView:
    scope = view.scope
    return TodoImportHttpView(
        import_id=view.import_id,
        scope=scope.kind,
        organization_id=scope.organization_id
        if isinstance(scope, OrganizationScope)
        else None,
        revision=view.revision,
        state=view.state,
        action_id=action_id,
        groups=draft_group_views(view.groups),
        issue=None if view.issue is None else draft_issue_view(view.issue),
        result=view.result,
    )
