"""Decide, without I/O, what a draft means in the workspace as it is: which
names are new, which TODOs are already there, and whether the scope allows it.

Every writer and the registration run the same `resolve_draft`, so a draft is
held to one rule however it was written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    OrganizationScope,
    TodoScope,
)
from pantaray_agents.local_runtime.todos.imports.model import (
    MAX_DRAFT_TODOS,
    DraftGroup,
    DraftIssue,
    DraftIssueReason,
    ExistingOrganization,
    ExistingProject,
    NewOrganization,
    NewProject,
    ResolvedGroup,
    TodoImportDraft,
    TodoLine,
)
from pantaray_agents.local_runtime.todos.model import WrittenOrigin

type _Organization = ExistingOrganization | NewOrganization | None
type _ProjectKey = str | NewProject | None


@dataclass(frozen=True, slots=True)
class Workspace:
    """The user's active organizations and projects, by their unique names."""

    organization_ids: Mapping[str, str]
    projects: Mapping[str, ExistingProject]
    # (project_id, text) of every open TODO; a None project is "no project".
    open_todos: frozenset[tuple[_ProjectKey, str]]


def resolve_draft(
    draft: TodoImportDraft, *, scope: TodoScope, workspace: Workspace
) -> tuple[tuple[ResolvedGroup, ...], DraftIssue | None]:
    """The draft against the workspace, and its first issue if it has one."""

    scope_organization = _scope_organization(scope, workspace)
    # Texts already in each project, by the project's identity: the workspace's
    # open TODOs, then each draft line as it is read, so a later line repeating
    # an earlier one, in this group or another for the same project, is marked.
    seen: set[tuple[_ProjectKey, str]] = set(workspace.open_todos)
    groups = tuple(
        _resolve_group(group, scope_organization, workspace, seen)
        for group in draft.groups
    )
    if isinstance(scope, OrganizationScope) and scope_organization is None:
        return groups, DraftIssue("organization_not_found")
    if sum(len(group.todos) for group in draft.groups) > MAX_DRAFT_TODOS:
        return groups, DraftIssue("too_many_todos")
    new_project_organizations: dict[str, str | None] = {}
    for index, resolved in enumerate(groups):
        reason = _group_issue(resolved, scope, new_project_organizations)
        if reason is not None:
            return groups, DraftIssue(reason, group_index=index)
        for todo_index, line in enumerate(resolved.todos):
            if not _is_todo_text(line.todo.text):
                return groups, DraftIssue(
                    "invalid_text", group_index=index, todo_index=todo_index
                )
    return groups, None


def _scope_organization(
    scope: TodoScope, workspace: Workspace
) -> ExistingOrganization | None:
    if not isinstance(scope, OrganizationScope):
        return None
    for name, organization_id in workspace.organization_ids.items():
        if organization_id == scope.organization_id:
            return ExistingOrganization(organization_id=organization_id, name=name)
    return None


def _resolve_group(
    group: DraftGroup,
    scope_organization: ExistingOrganization | None,
    workspace: Workspace,
    seen: set[tuple[_ProjectKey, str]],
) -> ResolvedGroup:
    organization: _Organization
    if scope_organization is not None and group.organization in (
        None,
        scope_organization.name,
    ):
        organization = scope_organization
    elif group.organization is None:
        organization = None
    elif group.organization in workspace.organization_ids:
        organization = ExistingOrganization(
            organization_id=workspace.organization_ids[group.organization],
            name=group.organization,
        )
    else:
        organization = NewOrganization(name=group.organization)
    project = (
        None
        if group.project is None
        else workspace.projects.get(group.project) or NewProject(group.project)
    )
    # A new project is known by its name, an existing one by its id.
    project_key = (
        project.project_id if isinstance(project, ExistingProject) else project
    )
    lines: list[TodoLine] = []
    for todo in group.todos:
        key = (project_key, todo.text.strip())
        lines.append(TodoLine(todo=todo, duplicate=key in seen))
        seen.add(key)
    return ResolvedGroup(organization=organization, project=project, todos=tuple(lines))


def _group_issue(
    resolved: ResolvedGroup,
    scope: TodoScope,
    new_project_organizations: dict[str, str | None],
) -> DraftIssueReason | None:
    organization = resolved.organization
    if isinstance(scope, OrganizationScope):
        # `_resolve_group` gives a group naming no organization, or this one,
        # the scope's organization.
        allowed = _is_in(organization, scope.organization_id)
    else:
        allowed = organization is None or isinstance(scope, AllScope)
    if not allowed:
        return "organization_outside_scope"
    project = resolved.project
    if project is None:
        return None if organization is None else "project_required"
    if isinstance(project, ExistingProject):
        inside = (
            not project.organization_ids
            if organization is None
            else any(_is_in(organization, id_) for id_ in project.organization_ids)
        )
        return None if inside else "project_name_taken"
    organization_name = None if organization is None else organization.name
    claimed = new_project_organizations.setdefault(project.name, organization_name)
    return None if claimed == organization_name else "project_name_taken"


def _is_in(organization: _Organization, organization_id: str) -> bool:
    return (
        isinstance(organization, ExistingOrganization)
        and organization.organization_id == organization_id
    )


def _is_todo_text(text: str) -> bool:
    # The core owns what a TODO's text may be; only its verdict is used here.
    created = core.create_todo(
        todo_id="draft",
        text=text,
        author="user",
        origin=WrittenOrigin(),
        project_id=None,
        starts_on=None,
        due=None,
        now="",
    )
    return not isinstance(created, core.Rejected)
