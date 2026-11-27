"""The tools an Action sets up hooks with: later messages to a conversation.

A hook sends its message at a time, on a repeat, or when a recorded window
starts to match, to this conversation or to a new one on each fire. Each
create or replace asks the user; the tools reach only the hooks this
conversation created.
"""

from __future__ import annotations

from typing import Final

from pantaray_agents.local_runtime.hooks.events import USER_HOOK_EVENT_FIELDS
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ToolConcurrency

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

CREATE_HOOK_TOOL_ID = "create_hook"
LIST_HOOKS_TOOL_ID = "list_hooks"
DELETE_HOOK_TOOL_ID = "delete_hook"
HOOK_TOOL_IDS: Final = (CREATE_HOOK_TOOL_ID, LIST_HOOKS_TOOL_ID, DELETE_HOOK_TOOL_ID)

_INSTANT: Final[dict[str, JSONValue]] = {
    "type": "string",
    "format": "date-time",
    "minLength": 1,
}
_TEXT: Final[dict[str, JSONValue]] = {
    "type": "string",
    "minLength": 1,
    "pattern": r"\S",
}
_NAME: Final[dict[str, JSONValue]] = {**_TEXT, "type": ["string", "null"]}
_HOOK_ID: Final[dict[str, JSONValue]] = {
    "type": "string",
    "pattern": r"^task\.[a-z0-9][a-z0-9_-]{0,63}$",
}
_ACTIVITY_FIELDS: Final[list[JSONValue]] = [*sorted(USER_HOOK_EVENT_FIELDS["activity"])]
# The trigger as stored: one of the three shapes, as the model gave it.
_TRIGGER: Final[dict[str, JSONValue]] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"at": _INSTANT},
            "required": ["at"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "every": {
                    "type": "string",
                    "pattern": r"^(hour|day|week|month|quarter|[0-9]+[smhd])$",
                },
                "offset": {"type": ["string", "null"], "pattern": r"^[0-9]+[smhd]$"},
            },
            "required": ["every"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "on": {"const": "activity"},
                "match": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"enum": _ACTIVITY_FIELDS},
                            "op": {"enum": ["equals", "contains"]},
                            "values": {"type": "array", "minItems": 1, "items": _TEXT},
                        },
                        "required": ["field", "op", "values"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["on", "match"],
            "additionalProperties": False,
        },
    ]
}
_PLACE: Final[dict[str, JSONValue]] = {
    "type": ["object", "null"],
    "properties": {"organization": _NAME, "project": _NAME},
    "additionalProperties": False,
}
_NULLABLE_INSTANT: Final[dict[str, JSONValue]] = {
    **_INSTANT,
    "type": ["string", "null"],
}

_NULLABLE_TEXT: Final[dict[str, JSONValue]] = {"type": ["string", "null"]}
# A hook as list_hooks shows it: create_hook's arguments, then its runs.
_LISTED_HOOK: Final[dict[str, JSONValue]] = {
    "hook_id": {"type": "string"},
    "trigger": {"type": "object"},
    "until": _NULLABLE_TEXT,
    "send_to": {"enum": ["this_session", "new_session"]},
    "place": {
        **_PLACE,
        "properties": {"organization": _NULLABLE_TEXT, "project": _NULLABLE_TEXT},
        "required": ["organization", "project"],
    },
    "title": _NULLABLE_TEXT,
    "message": {"type": "string"},
    "next_run_at": _NULLABLE_TEXT,
    "enabled": {"type": "boolean"},
    "last_result": _NULLABLE_TEXT,
    "failure_count": {"type": "integer"},
    "stopped_reason": _NULLABLE_TEXT,
}

CREATE_HOOK_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=CREATE_HOOK_TOOL_ID,
        name="Create Hook",
        description=(
            "Send a message later, once, repeatedly, or when the user starts using "
            "a matching window, to this conversation or to a new one each time."
        ),
        guide=ToolGuideSpec(
            what=(
                "Each time the trigger fires, Pantaray sends the message as the next "
                'turn of this conversation (send_to "this_session"), or starts a new '
                'conversation with it (send_to "new_session") as that of a new TODO '
                "titled title, in place's organization or project. Triggers: "
                "{at} for one time; {every, offset} to repeat, where every is a "
                "calendar unit (hour, day, week, month, quarter) that fires at the "
                "unit's start on local time plus offset, or a duration (30m, 2h, 3d) "
                "that repeats from the approval. Weeks start on Monday 00:00 and "
                'months on the 1st, so every day at 08:30 is every "day" with offset '
                '"510m". {on: "activity", match} fires once each time a window the '
                "user's computer recorded starts to match: every field match must "
                "hold, a field matches when it equals, or contains, any one of its "
                "values, ignoring case; values are plain text, not patterns. Apps "
                "that need different matches take one hook each. The "
                "matched window's app name, bundle id and title are added to the "
                "message. Nothing fires at or after until. Give replaces to replace "
                "one of this conversation's hooks."
            ),
            when=(
                "Use when the user asks for work at a later time, on a repeating "
                "basis, or whenever something starts, such as each online meeting. "
                "You may offer it when the work needs a later look, but call it only "
                "once the user wants it."
            ),
            pitfalls=(
                "Every call asks the user to approve it, and nothing is saved if they "
                "decline. Write the message so it stands on its own when it arrives "
                "later. Before choosing app names or window titles, look at the "
                "user's actual records with zanei_timeline; a meeting app is picked "
                "reliably by its bundle_id. place names existing organizations and "
                "projects exactly. A hook runs one turn at a time, and three failed "
                "turns in a row stop it; replacing it turns it back on."
            ),
        ),
        # Sequential: it pauses the Action for the user's approval.
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="trigger",
                    schema=_TRIGGER,
                    required=True,
                    description=(
                        "{at}, {every, offset}, or {on, match}. Times are ISO 8601 "
                        "with their offset, such as 2026-11-22T09:00:00+09:00."
                    ),
                ),
                field_spec(
                    name="until",
                    schema=_NULLABLE_INSTANT,
                    description="When the hook ends, ISO 8601 with its offset.",
                ),
                field_spec(
                    name="send_to",
                    schema={"enum": ["this_session", "new_session"]},
                    required=True,
                    description=(
                        "this_session continues this conversation; new_session "
                        "starts a new one on each fire."
                    ),
                ),
                field_spec(
                    name="place",
                    schema=_PLACE,
                    description=(
                        "With new_session only: the organization and project, by "
                        "name, for the new TODO; omit for neither."
                    ),
                ),
                field_spec(
                    name="title",
                    schema=_NAME,
                    description=(
                        "With new_session only, and required there: the new TODO's "
                        "one-line text."
                    ),
                ),
                field_spec(
                    name="message",
                    schema=_TEXT,
                    required=True,
                    description="The message each fire sends.",
                ),
                field_spec(
                    name="replaces",
                    schema={**_HOOK_ID, "type": ["string", "null"]},
                    description="A hook_id from list_hooks to replace that hook.",
                ),
            ),
            description="Create or replace a hook of this conversation.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"const": "created"},
                "hook_id": {"type": "string"},
                # None for an on trigger, which waits for its event.
                "next_run_at": {"type": ["string", "null"]},
            },
            "required": ["status", "hook_id", "next_run_at"],
            "additionalProperties": False,
        },
    )
)

LIST_HOOKS_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=LIST_HOOKS_TOOL_ID,
        name="List Hooks",
        description="List the hooks this conversation created.",
        guide=ToolGuideSpec(
            what=(
                "Lists each hook this conversation created with its hook_id, its "
                "definition as create_hook took it, its next run, and how its latest "
                "turn went."
            ),
            when=(
                "Before you replace or delete a hook, or when the user asks what is "
                "set up here."
            ),
            pitfalls=(
                "Hooks of other conversations are not listed. stopped_reason means "
                "failures stopped it and enabled false that the user switched it "
                "off; replacing it, with the user's approval, turns it back on."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(fields=(), description="List this conversation's hooks."),
        output_schema={
            "type": "object",
            "properties": {
                "hooks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": _LISTED_HOOK,
                        "required": [*_LISTED_HOOK],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["hooks"],
            "additionalProperties": False,
        },
    )
)

DELETE_HOOK_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=DELETE_HOOK_TOOL_ID,
        name="Delete Hook",
        description="Delete one hook this conversation created.",
        guide=ToolGuideSpec(
            what="Deletes the hook, so it sends no more messages.",
            when=(
                "When the user asks to stop it, or the work it repeats is done and "
                "the user agreed to end it."
            ),
            pitfalls=(
                "It cannot be undone; create it again to restore it, which asks the "
                "user. A turn already sent still runs."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="hook_id",
                    schema=_HOOK_ID,
                    required=True,
                    description="A hook_id from list_hooks.",
                ),
            ),
            description="Delete one of this conversation's hooks.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"const": "deleted"},
                "hook_id": {"type": "string"},
            },
            "required": ["status", "hook_id"],
            "additionalProperties": False,
        },
    )
)

__all__ = [
    "CREATE_HOOK_TOOL",
    "CREATE_HOOK_TOOL_ID",
    "DELETE_HOOK_TOOL",
    "DELETE_HOOK_TOOL_ID",
    "HOOK_TOOL_IDS",
    "LIST_HOOKS_TOOL",
    "LIST_HOOKS_TOOL_ID",
]
