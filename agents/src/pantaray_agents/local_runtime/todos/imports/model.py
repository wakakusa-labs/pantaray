"""A TODO import's draft as written, the view of it against the workspace, and
what its registration created.

The draft names organizations and projects; whether each is new or existing is
never stored, only decided by `resolution` against the workspace as it is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pantaray_agents.local_runtime.todos.db import TodoScope
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)

# Design limit: a draft is replaced whole on every write, so its size bounds
# each write. Add partial (diff) writes when real exports exceed 300 TODOs.
MAX_DRAFT_TODOS: Final = 300

# Stripped as the workspace stores names, so a name matches as it is shown.
type _Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type ImportState = Literal["open", "registered", "canceled"]


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DraftTodo(_Stored):
    # The pickup hands the text to an Action as its first message; whether it
    # is one line is the core's rule, checked by `resolution`.
    text: Annotated[str, Field(max_length=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)]
    due: dt.date | None = None
    # Unset by default, so an import does not start all its TODOs at once.
    starts_on: dt.date | None = None
    # Register it even when the project already has an open TODO of this text.
    keep_duplicate: bool = False


class DraftGroup(_Stored):
    """TODOs of one project, or of no project when `project` is None."""

    organization: _Name | None
    project: _Name | None
    todos: tuple[DraftTodo, ...]


class TodoImportDraft(_Stored):
    groups: tuple[DraftGroup, ...] = ()


class TodoImportResult(_Stored):
    """What a registration created, stored to answer a repeated one."""

    organization_ids: tuple[str, ...]
    project_ids: tuple[str, ...]
    todo_ids: tuple[str, ...]
    skipped_duplicates: int


@dataclass(frozen=True, slots=True)
class ExistingOrganization:
    organization_id: str
    name: str
    kind: Literal["existing"] = "existing"


@dataclass(frozen=True, slots=True)
class NewOrganization:
    name: str
    kind: Literal["new"] = "new"


@dataclass(frozen=True, slots=True)
class ExistingProject:
    project_id: str
    name: str
    organization_ids: frozenset[str]
    kind: Literal["existing"] = "existing"


@dataclass(frozen=True, slots=True)
class NewProject:
    name: str
    kind: Literal["new"] = "new"


@dataclass(frozen=True, slots=True)
class TodoLine:
    todo: DraftTodo
    # The project already has an open TODO of this text, or an earlier line of
    # the draft puts one there.
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ResolvedGroup:
    # The organization the group's project is in: in an organization's scope,
    # that organization even when the group names none.
    organization: ExistingOrganization | NewOrganization | None
    project: ExistingProject | NewProject | None
    todos: tuple[TodoLine, ...]


type DraftIssueReason = Literal[
    # The scope's organization was deleted.
    "organization_not_found",
    # The group names an organization the scope does not allow.
    "organization_outside_scope",
    # A group in an organization has no project; a TODO without a project has
    # no organization.
    "project_required",
    # The project's name belongs to a project outside the group's organization,
    # or two groups put one new project in different organizations.
    "project_name_taken",
    # The TODO's text is empty or more than one line.
    "invalid_text",
    "too_many_todos",
]


@dataclass(frozen=True, slots=True)
class DraftIssue:
    """Why the draft cannot be written or registered, and where."""

    reason: DraftIssueReason
    group_index: int | None = None
    todo_index: int | None = None


@dataclass(frozen=True, slots=True)
class TodoImportView:
    import_id: str
    scope: TodoScope
    revision: int
    state: ImportState
    draft: TodoImportDraft
    groups: tuple[ResolvedGroup, ...]
    # Why the draft no longer registers, after the workspace changed under it.
    issue: DraftIssue | None
    result: TodoImportResult | None


@dataclass(frozen=True, slots=True)
class StaleRevision:
    """The draft changed since `expected_revision`; write on top of `current`."""

    current: TodoImportView


@dataclass(frozen=True, slots=True)
class ImportClosed:
    """The import was registered or canceled, so it takes no more writes."""

    state: Literal["registered", "canceled"]
