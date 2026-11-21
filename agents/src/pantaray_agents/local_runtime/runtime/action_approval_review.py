"""Put a pending approval to the approval review before the user is asked.

An allow approves the request once and the call runs with no panel; otherwise
it waits for the user, with the review's reason when it gave one. The review
reads only the user's own words, and labels the task text the user did not
write. For a task the chat started on its own instruction, the user's own words
are their chat messages that led to it. A screen capture is never reviewed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
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
    ConversationEntry,
    DeclinedOperation,
    PendingBrowserWrite,
    PendingCommand,
    PendingIntegrationWrite,
    PendingOperation,
    PendingUserBrowserRead,
    RequestTextNotByUser,
    UserMessage,
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

from .action_message_models import ACTION_HOOK_STEP_NAME, ACTION_HOST_STEP_NAMES
from .action_stop_signal import action_stop_requested_in_connection
from .utc_timestamps import now_utc_iso

if TYPE_CHECKING:
    from pantaray_agents.dependencies import LLMClientProtocol

_COMMAND_SUMMARY_KINDS = frozenset({"bash", "run_python", "apply_patch"})
_INTEGRATION_SUMMARY_KIND = "integration_tool"
_BROWSER_WRITE_SUMMARY_KIND = "browser_write"
_USER_BROWSER_READ_SUMMARY_KIND = "user_browser_read"

# The rows an Action's request is told in: its USER rows, and the assistant
# message a Suggestion reply or an AI-written TODO opens it with.
_REQUEST_ROWS_SQL = """
SELECT step.step_type, step.step_name, step.user_message_id,
       step.user_message_json, step.user_request_text, step.llm_response_text,
       step.source_suggestion_id, step.created_at,
       COALESCE(suggestion.user_reaction = 'rejected', 0) AS suggestion_declined
FROM agent_action_steps AS step
LEFT JOIN agent_suggestions AS suggestion
  ON suggestion.user_id = step.user_id
 AND suggestion.suggestion_id = step.source_suggestion_id
WHERE step.user_id = ? AND step.action_id = ? AND step.step_number IS NOT NULL
  AND (step.step_type = 'user_request'
       OR (step.step_type = 'assistant_message' AND step.step_number = 1))
ORDER BY step.step_number, step.step_id
"""

# The user's chat messages a hand-off relayed into the task, as first written.
_RELAYED_CHAT_SQL = """
SELECT item_id, created_at, json_extract(payload, '$.text') AS text FROM chat_items
WHERE user_id = ? AND kind = 'user_message'
  AND item_id IN (SELECT value FROM json_each(?))
  AND length(json_extract(payload, '$.text')) > 0
"""

# The user's chat messages up to a hand-off, since the chat last handed work to
# a task in an earlier turn. Every hand-off's USER row has a message id under
# its turn's key. Design limit: the newest 20 messages; widen it when
# chat-started tasks still ask about requests the user made further back.
_CHAT_WORDS_SQL = """
SELECT item_id, created_at, json_extract(payload, '$.text') AS text FROM chat_items
WHERE user_id = :user_id AND kind = 'user_message' AND created_at <= :handed_at
  AND length(json_extract(payload, '$.text')) > 0
  AND created_at > COALESCE((
        SELECT MAX(created_at) FROM agent_action_steps
        WHERE user_id = :user_id AND step_type = 'user_request'
          AND user_message_id GLOB 'chat-turn/*'
          AND user_message_id NOT GLOB :turn || '*'
          AND created_at < :handed_at), '')
ORDER BY sequence DESC LIMIT 20
"""


# The user's own declines in an Action. Design limit: the newest 10; raise it
# when a review allows something the user declined further back.
_DECLINED_SQL = """
SELECT tool_id, command_summary_json, decided_at FROM approval_sessions
WHERE user_id = ? AND action_id = ? AND approval_source = 'prompt'
  AND status = 'denied'
ORDER BY decided_at DESC LIMIT 10
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
    created_at: str
    # Whether the user had declined the suggestion this Action answers.
    suggestion_declined: bool


# An entry of the review's conversation and when it happened (UTC ISO 8601).
type Timed[T] = tuple[str, T]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One of the user's chat messages, with its identity and the time it was sent."""

    item_id: str
    at: str
    text: str


def action_request_words(
    rows: Iterable[ActionRequestRow],
    chat_words: Callable[[ActionRequestRow], tuple[ChatMessage, ...]],
    relayed_words: Callable[[tuple[str, ...]], tuple[ChatMessage, ...]],
) -> tuple[tuple[Timed[UserMessage], ...], RequestTextNotByUser | None]:
    """The user's own messages, and the task text the user did not write.

    ``chat_words`` reads the user's chat messages behind a chat instruction, and
    ``relayed_words`` the chat messages a hand-off relayed. A chat message keeps
    the time the user sent it and counts once, however often it is relayed.
    """

    user_messages: list[Timed[UserMessage]] = []
    seen_chat_items: set[str] = set()

    def add_chat(messages: tuple[ChatMessage, ...]) -> None:
        for chat in messages:
            if chat.item_id not in seen_chat_items:
                seen_chat_items.add(chat.item_id)
                user_messages.append((chat.at, UserMessage(chat.text)))

    not_by_user: RequestTextNotByUser | None = None
    for row in rows:
        if row.step_type == "assistant_message":
            if row.llm_response_text:
                # A suggestion opens an Action this way only when the user
                # replied to it; an AI-written TODO is the other opening.
                not_by_user = RequestTextNotByUser(
                    source=(
                        "todo"
                        if row.source_suggestion_id is None
                        else "suggestion_reply"
                    ),
                    text=row.llm_response_text,
                    declined=row.suggestion_declined,
                )
            continue
        if row.step_name == ACTION_HOOK_STEP_NAME and row.user_request_text:
            # Pantaray sent a task's prompt; the user approved the task, not
            # this turn.
            not_by_user = RequestTextNotByUser(
                source="hook", text=row.user_request_text, declined=False
            )
            continue
        if row.step_name in ACTION_HOST_STEP_NAMES or row.user_request_text is None:
            continue
        message = parse_stored_action_user_message(
            message_id=row.user_message_id,
            message_json=row.user_message_json,
            user_request_text=row.user_request_text,
        )
        # A row with no envelope cannot say who wrote it.
        if message is None:
            continue
        # A chat instruction is the chat's words; the user's are in the chat.
        if message.is_chat_instruction:
            add_chat(chat_words(row))
            continue
        # Relayed words (the content, or a suggestion's supplement) are the
        # user's chat messages, which may be older than the hand-off.
        relayed = (
            message.chat_handoff.relayed_item_ids
            if message.chat_handoff is not None
            else ()
        )
        if message.suggestion_approval is not None:
            not_by_user = RequestTextNotByUser(
                source="suggestion_accepted", text=message.content, declined=False
            )
            if relayed:
                add_chat(relayed_words(relayed))
            elif message.supplement is not None:
                user_messages.append((row.created_at, UserMessage(message.supplement)))
            continue
        if relayed:
            add_chat(relayed_words(relayed))
            continue
        user_messages.append((row.created_at, UserMessage(message.content)))
    return tuple(user_messages), not_by_user


def ordered_conversation(
    *entries: Iterable[Timed[ConversationEntry]],
) -> tuple[ConversationEntry, ...]:
    """The entries oldest first, so a decline is read against what came after."""

    # Parsed, not compared as text: stored times differ in their offset spelling.
    timed = sorted(
        (entry for group in entries for entry in group),
        key=lambda entry: datetime.fromisoformat(entry[0]),
    )
    return tuple(entry for _at, entry in timed)


def pending_operation(
    *, tool_id: str, summary: dict[str, JSONValue], args: dict[str, JSONValue]
) -> PendingOperation | None:
    """The operation an approval summary asks for, or None when it is not reviewed."""

    kind = summary.get("summary_kind")
    if kind == _INTEGRATION_SUMMARY_KIND:
        return PendingIntegrationWrite(
            toolkit=str(summary["toolkit"]),
            action=str(summary["slug"]),
            account_id=str(summary["account_id"]),
            account_label=str(summary["account"]),
            arguments=json.loads(str(summary["arguments"])),
            justification=None,
        )
    if kind == _BROWSER_WRITE_SUMMARY_KIND:
        page_host = summary["page_host"]
        # Only a write from the user's own browser names it.
        users_browser = summary.get("browser")
        return PendingBrowserWrite(
            host=str(summary["host"]),
            page_host=None if page_host is None else str(page_host),
            justification=str(summary["reason"]),
            users_browser=None if users_browser is None else str(users_browser),
        )
    if kind == _USER_BROWSER_READ_SUMMARY_KIND:
        return PendingUserBrowserRead(
            browser=str(summary["browser"]),
            host=str(summary["host"]),
            justification=str(summary["reason"]),
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
        run_in_background=summary.get("run_in_background") is True,
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
    job_id: str | None,
    approval_session_id: str,
    tool_request_id: str,
    args: dict[str, JSONValue],
) -> bool:
    """Whether the review approved the pending request, so it runs unasked.

    A request the user or a stop decided while the review ran keeps that
    decision, and a run stopped meanwhile (``job_id`` names its job) gets no
    approval. A request reviewed before keeps waiting for the user.
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
                # Read in the same transaction as the write: a Stop committed
                # before it never lets the call start.
                if action_stop_requested_in_connection(
                    connection, user_id=user_id, action_id=action_id, job_id=job_id
                ):
                    return False
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


def action_request_rows(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> tuple[ActionRequestRow, ...]:
    """The rows the Action's request is told in, in order."""

    return tuple(
        ActionRequestRow(*row[:-1], suggestion_declined=bool(row[-1]))
        for row in connection.execute(_REQUEST_ROWS_SQL, (user_id, action_id))
    )


def read_chat_words(
    connection: sqlite3.Connection, *, user_id: str, row: ActionRequestRow
) -> tuple[ChatMessage, ...]:
    """The user's chat messages that led to the hand-off ``row``, oldest first."""

    # "chat-turn/<turn key>/<tool>/<n>": hand-offs of the same turn share a window.
    turn = "/".join(str(row.user_message_id).split("/")[:2]) + "/"
    found = connection.execute(
        _CHAT_WORDS_SQL,
        {"user_id": user_id, "handed_at": row.created_at, "turn": turn},
    ).fetchall()
    return tuple(ChatMessage(*map(str, chat)) for chat in reversed(found))


def read_relayed_words(
    connection: sqlite3.Connection, *, user_id: str, item_ids: tuple[str, ...]
) -> tuple[ChatMessage, ...]:
    """The user's chat messages ``item_ids`` as they first sent them."""

    found = connection.execute(
        _RELAYED_CHAT_SQL, (user_id, json.dumps(list(item_ids)))
    ).fetchall()
    return tuple(ChatMessage(*map(str, chat)) for chat in found)


def declined_operations(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> tuple[Timed[DeclinedOperation], ...]:
    """What the user declined in this Action and when, newest first.

    A subagent's requests carry its parent's Action id, so they count here too.
    """

    declined: list[Timed[DeclinedOperation]] = []
    for tool_id, summary_json, decided_at in connection.execute(
        _DECLINED_SQL, (user_id, action_id)
    ):
        summary = json.loads(str(summary_json))
        # A declined call keeps no arguments; its summary names what it did.
        operation = pending_operation(
            tool_id=str(tool_id), summary=summary, args=summary
        )
        if operation is not None:
            declined.append((str(decided_at), DeclinedOperation(operation)))
    return tuple(declined)


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
        action_request_rows(connection, user_id=user_id, action_id=action_id),
        lambda row: read_chat_words(connection, user_id=user_id, row=row),
        lambda item_ids: read_relayed_words(
            connection, user_id=user_id, item_ids=item_ids
        ),
    )
    roots = load_ready_manifest_roots_in_connection(
        connection=connection,
        user_id=user_id,
        manifest_id=str(session["manifest_id"]),
    )
    return ApprovalReviewRequest(
        conversation=ordered_conversation(
            user_messages,
            declined_operations(connection, user_id=user_id, action_id=action_id),
        ),
        request_text_not_by_user=not_by_user,
        operation=operation,
        workspace_folders=tuple(
            str(root.canonical_real_path) for root in roots if root.can_apply_patch
        ),
    )


__all__ = [
    "ActionRequestRow",
    "ChatMessage",
    "action_request_rows",
    "action_request_words",
    "declined_operations",
    "ordered_conversation",
    "pending_operation",
    "read_chat_words",
    "read_relayed_words",
    "review_pending_approval",
]
