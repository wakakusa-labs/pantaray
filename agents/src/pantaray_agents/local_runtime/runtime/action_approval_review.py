"""Put a pending approval to the approval review before the user is asked.

An allow approves the request once and the call runs with no panel; otherwise
it waits for the user, with the review's reason when it gave one. The review
reads only the user's own words, and labels the task text the user did not
write. A screen capture is never reviewed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pantaray_agents.agents.core.mixins.llm_usage import TokenSink
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.approval_review import (
    ApprovalReviewRequest,
    CommandReviewKind,
    PendingCommand,
    PendingIntegrationWrite,
    RequestTextNotByUser,
    review_approval,
)
from pantaray_agents.local_runtime.tooling.brokering.command_approval_summaries import (
    outside_workspace_folder_paths,
)
from pantaray_agents.local_runtime.tooling.repository.approval_session_mutations import (
    record_review_approval_in_connection,
    record_review_question_in_connection,
)
from pantaray_agents.local_runtime.tooling.workspace_manifest_roots import (
    load_ready_manifest_roots_in_connection,
)
from pantaray_agents.schema.agent.action_message_codec import (
    parse_stored_action_user_message,
)
from pantaray_agents.schema.agent.base import JSONValue

from .action_message_models import ACTION_HOST_STEP_NAMES
from .utc_timestamps import now_utc_iso

if TYPE_CHECKING:
    from pantaray_agents.dependencies import LLMClientProtocol

_COMMAND_SUMMARY_KINDS = frozenset({"bash", "run_python", "apply_patch"})
_INTEGRATION_SUMMARY_KIND = "integration_tool"

# The rows an Action's request is told in: its USER rows, and the assistant
# message a Suggestion reply or an AI-written TODO opens it with.
_REQUEST_ROWS_SQL = """
SELECT step_type, step_name, user_message_id, user_message_json,
       user_request_text, llm_response_text, source_suggestion_id
FROM agent_action_steps
WHERE user_id = ? AND action_id = ? AND step_number IS NOT NULL
  AND (step_type = 'user_request'
       OR (step_type = 'assistant_message' AND step_number = 1))
ORDER BY step_number, step_id
"""


@dataclass(frozen=True, slots=True)
class ActionRequestRow:
    """One row of :data:`_REQUEST_ROWS_SQL`."""

    step_type: str
    step_name: str
    user_message_id: str | None
    user_message_json: str | None
    user_request_text: str | None
    llm_response_text: str | None
    source_suggestion_id: str | None


def action_request_words(
    rows: Iterable[ActionRequestRow],
) -> tuple[tuple[str, ...], RequestTextNotByUser | None]:
    """The user's own messages, and the task text the user did not write."""

    user_messages: list[str] = []
    not_by_user: RequestTextNotByUser | None = None
    for row in rows:
        if row.step_type == "assistant_message":
            if row.llm_response_text:
                not_by_user = RequestTextNotByUser(
                    source="todo" if row.source_suggestion_id is None else "suggestion",
                    text=row.llm_response_text,
                )
            continue
        if row.step_name in ACTION_HOST_STEP_NAMES or row.user_request_text is None:
            continue
        message = parse_stored_action_user_message(
            message_id=row.user_message_id,
            message_json=row.user_message_json,
            user_request_text=row.user_request_text,
        )
        # A row with no envelope cannot say who wrote it, and a chat
        # instruction is the chat's words: neither speaks for the user.
        if message is None or message.is_chat_instruction:
            continue
        if message.suggestion_approval is not None:
            not_by_user = RequestTextNotByUser(
                source="suggestion", text=message.content
            )
            if message.supplement is not None:
                user_messages.append(message.supplement)
            continue
        user_messages.append(message.content)
    return tuple(user_messages), not_by_user


def pending_operation(
    *, tool_id: str, summary: dict[str, JSONValue], args: dict[str, JSONValue]
) -> PendingCommand | PendingIntegrationWrite | None:
    """The operation an approval summary asks for, or None when it is not reviewed."""

    kind = summary.get("summary_kind")
    if kind == _INTEGRATION_SUMMARY_KIND:
        return PendingIntegrationWrite(
            toolkit=str(summary["toolkit"]),
            action=str(summary["slug"]),
            arguments=json.loads(str(summary["arguments"])),
            justification=None,
        )
    if kind not in _COMMAND_SUMMARY_KINDS:
        # A screen capture always asks the user.
        return None
    kinds: set[CommandReviewKind] = set()
    if summary.get("run_outside_sandbox") is True:
        kinds.add("outside_sandbox")
    if summary.get("use_login_environment") is True:
        kinds.add("login_environment")
    folders = outside_workspace_folder_paths(summary)
    if folders:
        kinds.add("outside_write")
    command = summary.get("command")
    cwd = summary.get("cwd")
    reason = summary.get("reason")
    return PendingCommand(
        tool=tool_id,
        kinds=frozenset(kinds),
        # A bash summary names its command; the other tools' summaries hold
        # only a digest or the paths, so their arguments are the command.
        command=(
            command
            if isinstance(command, str)
            else json.dumps(args, ensure_ascii=False, indent=2)
        ),
        cwd=cwd if isinstance(cwd, str) and cwd else None,
        additional_write_folders=folders,
        justification=reason if isinstance(reason, str) else None,
    )


async def review_pending_approval(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    client: LLMClientProtocol,
    sink: TokenSink,
    user_id: str,
    action_id: str,
    approval_session_id: str,
    tool_request_id: str,
    args: dict[str, JSONValue],
) -> bool:
    """Whether the review approved the pending request, so it runs unasked.

    A request the user or a stop decided while the review ran keeps that
    decision. A request reviewed before keeps waiting for the user.
    """

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        request = _load_review_request(
            connection,
            user_id=user_id,
            action_id=action_id,
            approval_session_id=approval_session_id,
            args=args,
        )
    if request is None:
        return False
    verdict = await review_approval(client, request, sink=sink)
    if verdict is None:
        return False
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            if verdict.decision == "allow":
                return record_review_approval_in_connection(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    tool_request_id=tool_request_id,
                    approval_session_id=approval_session_id,
                    reason=verdict.reason,
                    decided_at=now_utc_iso(),
                )
            record_review_question_in_connection(
                connection=connection,
                user_id=user_id,
                action_id=action_id,
                tool_request_id=tool_request_id,
                approval_session_id=approval_session_id,
                reason=verdict.reason,
            )
    return False


def _load_review_request(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    approval_session_id: str,
    args: dict[str, JSONValue],
) -> ApprovalReviewRequest | None:
    session = connection.execute(
        """
        SELECT tool_id, manifest_id, command_summary_json FROM approval_sessions
        WHERE user_id = ? AND action_id = ? AND approval_session_id = ?
          AND approval_source = 'prompt' AND status = 'pending'
          AND review_reason IS NULL
        """,
        (user_id, action_id, approval_session_id),
    ).fetchone()
    if session is None:
        return None
    operation = pending_operation(
        tool_id=str(session["tool_id"]),
        summary=json.loads(str(session["command_summary_json"])),
        args=args,
    )
    if operation is None:
        return None
    user_messages, not_by_user = action_request_words(
        ActionRequestRow(*row)
        for row in connection.execute(_REQUEST_ROWS_SQL, (user_id, action_id))
    )
    roots = load_ready_manifest_roots_in_connection(
        connection=connection,
        user_id=user_id,
        manifest_id=str(session["manifest_id"]),
    )
    return ApprovalReviewRequest(
        user_messages=user_messages,
        request_text_not_by_user=not_by_user,
        operation=operation,
        workspace_folders=tuple(
            str(root.canonical_real_path) for root in roots if root.can_apply_patch
        ),
    )


__all__ = [
    "ActionRequestRow",
    "action_request_words",
    "pending_operation",
    "review_pending_approval",
]
