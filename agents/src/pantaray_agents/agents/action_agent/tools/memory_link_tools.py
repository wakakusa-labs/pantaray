from __future__ import annotations

from pantaray_agents.tools.contract import ToolConcurrency

from .base import (
    FieldSpec,
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolIntentClass,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)


def _tool(
    *,
    tool_id: str,
    name: str,
    description: str,
    when: str,
    pitfalls: str,
    fields: tuple[FieldSpec, ...],
    intent_class: ToolIntentClass,
    concurrency: ToolConcurrency,
    output_properties: dict[str, object],
    output_required: tuple[str, ...],
) -> ToolDefinition:
    return ToolDefinition.from_spec(
        ToolSpec(
            tool_id=tool_id,
            name=name,
            description=description,
            guide=ToolGuideSpec(what=description, when=when, pitfalls=pitfalls),
            concurrency=concurrency,
            execution_policy=tool_execution_policy(
                intent_class=intent_class,
                default_timeout_ms=30_000,
            ),
            input_spec=InputSpec(fields=fields),
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": output_properties,
                "required": list(output_required),
            },
        )
    )


_LINK_WHEN = "Use only when the relevant persisted memory is visible in this turn."
_LINK_PITFALLS = (
    "Never write [[ref:...]] syntax yourself. Context handles come from "
    "memory_search and get_memory_reference results and must not be invented."
)

LINK_MEMORY_TOOL = _tool(
    tool_id="link_memory",
    name="Link Memory",
    description=(
        "Attach a note-bearing semantic ref to one exact block in the Supervisor "
        "final-answer draft, targeting a memory fragment visible in this turn."
    ),
    when=_LINK_WHEN,
    pitfalls=_LINK_PITFALLS,
    intent_class="surgical_edit",
    concurrency=ToolConcurrency("sequential"),
    fields=(
        field_spec(
            name="target_handle",
            schema={"type": "string", "minLength": 1},
            required=True,
            description="context_handle shown with the target memory fragment.",
        ),
        field_spec(
            name="source_path",
            schema={"type": "string", "const": "body.md"},
            required=True,
            description="Action draft path; use body.md.",
        ),
        field_spec(
            name="exact_text",
            schema={"type": "string", "minLength": 1},
            required=True,
            description="Exact paragraph or list-item text that should receive the ref.",
        ),
        field_spec(
            name="occurrence",
            schema={"type": "integer", "minimum": 1},
            required=True,
            description="1-based occurrence when exact_text repeats.",
        ),
        field_spec(
            name="note",
            schema={"type": "string", "minLength": 1},
            required=True,
            description="Single-line explanation of how the two memories relate.",
        ),
        field_spec(
            name="expected_draft_revision",
            schema={"type": "string", "pattern": "^sha256:"},
            required=True,
            description="draft_revision returned by the latest draft_final_answer, link_memory or unlink_memory.",
        ),
    ),
    output_properties={
        "status": {"type": "string", "const": "linked"},
        "local_ref_id": {"type": "string", "pattern": "^ref_"},
        "draft_revision": {"type": "string", "pattern": "^sha256:"},
    },
    output_required=("status", "local_ref_id", "draft_revision"),
)

UNLINK_MEMORY_TOOL = _tool(
    tool_id="unlink_memory",
    name="Unlink Memory",
    description="Remove one complete semantic ref from the Supervisor final-answer draft.",
    when=_LINK_WHEN,
    pitfalls=_LINK_PITFALLS,
    intent_class="surgical_edit",
    concurrency=ToolConcurrency("sequential"),
    fields=(
        field_spec(
            name="local_ref_id",
            schema={"type": "string", "pattern": "^ref_"},
            required=True,
            description="local_ref_id returned by link_memory.",
        ),
        field_spec(
            name="expected_draft_revision",
            schema={"type": "string", "pattern": "^sha256:"},
            required=True,
            description="draft_revision returned by the latest draft_final_answer, link_memory or unlink_memory.",
        ),
    ),
    output_properties={
        "status": {"type": "string", "const": "unlinked"},
        "draft_revision": {"type": "string", "pattern": "^sha256:"},
    },
    output_required=("status", "draft_revision"),
)

GET_MEMORY_REFERENCE_TOOL = _tool(
    tool_id="get_memory_reference",
    name="Get Memory Reference",
    description=(
        "Resolve one [[ref:...]] tag seen in memory, such as in a memory file you "
        "read, the memory indexes or a memory_search result, by its ID. The "
        "relationship note is returned before the immutable target fragment."
    ),
    when="Use when a ref in memory points to something the task needs.",
    pitfalls="Pass the ID exactly as the tag shows it.",
    intent_class="read_only",
    # Reads memory_context_epoch and writes the extended epoch back; two at
    # once would lose one extension and leave later handles unresolvable.
    concurrency=ToolConcurrency("parallel", shared_state="memory_context_epoch"),
    fields=(
        field_spec(
            name="local_ref_id",
            schema={"type": "string", "pattern": "^ref_"},
            required=True,
            description="The ID after ref: in the tag.",
        ),
    ),
    output_properties={
        "local_ref_id": {"type": "string", "pattern": "^ref_"},
        "reference_note": {"type": "string"},
        "source_path": {"type": "string"},
        "source_heading_path": {"type": ["string", "null"]},
        "target_fragment_id": {"type": "string"},
        "target_source": {"type": "string"},
        "target_content": {"type": "string"},
        "target_path": {"type": "string"},
        "target_heading_path": {"type": ["string", "null"]},
        "target_revision_id": {"type": "string"},
        "target_is_current": {"type": "boolean"},
        "target_lifecycle": {"type": "string"},
        "target_integrity": {"type": "string"},
        "current_target_revision_id": {"type": "string"},
        "target_context_handle": {"type": "string", "minLength": 1},
    },
    output_required=(
        "local_ref_id",
        "reference_note",
        "source_path",
        "source_heading_path",
        "target_fragment_id",
        "target_source",
        "target_content",
        "target_path",
        "target_heading_path",
        "target_revision_id",
        "target_is_current",
        "target_lifecycle",
        "target_integrity",
        "current_target_revision_id",
        "target_context_handle",
    ),
)
