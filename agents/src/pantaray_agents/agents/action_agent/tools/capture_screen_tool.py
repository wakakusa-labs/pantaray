"""Screen capture tool definition."""

from __future__ import annotations

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

CAPTURE_SCREEN_TOOL_ID = "capture_screen"
CAPTURE_SCREEN_CAPABILITY = "screen_capture"
CAPTURE_SCREEN_TIMEOUT_MS = 15_000

CAPTURE_SCREEN_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=CAPTURE_SCREEN_TOOL_ID,
        name="Capture Screen",
        description="Capture one window of an open app as an image the next step can see.",
        guide=ToolGuideSpec(
            what=(
                "Take one screenshot of the frontmost window of the named app, even "
                "when other windows cover it, and attach it to this step so the very "
                "next reasoning step can look at it."
            ),
            when=(
                "Use to see the current state or look of an app, such as one you just "
                "operated or opened a file in. For pages of a PDF, Word, PowerPoint or "
                "Excel file, use render_pdf_page instead."
            ),
            pitfalls=(
                "Every call asks the user for permission, so do not call it "
                "speculatively or to poll for a change. Only the step immediately "
                "after the capture sees the image; call it again if you need a "
                "later look. A minimized or hidden window, or one on another desktop, "
                "cannot be captured. The capture can be refused by the user's "
                "recording filter or by a missing macOS Screen Recording permission; "
                "the error code says which, and no image exists in that case."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="screen_capture",
            required_capabilities=(CAPTURE_SCREEN_CAPABILITY,),
            default_timeout_ms=CAPTURE_SCREEN_TIMEOUT_MS,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="app_name",
                    schema={"type": "string", "minLength": 1, "pattern": r"\S"},
                    required=True,
                    description=(
                        "Name of an open app as its menu bar shows it, such as "
                        '"Microsoft Excel" or "Google Chrome".'
                    ),
                ),
            ),
            description="Capture one window of an open app.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["captured"]},
                "app_name": {
                    "type": "string",
                    "description": "App whose window was captured.",
                },
                "captured_at": {"type": "string"},
                "width_px": {"type": "integer"},
                "height_px": {"type": "integer"},
                "message": {"type": "string"},
                "attachments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["file"]},
                            "mime_type": {"type": "string"},
                            "path": {"type": "string"},
                            "ref": {"type": "string"},
                            "byte_size": {"type": "integer"},
                        },
                        "required": [
                            "type",
                            "mime_type",
                            "path",
                            "ref",
                            "byte_size",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "status",
                "app_name",
                "captured_at",
                "width_px",
                "height_px",
                "message",
                "attachments",
            ],
            "additionalProperties": False,
        },
    )
)

__all__ = [
    "CAPTURE_SCREEN_CAPABILITY",
    "CAPTURE_SCREEN_TIMEOUT_MS",
    "CAPTURE_SCREEN_TOOL",
    "CAPTURE_SCREEN_TOOL_ID",
]
