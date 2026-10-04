"""Durable reference extraction for Action-owned tool-result files."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import ValidationError

from pantaray_agents.schema.tool_result import FormalToolStepOutput

from ..storage.migrations import MigrationError
from .repository.common import BUSY_TIMEOUT_PRAGMA_TEMPLATE
from .tool_result_spill import is_json_spill_shape
from .tool_result_storage import (
    ACTION_TOOL_RESULTS_DIRNAME,
    TOOL_RESULT_BINARY_MEDIA_TYPE,
    TOOL_RESULT_JSON_MEDIA_TYPE,
)

_FINAL_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.(?:bin|json)$")
_BINARY_METADATA_KEYS = frozenset({"storage", "path", "media_type", "byte_size"})


class ToolResultRecoveryError(MigrationError):
    """Tool-result references or managed storage are unsafe to reconcile."""


ToolResultReferenceSourceKind = Literal["tool_invocation", "action_step"]


@dataclass(frozen=True, slots=True)
class StoredToolResultReference:
    path: Path
    source_kind: ToolResultReferenceSourceKind
    source_id: str


@dataclass(frozen=True, slots=True)
class _ActionFileReference:
    path: Path
    media_type: str
    byte_size: int
    character_count: int | None
    line_count: int | None


def load_stored_tool_result_references(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    workspace_root: Path,
) -> frozenset[StoredToolResultReference]:
    if busy_timeout_ms <= 0:
        raise ToolResultRecoveryError(
            "LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer"
        )
    terminal_references: dict[Path, _ActionFileReference] = {}
    references: set[StoredToolResultReference] = set()
    invocation_references_by_step: defaultdict[str, set[_ActionFileReference]] = (
        defaultdict(set)
    )
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                BUSY_TIMEOUT_PRAGMA_TEMPLATE.format(timeout_ms=busy_timeout_ms)
            )
            connection.execute("PRAGMA query_only = ON;")
            connection.execute("BEGIN")
            for row in connection.execute(
                """
                SELECT
                    invocations.invocation_id,
                    invocations.user_id,
                    invocations.action_id,
                    invocations.step_id,
                    outputs.output_json,
                    outputs.output_storage_kind
                FROM tool_outputs AS outputs
                JOIN tool_invocations AS invocations
                  ON invocations.invocation_id = outputs.invocation_id
                """
            ):
                invocation_id = str(row["invocation_id"])
                reference = _terminal_reference_from_json_column(
                    raw_json=row["output_json"],
                    storage_kind=row["output_storage_kind"],
                    workspace_root=workspace_root,
                    user_id=str(row["user_id"]),
                    action_id=str(row["action_id"]),
                    invocation_id=invocation_id,
                )
                if reference is None:
                    continue
                existing = terminal_references.get(reference.path)
                if existing is not None and existing != reference:
                    raise ToolResultRecoveryError(
                        "tool_outputs contains conflicting action-file metadata"
                    )
                terminal_references[reference.path] = reference
                references.add(
                    StoredToolResultReference(
                        path=reference.path,
                        source_kind="tool_invocation",
                        source_id=invocation_id,
                    )
                )
                step_id = row["step_id"]
                if isinstance(step_id, str) and step_id:
                    invocation_references_by_step[step_id].add(reference)
            for row in connection.execute(
                """
                SELECT step_id, user_id, action_id, tool_output
                FROM agent_action_steps
                WHERE tool_output IS NOT NULL
                """
            ):
                step_id = str(row["step_id"])
                for reference in _step_references_from_json_column(
                    raw_json=row["tool_output"],
                    workspace_root=workspace_root,
                    user_id=str(row["user_id"]),
                    action_id=str(row["action_id"]),
                    step_id=step_id,
                    linked_invocation_references=frozenset(
                        invocation_references_by_step.get(step_id, ())
                    ),
                ):
                    references.add(
                        StoredToolResultReference(
                            path=reference.path,
                            source_kind="action_step",
                            source_id=step_id,
                        )
                    )
            connection.commit()
    except sqlite3.Error as exc:
        raise ToolResultRecoveryError(
            "failed to load durable tool-result references"
        ) from exc
    return frozenset(references)


def _terminal_reference_from_json_column(
    *,
    raw_json: object,
    storage_kind: object,
    workspace_root: Path,
    user_id: str,
    action_id: str,
    invocation_id: str,
) -> _ActionFileReference | None:
    if storage_kind is None and raw_json is None:
        return None
    if storage_kind == "inline_json" and raw_json is not None:
        return None
    if storage_kind != "action_file" or raw_json is None:
        raise ToolResultRecoveryError(
            "tool_outputs has an invalid output storage contract"
        )
    payload = _parse_json_column(
        raw_json=raw_json,
        column_name="tool_outputs.output_json",
    )
    if not isinstance(payload, dict):
        raise ToolResultRecoveryError(
            "action-file tool output metadata must be a JSON object"
        )
    action_root = _action_tool_results_root(
        workspace_root=workspace_root,
        user_id=user_id,
        action_id=action_id,
    )
    return _validate_action_file_metadata(
        value=payload,
        action_root=action_root,
        owner_name=_storage_owner_name(invocation_id),
        column_name="tool_outputs.output_json",
    )


def _step_references_from_json_column(
    *,
    raw_json: object,
    workspace_root: Path,
    user_id: str,
    action_id: str,
    step_id: str,
    linked_invocation_references: frozenset[_ActionFileReference],
) -> set[_ActionFileReference]:
    payload = _parse_formal_step_output(raw_json=raw_json)
    output = payload.output
    output_storage_kind = payload.output_storage_kind
    output_owner_kind = payload.output_owner_kind
    action_root = _action_tool_results_root(
        workspace_root=workspace_root,
        user_id=user_id,
        action_id=action_id,
    )
    if output_storage_kind == "action_file":
        if output_owner_kind == "tool_invocation":
            if not isinstance(output, dict):
                raise ToolResultRecoveryError(
                    "invocation-owned action-file output must contain metadata"
                )
            reference = _validate_action_file_metadata(
                value=output,
                action_root=action_root,
                owner_name=None,
                column_name="agent_action_steps.tool_output",
            )
            if reference not in linked_invocation_references:
                raise ToolResultRecoveryError(
                    "invocation-owned formal output disagrees with linked tool_outputs"
                )
            return {reference}
        if not isinstance(output, dict):
            raise ToolResultRecoveryError(
                "action-file formal output must contain metadata"
            )
        return {
            _validate_action_file_metadata(
                value=output,
                action_root=action_root,
                owner_name=_storage_owner_name(step_id),
                column_name="agent_action_steps.tool_output",
            )
        }
    if output_storage_kind != "inline_json":
        raise ToolResultRecoveryError(
            "formal tool output has an invalid storage contract"
        )
    if payload.status == "processing" and output_owner_kind == "action_step":
        return _approval_summary_reference(
            output=output,
            action_root=action_root,
        )
    return set()


def _parse_formal_step_output(*, raw_json: object) -> FormalToolStepOutput:
    if not isinstance(raw_json, str):
        raise ToolResultRecoveryError(
            "agent_action_steps.tool_output must contain JSON text"
        )
    try:
        return FormalToolStepOutput.model_validate_json(raw_json)
    except ValidationError as exc:
        raise ToolResultRecoveryError(
            "agent_action_steps.tool_output violates the formal tool-step contract"
        ) from exc


def _approval_summary_reference(
    *, output: object, action_root: Path
) -> set[_ActionFileReference]:
    if not isinstance(output, dict) or output.get("kind") != "approval_required":
        return set()
    tool_request_id = output.get("tool_request_id")
    command_summary = output.get("command_summary")
    storage_kind = output.get("command_summary_storage_kind")
    if storage_kind == "inline_json" and isinstance(command_summary, dict):
        return set()
    if storage_kind != "action_file":
        raise ToolResultRecoveryError(
            "approval command summary has an invalid storage contract"
        )
    if not isinstance(tool_request_id, str) or not isinstance(command_summary, dict):
        raise ToolResultRecoveryError(
            "action-file approval command summary must include owner and metadata"
        )
    return {
        _validate_action_file_metadata(
            value=command_summary,
            action_root=action_root,
            owner_name=_storage_owner_name(tool_request_id),
            column_name="agent_action_steps.tool_output",
        )
    }


def _parse_json_column(*, raw_json: object, column_name: str) -> object:
    if not isinstance(raw_json, str):
        raise ToolResultRecoveryError(f"{column_name} must contain JSON text")
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ToolResultRecoveryError(f"invalid JSON in {column_name}") from exc


def _storage_owner_name(owner_id: str) -> str:
    if not _is_path_component(owner_id):
        raise ToolResultRecoveryError("tool-result reference has an invalid owner id")
    return quote(owner_id, safe="-_")


def _validate_action_file_metadata(
    *,
    value: Mapping[str, object],
    action_root: Path,
    owner_name: str | None,
    column_name: str,
) -> _ActionFileReference:
    if value.get("storage") != "action_file":
        raise ToolResultRecoveryError(
            f"invalid action-file storage marker in {column_name}"
        )
    media_type = value.get("media_type")
    if media_type == TOOL_RESULT_JSON_MEDIA_TYPE:
        shape_is_valid = is_json_spill_shape(set(value))
        expected_suffix = ".json"
        size_fields: tuple[str, ...] = (
            "byte_size",
            "character_count",
            "line_count",
        )
    elif media_type == TOOL_RESULT_BINARY_MEDIA_TYPE:
        shape_is_valid = set(value) == _BINARY_METADATA_KEYS
        expected_suffix = ".bin"
        size_fields = ("byte_size",)
    else:
        raise ToolResultRecoveryError(
            f"unsupported action-file media_type in {column_name}"
        )
    if not shape_is_valid:
        raise ToolResultRecoveryError(
            f"invalid action-file metadata shape in {column_name}"
        )
    if any(not _is_non_negative_integer(value[field]) for field in size_fields):
        raise ToolResultRecoveryError(
            f"invalid action-file size metadata in {column_name}"
        )
    raw_path = value["path"]
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ToolResultRecoveryError(
            f"action-file path must be absolute in {column_name}"
        )
    file_path = Path(os.path.normpath(raw_path))
    expected_parent = action_root / owner_name if owner_name is not None else None
    parent_is_valid = (
        file_path.parent == expected_parent
        if expected_parent is not None
        else (
            file_path.parent.parent == action_root
            and _is_path_component(file_path.parent.name)
        )
    )
    if (
        not parent_is_valid
        or file_path.suffix != expected_suffix
        or _FINAL_RESULT_FILE_PATTERN.fullmatch(file_path.name) is None
    ):
        raise ToolResultRecoveryError(
            f"action-file path is outside its Action tool-results root in {column_name}"
        )
    byte_size = value["byte_size"]
    assert isinstance(byte_size, int) and not isinstance(byte_size, bool)
    character_count = value.get("character_count")
    line_count = value.get("line_count")
    assert character_count is None or (
        isinstance(character_count, int) and not isinstance(character_count, bool)
    )
    assert line_count is None or (
        isinstance(line_count, int) and not isinstance(line_count, bool)
    )
    assert isinstance(media_type, str)
    return _ActionFileReference(
        path=file_path,
        media_type=media_type,
        byte_size=byte_size,
        character_count=character_count,
        line_count=line_count,
    )


def _action_tool_results_root(
    *, workspace_root: Path, user_id: str, action_id: str
) -> Path:
    if not _is_path_component(user_id) or not _is_path_component(action_id):
        raise ToolResultRecoveryError(
            "persisted tool-result owner has an invalid user_id or action_id"
        )
    return workspace_root / user_id / action_id / ACTION_TOOL_RESULTS_DIRNAME


def _is_path_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
    )


def _is_non_negative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "StoredToolResultReference",
    "ToolResultRecoveryError",
    "load_stored_tool_result_references",
]
