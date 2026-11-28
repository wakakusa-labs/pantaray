"""What a create_hook call asks for, checked once before the user is asked.

The input schema has already fixed the arguments' shapes; this reads them into
the task the approval would save, and refuses what could not fire as meant or
names a place the workspace does not have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict, cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_object_arg,
    optional_string_arg,
    require_string_arg,
)
from pantaray_agents.hooks.definitions import FieldMatch, HookDefinitionError, MatchOp
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z
from pantaray_agents.local_runtime.hooks.agent_tasks import new_task_id
from pantaray_agents.local_runtime.hooks.controls import NewConversation
from pantaray_agents.local_runtime.hooks.tasks import (
    Task,
    TaskSchedule,
    build_task,
    trigger_json,
)
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.model import WrittenOrigin
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.schema.agent.base import JSONValue

SendTo = Literal["this_session", "new_session"]


class HookRequestError(ValueError):
    """The requested hook cannot be saved; the message says why."""


class _MatchArg(TypedDict):
    field: str
    op: MatchOp
    values: list[str]


class _TriggerArg(TypedDict, total=False):
    kind: Literal["at", "every", "on"]
    at: str
    every: str
    offset: str | None
    on: str
    match: list[_MatchArg]


@dataclass(frozen=True, slots=True)
class PlaceNames:
    """The place as the call named it, which the user sees."""

    organization: str | None
    project: str | None


@dataclass(frozen=True, slots=True)
class HookRequest:
    task: Task
    send_to: SendTo
    place: PlaceNames | None
    # Set exactly for new_session: the title, and the place resolved to ids.
    new_conversation: NewConversation | None
    # None for a new hook.
    replaces: str | None

    def summary(self, *, resumes: bool) -> dict[str, JSONValue]:
        """What the user approves, and the approval binds to: the definition
        as it will be stored, and whether the yes switches a paused hook back
        on. The panel words the trigger from this stored form."""

        until = self.task.schedule.until
        return {
            "summary_kind": "hook",
            "replaces": self.replaces is not None,
            "resumes": resumes,
            "trigger": stored_trigger(self.task),
            "until": None if until is None else iso_z(until),
            "send_to": self.send_to,
            "place": None
            if self.place is None
            else {
                "organization": self.place.organization,
                "project": self.place.project,
            },
            "title": None
            if self.new_conversation is None
            else self.new_conversation.title,
            "message": self.task.message,
        }


def stored_trigger(task: Task) -> dict[str, JSONValue]:
    """The trigger as stored, in the shape create_hook takes it."""

    trigger: dict[str, JSONValue] = json.loads(trigger_json(task.schedule))
    kind = next(key for key in ("at", "every", "on") if key in trigger)
    return {"kind": kind, **trigger}


def resolve_request(
    args: ToolArgs, *, tool_request_id: str, workspace: WorkspaceSettings
) -> HookRequest:
    """The hook a create_hook call asks for.

    Raises:
        HookRequestError: the call cannot be saved as given.
    """

    replaces = optional_string_arg(args, "replaces")
    try:
        task = build_task(
            replaces or new_task_id(tool_request_id),
            _schedule(cast("_TriggerArg", args["trigger"]), _instant(args, "until")),
            require_string_arg(args, "message"),
        )
    except HookDefinitionError as exc:
        raise HookRequestError(str(exc)) from exc
    send_to = cast("SendTo", require_string_arg(args, "send_to"))
    title = optional_string_arg(args, "title")
    place_arg = optional_object_arg(args, "place")
    place = (
        None
        if place_arg is None
        else PlaceNames(
            organization=_name(place_arg.get("organization")),
            project=_name(place_arg.get("project")),
        )
    )
    if place == PlaceNames(organization=None, project=None):
        place = None
    if send_to == "this_session":
        if title is not None or place is not None:
            raise HookRequestError(
                "title and place go only with send_to new_session; this_session "
                "continues this conversation."
            )
        new_conversation = None
    else:
        new_conversation = _new_conversation(title, place, workspace)
    return HookRequest(
        task=task,
        send_to=send_to,
        place=place,
        new_conversation=new_conversation,
        replaces=replaces,
    )


def _schedule(trigger: _TriggerArg, until: datetime | None) -> TaskSchedule:
    if trigger["kind"] == "on":
        return TaskSchedule(
            at=None,
            every=None,
            offset=None,
            until=until,
            on=trigger["on"],
            match=tuple(
                FieldMatch(field=m["field"], op=m["op"], values=tuple(m["values"]))
                for m in trigger["match"]
            ),
        )
    if trigger["kind"] == "at":
        return TaskSchedule(
            at=_parse_instant(trigger["at"], "trigger.at"),
            every=None,
            offset=None,
            until=until,
        )
    every, offset = trigger["every"], trigger.get("offset")
    if offset is not None and every[0].isdigit():
        raise HookRequestError("offset goes only with a calendar unit in every.")
    return TaskSchedule(at=None, every=every, offset=offset, until=until)


def _new_conversation(
    title: str | None, place: PlaceNames | None, workspace: WorkspaceSettings
) -> NewConversation:
    if title is None:
        raise HookRequestError("send_to new_session needs a title for its TODO.")
    created = core.create_todo(
        todo_id="hook-title",
        text=title,
        author="ai",
        origin=WrittenOrigin(),
        project_id=None,
        organization_id=None,
        starts_on=None,
        due=None,
        now="",
    )
    if isinstance(created, core.Rejected):
        raise HookRequestError("title must be one non-empty line, as a TODO's text.")
    organization_id, project_id = _place_ids(place, workspace)
    return NewConversation(
        title=created.text, organization_id=organization_id, project_id=project_id
    )


def _place_ids(
    place: PlaceNames | None, workspace: WorkspaceSettings
) -> tuple[str | None, str | None]:
    """The place's ids as a TODO stores them: its project, or with none its
    organization. Names match exactly, as the TODO import reads them, and only
    existing ones do: a hook never creates an organization or a project."""

    if place is None:
        return None, None
    organizations = {o.display_name: o.organization_id for o in workspace.organizations}
    organization_id = None
    if place.organization is not None:
        organization_id = organizations.get(place.organization)
        if organization_id is None:
            raise HookRequestError(
                f"No organization is named {place.organization!r}; the "
                f"organizations are {sorted(organizations)}."
            )
    if place.project is None:
        return organization_id, None
    projects = {p.display_name: p for p in workspace.projects}
    project = projects.get(place.project)
    if project is None:
        raise HookRequestError(
            f"No project is named {place.project!r}; the projects are "
            f"{sorted(projects)}."
        )
    if organization_id is not None and organization_id not in project.organization_ids:
        raise HookRequestError(
            f"The project {place.project!r} is not in the organization "
            f"{place.organization!r}."
        )
    return None, project.project_id


def _name(value: JSONValue) -> str | None:
    return value if isinstance(value, str) else None


def _instant(args: ToolArgs, key: str) -> datetime | None:
    value = optional_string_arg(args, key)
    return None if value is None else _parse_instant(value, key)


def _parse_instant(value: str, key: str) -> datetime:
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HookRequestError(f"{key} is not an ISO 8601 date and time.") from exc
    if instant.utcoffset() is None:
        raise HookRequestError(f"{key} needs its offset, such as +09:00.")
    return instant


__all__ = [
    "HookRequest",
    "HookRequestError",
    "PlaceNames",
    "SendTo",
    "resolve_request",
    "stored_trigger",
]
