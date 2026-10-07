from __future__ import annotations

from pantaray_agents.tools.contract import TOOL_ERROR_RESPONSE_SCHEMA, JsonSchema

READ_FILE_TOOL_NAME = "read_file"
SEARCH_FILES_TOOL_NAME = "search_files"
APPLY_PATCH_TOOL_NAME = "apply_patch"
WRITE_FILE_TOOL_NAME = "write_file"
SHELL_TOOL_NAME = "shell"
LINK_MEMORY_TOOL_NAME = "link_memory"
UNLINK_MEMORY_TOOL_NAME = "unlink_memory"
MOVE_MEMORY_FILE_TOOL_NAME = "move_memory_file"
DELETE_MEMORY_FILE_TOOL_NAME = "delete_memory_file"
MAX_PATCH_CHUNKS = 50
MAX_PATCH_LINES_PER_CHUNK = 80
MAX_PATCH_TOTAL_LINES = 800


def link_memory_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "target_handle",
            "source_path",
            "exact_text",
            "occurrence",
            "note",
            "expected_draft_revision",
        ],
        "properties": {
            "target_handle": {
                "type": "string",
                "minLength": 1,
                "description": "Visible ctx_ handle of the persisted target evidence.",
            },
            "source_path": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Editable draft file that receives the inline ref; never the "
                    "target evidence path."
                ),
            },
            "exact_text": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Exact current draft paragraph or list-item text that receives "
                    "the inline ref."
                ),
            },
            "occurrence": {
                "type": "integer",
                "minimum": 1,
                "description": "One-based occurrence of exact_text in source_path.",
            },
            "note": {
                "type": "string",
                "minLength": 1,
                "description": "Useful semantic relationship from source to target.",
            },
            "expected_draft_revision": {
                "type": "string",
                "pattern": "^sha256:",
                "description": (
                    "Exact current draft_revision from the initial draft state or "
                    "the latest successful draft tool result."
                ),
            },
        },
    }


def unlink_memory_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["local_ref_id", "expected_draft_revision"],
        "properties": {
            "local_ref_id": {"type": "string", "pattern": "^ref_"},
            "expected_draft_revision": {
                "type": "string",
                "pattern": "^sha256:",
            },
        },
    }


def move_memory_file_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_path", "destination_path", "expected_draft_revision"],
        "properties": {
            "source_path": {"type": "string", "minLength": 1},
            "destination_path": {"type": "string", "minLength": 1},
            "expected_draft_revision": {
                "type": "string",
                "pattern": "^sha256:",
            },
        },
    }


def delete_memory_file_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "expected_draft_revision"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "expected_draft_revision": {
                "type": "string",
                "pattern": "^sha256:",
            },
        },
    }


def apply_patch_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "chunks"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "chunks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PATCH_CHUNKS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["lines"],
                    "properties": {
                        "lines": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_PATCH_LINES_PER_CHUNK,
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


def apply_patch_response_schema() -> JsonSchema:
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "path", "changed", "diff_summary"],
                "properties": {
                    "status": {"type": "string", "enum": ["success"]},
                    "path": {"type": "string"},
                    "changed": {"type": "boolean"},
                    "diff_summary": {"type": "string"},
                    "draft_revision": {"type": "string", "pattern": "^sha256:"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "status",
                    "path",
                    "patch_applied",
                    "read_scope",
                    "file_truncated",
                    "text",
                    "windows",
                    "remaining_chunk_count",
                    "retry_advice",
                ],
                "properties": {
                    "status": {"type": "string", "enum": ["needs_read"]},
                    "path": {"type": "string"},
                    "patch_applied": {"type": "boolean", "enum": [False]},
                    "read_scope": {
                        "type": "string",
                        "enum": ["full_file", "target_windows"],
                    },
                    "file_truncated": {"type": "boolean"},
                    "text": {"type": "string"},
                    "windows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "start_line",
                                "end_line",
                                "match_reason",
                                "text",
                            ],
                            "properties": {
                                "start_line": {"type": "integer", "minimum": 1},
                                "end_line": {"type": "integer", "minimum": 1},
                                "match_reason": {"type": "string", "minLength": 1},
                                "text": {"type": "string"},
                            },
                        },
                    },
                    "remaining_chunk_count": {"type": "integer", "minimum": 0},
                    "retry_advice": {"type": "string", "minLength": 1},
                },
            },
            dict(TOOL_ERROR_RESPONSE_SCHEMA),
        ]
    }


def shell_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["cmd"],
        "properties": {"cmd": {"type": "string"}},
    }
