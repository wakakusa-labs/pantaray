from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling.tool_result_reading import (
    ToolResultLoadError,
    ToolResultTextOffsetError,
    read_tool_result_text_prefix,
)
from pantaray_agents.local_runtime.tooling.workspace_manifest_roots import (
    load_ready_manifest_root_in_connection,
    validate_manifest_root_authority,
)
from pantaray_agents.local_runtime.tooling.workspace_root_authority import (
    WorkspaceRootAuthorityError,
)
from pantaray_agents.schema.action_conversation import (
    ActionConversationIdentity,
    ActionToolOutputDetail,
)

from .cursor_codec import (
    OpaqueCursorError,
    decode_opaque_cursor,
    encode_opaque_cursor,
)
from .sqlite_visibility import register_action_tool_visibility_sqlite
from .tool_authority import (
    ActionToolOutputAuthority,
    ActionToolOutputIntegrityError,
    load_action_tool_output_authority_in_connection,
)

ACTION_TOOL_OUTPUT_MIN_PAGE_BYTES = 4
ACTION_TOOL_OUTPUT_DEFAULT_PAGE_BYTES = 16_384
ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES = 65_536
ACTION_TOOL_OUTPUT_MAX_READ_BYTES = 1_048_576


class _ActionToolOutputCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: ActionConversationIdentity
    action_id: ActionConversationIdentity
    step_id: ActionConversationIdentity
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    next_byte: Annotated[int, Field(gt=0, le=ACTION_TOOL_OUTPUT_MAX_READ_BYTES)]


_CURSOR_ADAPTER: TypeAdapter[_ActionToolOutputCursor] = TypeAdapter(
    _ActionToolOutputCursor
)


def load_action_tool_output_detail(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    step_id: str,
    cursor: str | None,
    limit_bytes: int,
) -> ActionToolOutputDetail | None:
    """Authorize and page one Tool output with one caller-owned connection."""

    cursor_payload = (
        None
        if cursor is None
        else decode_opaque_cursor(cursor, adapter=_CURSOR_ADAPTER)
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        register_action_tool_visibility_sqlite(connection)
        connection.execute("BEGIN")
        authority = load_action_tool_output_authority_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            step_id=step_id,
        )
        if authority is None:
            return None
        content_identity = _content_identity(authority)
        start_byte = _resolve_start_byte(
            cursor=cursor_payload,
            authority=authority,
            content_identity=content_identity,
        )
        tool_results_root = (
            _load_tool_results_root(connection=connection, authority=authority)
            if authority.output.output_storage_kind == "action_file"
            else None
        )
        try:
            prefix = read_tool_result_text_prefix(
                action_tool_results_path=tool_results_root,
                output_owner_id=authority.output_owner_id,
                output=authority.output.output,
                storage_kind=authority.output.output_storage_kind,
                start_byte=start_byte,
                page_bytes=limit_bytes,
                max_read_bytes=ACTION_TOOL_OUTPUT_MAX_READ_BYTES,
            )
        except ToolResultTextOffsetError as exc:
            raise OpaqueCursorError("cursor has an invalid byte position") from exc
        except ToolResultLoadError as exc:
            raise ActionToolOutputIntegrityError(
                "Action tool output storage is inconsistent"
            ) from exc
    if prefix.unavailable_reason is not None:
        return ActionToolOutputDetail(
            content="",
            next_cursor=None,
            truncated=False,
            unavailable_reason=prefix.unavailable_reason,
        )
    next_cursor = (
        None
        if prefix.next_byte_offset is None
        else encode_opaque_cursor(
            _ActionToolOutputCursor(
                user_id=user_id,
                action_id=action_id,
                step_id=step_id,
                content_sha256=content_identity,
                next_byte=prefix.next_byte_offset,
            ),
            adapter=_CURSOR_ADAPTER,
        )
    )
    return ActionToolOutputDetail(
        content=prefix.content,
        next_cursor=next_cursor,
        truncated=(
            next_cursor is None
            and prefix.total_bytes > ACTION_TOOL_OUTPUT_MAX_READ_BYTES
        ),
        unavailable_reason=None,
    )


def _resolve_start_byte(
    *,
    cursor: _ActionToolOutputCursor | None,
    authority: ActionToolOutputAuthority,
    content_identity: str,
) -> int:
    if cursor is None:
        return 0
    if (
        cursor.user_id != authority.user_id
        or cursor.action_id != authority.action_id
        or cursor.step_id != authority.step_id
    ):
        raise OpaqueCursorError("cursor is bound to another Action tool output")
    if cursor.content_sha256 != content_identity:
        raise OpaqueCursorError("cursor has an invalid content identity")
    return cursor.next_byte


def _content_identity(authority: ActionToolOutputAuthority) -> str:
    payload = json.dumps(
        {
            "output": authority.output.model_dump(mode="json"),
            "output_owner_id": authority.output_owner_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_tool_results_root(
    *, connection: sqlite3.Connection, authority: ActionToolOutputAuthority
) -> Path:
    manifest = connection.execute(
        """SELECT manifest_id FROM workspace_manifests
           WHERE user_id = ? AND action_id = ? AND status = 'ready'""",
        (authority.user_id, authority.action_id),
    ).fetchone()
    root = (
        None
        if manifest is None
        else load_ready_manifest_root_in_connection(
            connection=connection,
            user_id=authority.user_id,
            manifest_id=str(manifest["manifest_id"]),
            action_id=authority.action_id,
            root_id=f"root:{authority.action_id}:tool-results",
        )
    )
    if root is None:
        raise ActionToolOutputIntegrityError(
            "Action tool-results manifest root is unavailable"
        )
    try:
        validate_manifest_root_authority(root)
    except WorkspaceRootAuthorityError as exc:
        raise ActionToolOutputIntegrityError(
            "Action tool-results manifest root is inconsistent"
        ) from exc
    return root.canonical_real_path
