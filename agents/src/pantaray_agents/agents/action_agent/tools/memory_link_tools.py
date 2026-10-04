from __future__ import annotations

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
    fields: tuple[FieldSpec, ...],
    intent_class: ToolIntentClass,
    output_properties: dict[str, object],
    output_required: tuple[str, ...],
) -> ToolDefinition:
    return ToolDefinition.from_spec(
        ToolSpec(
            tool_id=tool_id,
            name=name,
            description=description,
            guide=ToolGuideSpec(
                what=description,
                when="Use only when the relevant persisted memory is visible in this turn.",
                pitfalls=(
                    "Never write [[ref:...]] syntax yourself. Context handles are scoped "
                    "to the rendered memory context and must not be invented."
                ),
            ),
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


LINK_MEMORY_TOOL = _tool(
    tool_id="link_memory",
    name="Link Memory",
    description=(
        "Attach a note-bearing semantic ref to one exact block in the Supervisor "
        "final-answer draft, targeting a memory fragment visible in this turn."
    ),
    intent_class="surgical_edit",
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
    intent_class="surgical_edit",
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
        "Resolve one ref from an exact source memory fragment visible in this turn. "
        "The relationship note is returned before the immutable target fragment."
    ),
    intent_class="read_only",
    fields=(
        field_spec(
            name="source_handle",
            schema={"type": "string", "minLength": 1},
            required=True,
            description="context_handle of the source fragment containing the ref.",
        ),
        field_spec(
            name="local_ref_id",
            schema={"type": "string", "pattern": "^ref_"},
            required=True,
            description="Local ref ID shown in that source fragment.",
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
