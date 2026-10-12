"""Agent Experience consolidation: lessons that overlap become one.

An entry is one lesson, and the host-written index lists each one's situation.
Lessons only accumulate, so the index outgrows what a later agent can be handed
whole. A consolidation merges the lessons that cover one situation into a new
entry that supersedes them, the same way an update replaces a lesson.
"""

from __future__ import annotations

import sqlite3
from typing import Final, TypedDict, cast, get_args

from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
    AgentExperienceContent,
    AgentExperienceOutcome,
    experience_entry_path,
    experience_id_from_path,
    parse_agent_experience_markdown,
    render_agent_experience_markdown,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_delta import (
    rebuild_agent_experience_index,
)
from pantaray_agents.local_runtime.memory_catalog.draft import replace_draft_documents
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.fragments import content_sha256
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    consolidated_experience_ids,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryDraftCheckpoint,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    MemoryDraftToolSession,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.tool_support import (
    args_object,
    response_schema,
    success,
    tool,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
)

MERGE_EXPERIENCES_TOOL_NAME: Final[str] = "merge_experiences"


class _Merge(TypedDict):
    supersedes: list[str]
    applies_when: str
    observed_approach: str
    outcome: AgentExperienceOutcome
    next_time_rule: str
    observed_result: str


def merge_experiences_tool(
    *, session: MemoryDraftToolSession, run_id: str
) -> ReactToolDefinition:
    """Replace groups of lessons, each with one new lesson that supersedes them.

    Only lessons this run started with can be merged, so every ID a new entry
    supersedes leaves the tree in this publication. Each merge is judged on
    its own; a rejected one leaves the tree as the merges before it left it.
    """

    original = {
        experience_id_from_path(document.source_path)
        for document in session.draft.documents
        if document.source_path != AGENT_EXPERIENCE_INDEX_PATH
    }
    free_ids = [
        item for item in consolidated_experience_ids(run_id) if item not in original
    ]

    async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
        # The registry has checked the arguments against the request schema.
        merges = cast(list[_Merge], args_object(call.tool_args)["merges"])
        merged: list[JSONValue] = []
        rejected: list[JSONValue] = []
        for position, merge in enumerate(merges):
            try:
                experience_id = _apply_merge(
                    session=session, merge=merge, original=original, free_ids=free_ids
                )
            except (ValueError, MemoryCatalogIntegrityError) as exc:
                rejected.append({"merge": position, "message": str(exc)})
            else:
                merged.append(
                    {
                        "experience_id": experience_id,
                        "supersedes": list(merge["supersedes"]),
                    }
                )
        output: dict[str, JSONValue] = {
            "status": "success",
            "merged": merged,
            "rejected": rejected,
            "index_characters": len(experience_index(session.draft.documents)),
        }
        return success(call.tool_name, output)

    text_field: JSONValue = {"type": "string", "minLength": 1}
    return tool(
        name=MERGE_EXPERIENCES_TOOL_NAME,
        description=(
            "Replace each group of lessons named in `supersedes` with one new "
            "lesson written from them. The new lesson takes the group's Scope "
            "and a new Experience ID; the superseded files are deleted. A "
            "rejected merge changes nothing and is listed with its reason."
        ),
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["merges"],
            "properties": {
                "merges": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "supersedes",
                            "applies_when",
                            "observed_approach",
                            "outcome",
                            "next_time_rule",
                            "observed_result",
                        ],
                        "properties": {
                            "supersedes": {
                                "type": "array",
                                "minItems": 2,
                                "uniqueItems": True,
                                "items": {"type": "string"},
                                "description": "Experience IDs of the lessons merged.",
                            },
                            "applies_when": {
                                **text_field,
                                "description": "Specific condition, one line.",
                            },
                            "observed_approach": {
                                **text_field,
                                "description": "Approach actually observed in the evidence.",
                            },
                            "outcome": {
                                "type": "string",
                                "enum": list(get_args(AgentExperienceOutcome)),
                            },
                            "next_time_rule": {
                                **text_field,
                                "description": (
                                    "What to repeat, or what to avoid and do instead."
                                ),
                            },
                            "observed_result": {
                                **text_field,
                                "description": "Evidence-grounded result.",
                            },
                        },
                    },
                }
            },
        },
        response_schema=response_schema(
            required=("status", "merged", "rejected", "index_characters"),
            properties={
                "merged": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["experience_id", "supersedes"],
                        "properties": {
                            "experience_id": {"type": "string"},
                            "supersedes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
                "rejected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["merge", "message"],
                        "properties": {
                            "merge": {"type": "integer"},
                            "message": {"type": "string"},
                        },
                    },
                },
                "index_characters": {"type": "integer"},
            },
        ),
        execute=execute,
    )


def _apply_merge(
    *,
    session: MemoryDraftToolSession,
    merge: _Merge,
    original: set[str],
    free_ids: list[str],
) -> str:
    entries = {
        experience_id_from_path(document.source_path): document
        for document in session.draft.documents
        if document.source_path != AGENT_EXPERIENCE_INDEX_PATH
    }
    superseded: list[AgentExperienceContent] = []
    for experience_id in merge["supersedes"]:
        if experience_id not in original:
            raise ValueError(f"{experience_id} is not one of the lessons given")
        if experience_id not in entries:
            raise ValueError(f"{experience_id} was already merged")
        superseded.append(
            parse_agent_experience_markdown(
                entries[experience_id].content, expected_experience_id=experience_id
            )
        )
    scopes = {entry.scope for entry in superseded}
    if len(scopes) != 1:
        raise ValueError("only lessons with the same Scope can be merged")
    if not free_ids:
        raise ValueError("this run has used every new Experience ID")
    content = AgentExperienceContent(
        experience_id=free_ids[0],
        scope=superseded[0].scope,
        applies_when=merge["applies_when"],
        observed_approach=merge["observed_approach"],
        outcome=merge["outcome"],
        next_time_rule=merge["next_time_rule"],
        observed_result=merge["observed_result"],
        supersedes_experience_ids=tuple(merge["supersedes"]),
    )
    removed = {experience_entry_path(item) for item in merge["supersedes"]}
    session.draft = replace_draft_documents(
        draft=session.draft,
        documents=(
            *(
                item
                for item in session.draft.documents
                if item.source_path not in removed
            ),
            MemoryDocument(
                experience_entry_path(content.experience_id),
                render_agent_experience_markdown(content),
            ),
        ),
    )
    return free_ids.pop(0)


def entry_written_dates(
    *, connection: sqlite3.Connection, draft: MemoryDraftCheckpoint
) -> dict[str, str]:
    """When each entry's current text was first published, by path."""

    published = {
        (str(row["source_path"]), str(row["content_sha256"])): str(row["written_at"])
        for row in connection.execute(
            """
            SELECT fragments.source_path, fragments.content_sha256,
                   MIN(revisions.created_at) AS written_at
            FROM memory_fragments AS fragments
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id
             AND revisions.revision_id = fragments.revision_id
            WHERE revisions.user_id = ? AND revisions.node_id = ?
              AND fragments.block_kind = 'document_root'
            GROUP BY fragments.source_path, fragments.content_sha256
            """,
            (draft.user_id, draft.owner_node_id),
        )
    }
    return {
        document.source_path: published[
            (document.source_path, content_sha256(document.content))
        ]
        for document in draft.documents
        if document.source_path != AGENT_EXPERIENCE_INDEX_PATH
    }


def experience_entries_text(
    draft: MemoryDraftCheckpoint, written: dict[str, str]
) -> str:
    """Every entry with the date its text was written, oldest first."""

    return "\n".join(
        f'<entry written="{written[document.source_path][:10]}">\n'
        f"{document.content.rstrip()}\n</entry>"
        for document in sorted(
            (
                item
                for item in draft.documents
                if item.source_path != AGENT_EXPERIENCE_INDEX_PATH
            ),
            key=lambda item: (written[item.source_path], item.source_path),
        )
    )


def experience_index(documents: tuple[MemoryDocument, ...]) -> str:
    """The index publication will write for these entries."""

    return next(
        document.content
        for document in rebuild_agent_experience_index(documents)
        if document.source_path == AGENT_EXPERIENCE_INDEX_PATH
    )


__all__ = [
    "MERGE_EXPERIENCES_TOOL_NAME",
    "entry_written_dates",
    "experience_entries_text",
    "experience_index",
    "merge_experiences_tool",
]
