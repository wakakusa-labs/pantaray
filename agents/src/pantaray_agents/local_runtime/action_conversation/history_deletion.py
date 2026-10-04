"""Delete one conversation from history with its search copies and files.

An Action and the Suggestion it accepted or replied to are one conversation, so
either identity deletes both. Learned memory stays; only its links into the
deleted copies go.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathMissingError,
    open_directory_descriptor,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.local_image_store import (
    GENERIC_IMAGE_DIRECTORY,
)
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
    register_after_commit,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    ActionStoragePaths,
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    remove_descriptor_confined_directory_child,
)
from pantaray_agents.security.storage_paths import is_valid_image_storage_path

type HistoryItemKind = Literal["conversation", "suggestion"]


class ConversationBusyError(RuntimeError):
    """The conversation's own Action is still queued, running or paused."""


@dataclass(frozen=True, slots=True)
class _Conversation:
    action_id: str | None
    suggestion_ids: tuple[str, ...]


# The Action's runs, subagents and pipelines, and its Suggestions' own runs.
_CONVERSATION_PROCESS = """(
    processes.action_id = :action_id
    OR (processes.action_id IS NULL
        AND processes.suggestion_id IN (SELECT value FROM json_each(:suggestion_ids)))
)"""
_BUSY_SQL = f"""
SELECT
  EXISTS (SELECT 1 FROM agent_actions
          WHERE user_id = :user_id AND action_id = :action_id
            AND status IN ('queued', 'processing'))
  OR EXISTS (SELECT 1 FROM processes
             WHERE user_id = :user_id AND {_CONVERSATION_PROCESS}
               AND status IN ('enqueued', 'running', 'paused'))
  OR EXISTS (SELECT 1 FROM jobs JOIN processes ON processes.process_id = jobs.process_id
             WHERE processes.user_id = :user_id AND {_CONVERSATION_PROCESS}
               AND jobs.status IN ('queued', 'running', 'paused', 'retryable_error'))
"""
_COPY_NODES = """
SELECT node_id FROM memory_nodes
WHERE user_id = :user_id AND (
  (source_type = 'action' AND source_record_id = :action_id)
  OR (source_type = 'suggestion'
      AND source_record_id IN (SELECT value FROM json_each(:suggestion_ids)))
  OR (source_type = 'action_file_read' AND source_record_id IN (
      SELECT invocation_id FROM tool_invocations
      WHERE user_id = :user_id AND action_id = :action_id))
  OR (source_type = 'memory_note'
      AND substr(source_record_id, 1, length(:action_id) + 1) = :action_id || ':'))
"""
_COPY_REVISIONS = f"""
SELECT revision_id FROM memory_revisions
WHERE user_id = :user_id AND node_id IN ({_COPY_NODES})
"""
# The order matters: RESTRICT and NO ACTION references are removed before the
# rows they protect, and processes only after the steps that reference them.
_DELETE_STATEMENTS = (
    # Learned memory keeps its text; only its links into the copies go.
    f"""DELETE FROM memory_links WHERE user_id = :user_id AND target_fragment_id IN (
          SELECT fragment_id FROM memory_fragments
          WHERE user_id = :user_id AND revision_id IN ({_COPY_REVISIONS}))""",
    f"""DELETE FROM memory_evidence_edges WHERE user_id = :user_id
          AND source_revision_id IN ({_COPY_REVISIONS})""",
    # Fragments, FTS rows, embeddings and parents cascade from the revisions.
    f"DELETE FROM memory_revisions WHERE user_id = :user_id AND node_id IN ({_COPY_NODES})",
    f"DELETE FROM memory_nodes WHERE user_id = :user_id AND node_id IN ({_COPY_NODES})",
    """DELETE FROM memory_agent_triggers
       WHERE user_id = :user_id AND action_id = :action_id""",
    # Audits RESTRICT the approval sessions that cascade from the Action.
    """DELETE FROM command_invocation_audits WHERE invocation_id IN (
         SELECT invocation_id FROM tool_invocations
         WHERE user_id = :user_id AND action_id = :action_id)""",
    # Resource events only SET NULL their Action.
    """DELETE FROM tool_runtime_resource_events
       WHERE action_id = :action_id OR resource_id IN (
         SELECT resource_id FROM tool_runtime_resources WHERE action_id = :action_id)""",
    # Jobs only SET NULL their process; payloads and attempts cascade.
    f"""DELETE FROM jobs WHERE process_id IN (
          SELECT process_id FROM processes
          WHERE user_id = :user_id AND {_CONVERSATION_PROCESS})""",
    "DELETE FROM agent_actions WHERE user_id = :user_id AND action_id = :action_id",
    f"DELETE FROM processes WHERE user_id = :user_id AND {_CONVERSATION_PROCESS}",
    """DELETE FROM agent_suggestions WHERE user_id = :user_id
         AND suggestion_id IN (SELECT value FROM json_each(:suggestion_ids))""",
)
_RESOLVE_ACTION_SQL = """
SELECT action_id FROM agent_actions
WHERE user_id = :user_id AND (action_id = :action_id OR suggestion_id = :suggestion_id)
UNION
SELECT action_id FROM agent_action_steps
WHERE user_id = :user_id AND source_suggestion_id = :suggestion_id
"""
_LINKED_SUGGESTIONS_SQL = """
SELECT suggestion_id FROM agent_actions
WHERE user_id = ? AND action_id = ? AND suggestion_id IS NOT NULL
UNION
SELECT source_suggestion_id FROM agent_action_steps
WHERE user_id = ? AND action_id = ? AND source_suggestion_id IS NOT NULL
"""
_ACTION_IMAGES_SQL = """
SELECT json_extract(image.value, '$.storage_path')
FROM agent_action_steps AS step, json_each(step.user_message_json, '$.images') AS image
WHERE step.user_id = :user_id AND step.action_id = :action_id
UNION
SELECT json_extract(attachment.value, '$.storage_path')
FROM agent_action_steps AS step,
  json_each(step.tool_output, '$.output.attachments') AS attachment
WHERE step.user_id = :user_id AND step.action_id = :action_id
  AND json_extract(attachment.value, '$.source_kind') = 'local_image_blob'
"""
# Design limit: one scan of the user's steps per deleted image; add an image
# reference table when a delete with images exceeds 100 ms.
_IMAGE_REFERENCED_SQL = """
SELECT EXISTS (SELECT 1 FROM agent_action_steps
  WHERE user_id = ? AND (instr(user_message_json, ?) > 0 OR instr(tool_output, ?) > 0))
"""


def delete_history_item(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
    kind: HistoryItemKind,
    item_id: str,
) -> None:
    """Delete one history row's conversation; a missing row is already done.

    Raises:
        ConversationBusyError: the conversation's own run is still active.
    """

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            conversation = _resolve_conversation(
                connection=connection, user_id=user_id, kind=kind, item_id=item_id
            )
            if conversation is None:
                return
            params = {
                "user_id": user_id,
                "action_id": conversation.action_id,
                "suggestion_ids": json.dumps(conversation.suggestion_ids),
            }
            if connection.execute(_BUSY_SQL, params).fetchone()[0]:
                raise ConversationBusyError("conversation is running")
            storage = (
                resolve_action_storage_paths(
                    db_path=db_path, user_id=user_id, action_id=conversation.action_id
                )
                if conversation.action_id is not None
                else None
            )
            images = {
                str(row[0])
                for row in connection.execute(_ACTION_IMAGES_SQL, params)
                if isinstance(row[0], str)
                and is_valid_image_storage_path(user_id=user_id, storage_path=row[0])
            }
            for statement in _DELETE_STATEMENTS:
                connection.execute(statement, params)
            unreferenced = tuple(
                sorted(
                    path
                    for path in images
                    if not connection.execute(
                        _IMAGE_REFERENCED_SQL, (user_id, path, path)
                    ).fetchone()[0]
                )
            )
            # A file left behind only wastes space, but a row pointing at a
            # missing file breaks recovery, so files go only after commit.
            register_after_commit(
                connection=connection,
                callback=partial(
                    _remove_conversation_files,
                    storage=storage,
                    artifact_root=artifact_root,
                    image_paths=unreferenced,
                ),
            )


def _resolve_conversation(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    kind: HistoryItemKind,
    item_id: str,
) -> _Conversation | None:
    item = {"user_id": user_id, "action_id": None, "suggestion_id": None}
    item["action_id" if kind == "conversation" else "suggestion_id"] = item_id
    row = connection.execute(_RESOLVE_ACTION_SQL, item).fetchone()
    if row is not None:
        linked = connection.execute(_LINKED_SUGGESTIONS_SQL, (user_id, row[0]) * 2)
        return _Conversation(
            str(row[0]), tuple(sorted(str(link[0]) for link in linked))
        )
    standalone = connection.execute(
        "SELECT 1 FROM agent_suggestions WHERE user_id = ? AND suggestion_id = ?",
        (user_id, item["suggestion_id"]),
    ).fetchone()
    return None if standalone is None else _Conversation(None, (item_id,))


def _remove_conversation_files(
    *,
    storage: ActionStoragePaths | None,
    artifact_root: Path,
    image_paths: tuple[str, ...],
) -> None:
    if storage is not None:
        # Scratch workspace and tool results only; never a user's own folder.
        remove_descriptor_confined_directory_child(
            parent_path=storage.action_root.parent,
            child_name=storage.action_root.name,
        )
    for storage_path in image_paths:
        directory, name = storage_path.rsplit("/", maxsplit=1)
        try:
            descriptor = open_directory_descriptor(
                root_path=artifact_root,
                relative_path=f"{GENERIC_IMAGE_DIRECTORY}/{directory}",
            )
        except DescriptorPathMissingError:
            continue
        try:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "ConversationBusyError",
    "HistoryItemKind",
    "delete_history_item",
]
