from __future__ import annotations

from pantaray_agents.tools.contract import JsonSchema, react_tool_response_schema

ARTIFACT_PATCH_TOOL_NAME = "artifact_patch"
COMPLETED_TOOL_NAME = "completed"
MAX_STRUCTURED_PATCH_CHUNKS = 50
MAX_STRUCTURED_PATCH_LINES_PER_CHUNK = 80

CHUNKS_DESCRIPTION = """Use artifact_patch to edit the current artifact document.

Represent each edit as structured chunks. A chunk identifies one nearby ordered
span in the Current Document and replaces only the requested lines inside that
span.

Each line has:
- op: the edit operation for this physical Markdown line.
- text: the line text without any patch prefix. Copy Markdown text as-is.

Line operations:
- context: an unchanged line copied from the current document only. Use it only
  to locate the edit.
- remove: a line copied from the current document only. Use it only for existing
  lines that should disappear from the output.
- add: a new or rewritten output line. Use add for every line that is not already
  present in the current document.

Rules:
- context and remove text must identify one current-document line. Prefer copying
  the full line byte-for-byte. For long lines, you may abbreviate the middle as
  <OMITTED>; the prefix and suffix around <OMITTED> must still be copied
  byte-for-byte from the same line.
- Use <OMITTED> at most once per context/remove line. It means "match any middle
  text in this one line"; it is not matched as literal document text.
- In add lines, <OMITTED> has no special meaning and is inserted literally.
- Never put new wording, evidence text, examples, or intended output in context
  or remove. Use add for new wording.
- Preserve whitespace, indentation, punctuation, backticks, inline references, and blank lines.
- Do not copy examples or reference structures into context/remove text.
- Include only hunks relevant to the requested change.
- Every chunk must include at least one add or remove line.
- Include enough context lines to identify exactly one location.
- A blank current-document line is represented as {"op":"context","text":""}.
- Markdown bullet markers belong in text. Do not encode edit intent in text.

Examples:
- To keep a Markdown bullet as context: {"op":"context","text":"- Current Focus"}
- To remove a Markdown bullet: {"op":"remove","text":"- Current Focus"}
- To add a Markdown bullet: {"op":"add","text":"- Current Focus"}
"""


def artifact_patch_request_schema(*, logical_path: str) -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["chunks"],
        "description": (
            "Arguments for artifact_patch. Provide structured edit chunks for "
            f"{logical_path}."
        ),
        "properties": {
            "chunks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STRUCTURED_PATCH_CHUNKS,
                "description": CHUNKS_DESCRIPTION,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["lines"],
                    "properties": {
                        "lines": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_STRUCTURED_PATCH_LINES_PER_CHUNK,
                            "contains": {
                                "type": "object",
                                "required": ["op"],
                                "properties": {
                                    "op": {
                                        "type": "string",
                                        "enum": ["remove", "add"],
                                    }
                                },
                            },
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["op", "text"],
                                "properties": {
                                    "op": {
                                        "type": "string",
                                        "enum": ["context", "remove", "add"],
                                    },
                                    "text": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            },
        },
    }


def artifact_patch_response_schema() -> JsonSchema:
    return react_tool_response_schema(
        success_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "storage_path", "sha256", "base_sha256"],
            "properties": {
                "status": {"type": "string", "enum": ["success"]},
                "storage_path": {"type": "string"},
                "sha256": {"type": "string"},
                "base_sha256": {"type": "string"},
            },
        },
    )
