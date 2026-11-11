"""write_todo_draft: the draft of the TODO import an Action prepares.

Offered only to the Action a TODO import started, and always about that import,
so it takes no import id. A refusal is an ordinary result the model reads and
fixes, not a failed call.
"""

from __future__ import annotations

from typing import Final, Literal

from pantaray_agents.local_runtime.todos.imports.model import (
    MAX_DRAFT_TODOS,
    DraftIssueReason,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
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

WRITE_TODO_DRAFT_TOOL_ID = "write_todo_draft"

type TodoDraftRefusal = (
    DraftIssueReason
    | Literal["stale_revision", "registered", "canceled", "invalid_draft"]
)

# What the model reads when the draft is not written.
TODO_DRAFT_REFUSALS: Final[dict[TodoDraftRefusal, str]] = {
    "organization_not_found": (
        "The organization this import is for was deleted, so nothing can be "
        "drafted. Tell the user."
    ),
    "organization_outside_scope": (
        "The group at group_index names an organization this import's scope does "
        "not allow."
    ),
    "project_required": (
        "The group at group_index is in an organization but has no project; only "
        "TODOs in no organization may have no project."
    ),
    "project_name_taken": (
        "The project of the group at group_index is already a project of another "
        "organization, or another group puts the same new project in a different "
        "organization."
    ),
    "invalid_text": "The TODO at group_index and todo_index must be one line.",
    "too_many_todos": f"A draft holds at most {MAX_DRAFT_TODOS} TODOs.",
    "stale_revision": (
        "The draft changed since base_revision, possibly by the user's own edits. "
        "`current` is the draft now; write on top of it with its revision."
    ),
    "registered": "The user registered this import; its draft takes no more writes.",
    "canceled": "The user canceled this import; its draft takes no more writes.",
    "invalid_draft": "The draft does not fit its shape. Nothing was written.",
}

_NAME_SCHEMA: Final[dict[str, JSONValue]] = {
    "type": ["string", "null"],
    "minLength": 1,
    "pattern": r"\S",
}
_DATE_SCHEMA: Final[dict[str, JSONValue]] = {
    "type": ["string", "null"],
    "pattern": r"^\d{4}-\d{2}-\d{2}$",
}
_DRAFT_SCHEMA: Final[dict[str, JSONValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["organization", "project", "todos"],
                "properties": {
                    "organization": {
                        **_NAME_SCHEMA,
                        "description": "The organization's name, or null for none.",
                    },
                    "project": {
                        **_NAME_SCHEMA,
                        "description": "The project's name, or null for none.",
                    },
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text"],
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
                                    "description": "One line saying what to do.",
                                },
                                "due": {
                                    **_DATE_SCHEMA,
                                    "description": "YYYY-MM-DD, only as the source states it.",
                                },
                                "starts_on": {
                                    **_DATE_SCHEMA,
                                    "description": (
                                        "YYYY-MM-DD, only when the source gives a "
                                        "start date."
                                    ),
                                },
                                "keep_duplicate": {
                                    "type": "boolean",
                                    "description": (
                                        "Register it even though the project "
                                        "already has an open TODO of this text."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}

WRITE_TODO_DRAFT_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=WRITE_TODO_DRAFT_TOOL_ID,
        name="Write TODO Draft",
        description="Replace the whole draft of this conversation's TODO import.",
        guide=ToolGuideSpec(
            what=(
                "Replaces the whole draft of the TODO import this conversation "
                "prepares: TODOs grouped by organization and project, which the "
                "user reviews, edits and registers. Returns the draft as the "
                "workspace sees it: which names are new, and which TODOs repeat "
                "an open one."
            ),
            when=(
                "First read the user's message and every attached file in full. "
                "Then write the whole draft, one group per organization and "
                "project. Write it again, whole, for each change the user asks "
                "for. After writing, tell the user briefly what you drafted and "
                "what you were unsure of."
            ),
            pitfalls=(
                "Reuse existing organization and project names exactly as the "
                "workspace shows them; any other spelling drafts a new one. Write "
                "one TODO per line of work, in the source's language. Leave "
                "starts_on unset unless the source gives a start date, and keep "
                "every due date the source states. Base each write on the latest "
                "revision; the user may have edited the draft since. A refusal "
                "says what to fix; fix it and write again. Only the user registers "
                "the draft."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="surgical_edit",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="base_revision",
                    schema={"type": "integer", "minimum": 1},
                    required=True,
                    description=(
                        "The revision of the draft this write replaces: 1 for the "
                        "empty draft, then the revision your last write returned."
                    ),
                ),
                field_spec(
                    name="draft",
                    schema=_DRAFT_SCHEMA,
                    required=True,
                    description="The whole draft.",
                ),
            )
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["written", "refused"]},
                "revision": {"type": "integer"},
                "groups": {"type": "array"},
                "reason": {"type": "string", "enum": list(TODO_DRAFT_REFUSALS)},
                "message": {"type": "string"},
                "group_index": {"type": "integer"},
                "todo_index": {"type": "integer"},
                "current": {"type": "object"},
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    )
)


__all__ = [
    "TODO_DRAFT_REFUSALS",
    "TodoDraftRefusal",
    "WRITE_TODO_DRAFT_TOOL",
    "WRITE_TODO_DRAFT_TOOL_ID",
]
