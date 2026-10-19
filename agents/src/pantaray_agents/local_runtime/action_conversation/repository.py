"""Bounded scope transitions for durable Action conversation history."""

import sqlite3
from dataclasses import dataclass
from typing import Literal, NamedTuple, cast

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.action_resumability import (
    action_accepts_followup_turn,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.action_conversation import ActionConversationSummary

from .cursor import (
    ActionConversationCursor,
    ActionConversationRunBoundary,
    ActionConversationTimelineCursor,
    ActionConversationUnadoptedCursor,
    ActionConversationUnadoptedFrontier,
    decode_action_conversation_cursor,
    encode_action_conversation_cursor,
)
from .entry_projection import project_action_user_entry
from .history_queries import (
    ActionHistoryUserRow,
    TimelineAnchor,
    TimelineHistoryRow,
    UnadoptedAnchor,
    history_anchor_exists,
    read_action_timeline_frontier,
    read_timeline_history,
    read_unadopted_history,
)

type ActionConversationHistoryScope = Literal[
    "current_timeline", "unadopted", "older_timeline"
]


class ActionConversationNotFoundError(MigrationError):
    """The exact user-owned Action does not exist."""


class ActionConversationCursorConflictError(MigrationError):
    """The cursor owner, scope, or exact durable anchor is stale."""


class ActionConversationIntegrityError(MigrationError):
    """Durable Action conversation state violates the read contract."""


@dataclass(frozen=True, slots=True)
class ActionConversationHistoryPage:
    action: ActionConversationSummary
    scope: ActionConversationHistoryScope
    rows: tuple[TimelineHistoryRow, ...]
    next_cursor: str | None


class _LatestRunBoundary(NamedTuple):
    run_id: str
    anchor: TimelineAnchor


def read_action_conversation_history_page_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    cursor: str | None,
    limit: int,
) -> ActionConversationHistoryPage:
    """Read one history scope from a caller-owned SQLite snapshot.

    ``limit`` is a number of logical runs, not rows: a page never splits a run.
    """

    if not connection.in_transaction:
        raise ValueError("Action conversation read requires a caller-owned transaction")
    if type(limit) is not int or limit <= 0:
        raise ValueError("Action conversation history limit must be positive")

    action, boundary = _load_action_and_boundary(connection, user_id, action_id)
    decoded = decode_action_conversation_cursor(cursor) if cursor is not None else None
    if decoded is not None:
        _validate_cursor_owner(decoded, user_id, action_id)

    if decoded is None:
        current_page = _read_current_timeline(
            connection, user_id, action, boundary, None, limit
        )
        if current_page is not None:
            return current_page
        return _read_unadopted_or_older(
            connection, user_id, action, boundary, None, limit
        )

    if isinstance(decoded, ActionConversationTimelineCursor):
        anchor = TimelineAnchor(decoded.step_number, decoded.step_id)
        _require_exact_anchor(
            connection, user_id, action_id, "timeline", anchor[0], anchor[1]
        )
        if decoded.scope == "current_timeline":
            if boundary is None or anchor < boundary.anchor:
                raise ActionConversationCursorConflictError(
                    "Action conversation cursor no longer belongs to the current run"
                )
            timeline_ceiling = cast(
                ActionConversationRunBoundary, decoded.timeline_ceiling
            )
            _require_same_timeline_ceiling(
                connection,
                user_id,
                action_id,
                boundary,
                timeline_ceiling,
            )
            current_page = _read_current_timeline(
                connection,
                user_id,
                action,
                boundary,
                anchor,
                limit,
                timeline_ceiling,
            )
            if current_page is not None:
                return current_page
            return _read_unadopted_or_older(
                connection,
                user_id,
                action,
                boundary,
                None,
                limit,
                timeline_ceiling,
            )
        if boundary is not None and anchor >= boundary.anchor:
            raise ActionConversationCursorConflictError(
                "Action conversation cursor does not belong to older history"
            )
        return _read_older_timeline(connection, user_id, action, anchor, limit)

    unadopted_anchor = UnadoptedAnchor(decoded.accepted_sequence, decoded.step_id)
    _require_exact_anchor(
        connection,
        user_id,
        action_id,
        "unadopted",
        unadopted_anchor[0],
        unadopted_anchor[1],
    )
    _require_same_run_boundary(decoded.timeline_boundary, boundary)
    _require_same_unadopted_ceiling(
        connection, user_id, action_id, decoded.unadopted_ceiling
    )
    _require_same_timeline_ceiling(
        connection,
        user_id,
        action_id,
        boundary,
        decoded.timeline_ceiling,
    )
    return _read_unadopted_or_older(
        connection,
        user_id,
        action,
        boundary,
        unadopted_anchor,
        limit,
        decoded.timeline_ceiling,
        decoded.unadopted_ceiling,
    )


def _load_action_and_boundary(
    connection: sqlite3.Connection, user_id: str, action_id: str
) -> tuple[ActionConversationSummary, _LatestRunBoundary | None]:
    row = connection.execute(
        """
        WITH latest AS (
          SELECT adopted_process_id AS run_id
          FROM agent_action_steps
            INDEXED BY idx_agent_action_steps_adopted_user_timeline
          WHERE user_id=:user_id AND action_id=:action_id
            AND step_number IS NOT NULL AND step_type='user_request'
            AND status='success' AND adopted_process_id IS NOT NULL
          ORDER BY step_number DESC,step_id DESC LIMIT 1
        ), first AS (
          SELECT steps.step_number,steps.step_id
          FROM agent_action_steps AS steps
            INDEXED BY idx_agent_action_steps_adopted_process
          WHERE steps.user_id=:user_id AND steps.action_id=:action_id
            AND steps.adopted_process_id=(SELECT run_id FROM latest)
          ORDER BY steps.step_number ASC,steps.step_id ASC LIMIT 1
        )
        SELECT actions.action_id, actions.suggestion_id, actions.status,
               latest.run_id,first.step_number,first.step_id,
               initial.step_id,initial.step_number,initial.accepted_sequence,
               initial.user_message_id,initial.user_message_json,initial.user_request_text,
               initial.adopted_process_id,initial.expected_process_id,
               initial.adoption_canceled_at,initial.step_name
        FROM agent_actions AS actions
        LEFT JOIN latest ON TRUE
        LEFT JOIN first ON TRUE
        LEFT JOIN agent_action_steps AS initial
          ON initial.user_id=actions.user_id AND initial.action_id=actions.action_id
          AND initial.user_message_id=actions.initial_user_message_id
          AND initial.step_type='user_request' AND initial.status='success'
        WHERE actions.user_id=:user_id AND actions.action_id=:action_id
        """,
        {"user_id": user_id, "action_id": action_id},
    ).fetchone()
    if row is None:
        raise ActionConversationNotFoundError("Action conversation not found")
    approved_suggestion = None
    if row[1] is not None:
        if row[6] is None:
            raise ActionConversationIntegrityError(
                "Action initial USER message is missing"
            )
        approved_suggestion = project_action_user_entry(
            ActionHistoryUserRow(
                step_id=row[6],
                step_number=row[7],
                accepted_sequence=row[8],
                user_message_id=row[9],
                user_message_json=row[10],
                user_request_text=row[11],
                adopted_process_id=row[12],
                expected_process_id=row[13],
                adoption_canceled_at=row[14],
                step_name=row[15],
            )
        ).approved_suggestion
    try:
        action = ActionConversationSummary(
            action_id=row[0],
            suggestion_id=row[1],
            approved_suggestion=approved_suggestion,
            status=row[2],
            latest_run_id=row[3],
            resumable=row[2] == "canceled"
            and action_accepts_followup_turn(
                connection=connection,
                user_id=user_id,
                action_id=action_id,
            ),
        )
    except ValidationError as error:
        raise ActionConversationIntegrityError(
            "Action conversation summary is inconsistent"
        ) from error
    run_id = action.latest_run_id
    if run_id is None:
        return action, None
    anchor = _timeline_anchor(row[4], row[5])
    return action, _LatestRunBoundary(run_id, anchor)


def _read_current_timeline(
    connection: sqlite3.Connection,
    user_id: str,
    action: ActionConversationSummary,
    boundary: _LatestRunBoundary | None,
    before: TimelineAnchor | None,
    limit: int,
    timeline_ceiling: ActionConversationRunBoundary | None = None,
) -> ActionConversationHistoryPage | None:
    if boundary is None:
        return None
    # The current scope is exactly the latest run, and a page is a whole run, so
    # one run is all this scope ever asks for; ``limit`` counts older runs.
    queried = read_timeline_history(connection, user_id, action.action_id, before, 1)
    current_rows: list[TimelineHistoryRow] = []
    reached_older = False
    for row in queried.rows:
        row_is_current = _history_run_id(row) == boundary.run_id
        position_is_current = _row_timeline_anchor(row) >= boundary.anchor
        if row_is_current != position_is_current:
            raise ActionConversationIntegrityError(
                "Action timeline row crosses the latest-run boundary"
            )
        if row_is_current:
            if reached_older:
                raise ActionConversationIntegrityError(
                    "Action logical runs are not contiguous in timeline order"
                )
            current_rows.append(row)
        else:
            reached_older = True
    if not current_rows:
        return None
    if timeline_ceiling is None:
        timeline_ceiling = _read_timeline_ceiling(
            connection,
            user_id,
            action.action_id,
            boundary,
        )
    has_later = (
        queried.has_more
        or reached_older
        or bool(
            read_unadopted_history(connection, user_id, action.action_id, None, 1).rows
        )
    )
    return _page(
        action,
        "current_timeline",
        tuple(current_rows),
        has_later,
        user_id,
        boundary,
        timeline_ceiling,
    )


def _read_unadopted_or_older(
    connection: sqlite3.Connection,
    user_id: str,
    action: ActionConversationSummary,
    boundary: _LatestRunBoundary | None,
    before: UnadoptedAnchor | None,
    limit: int,
    timeline_ceiling: ActionConversationRunBoundary | None = None,
    unadopted_ceiling: ActionConversationUnadoptedFrontier | None = None,
) -> ActionConversationHistoryPage:
    queried = read_unadopted_history(
        connection, user_id, action.action_id, before, limit
    )
    if queried.rows:
        if unadopted_ceiling is None:
            first = queried.rows[0]
            unadopted_ceiling = ActionConversationUnadoptedFrontier(
                accepted_sequence=first.accepted_sequence,
                step_id=first.step_id,
            )
        if timeline_ceiling is None:
            timeline_ceiling = _read_timeline_ceiling(
                connection,
                user_id,
                action.action_id,
                boundary,
            )
        older_before = boundary.anchor if boundary is not None else None
        has_later = queried.has_more or bool(
            read_timeline_history(
                connection, user_id, action.action_id, older_before, 1
            ).rows
        )
        return _page(
            action,
            "unadopted",
            queried.rows,
            has_later,
            user_id,
            boundary,
            timeline_ceiling,
            unadopted_ceiling,
        )
    older_before = boundary.anchor if boundary is not None else None
    return _read_older_timeline(connection, user_id, action, older_before, limit)


def _read_older_timeline(
    connection: sqlite3.Connection,
    user_id: str,
    action: ActionConversationSummary,
    before: TimelineAnchor | None,
    limit: int,
) -> ActionConversationHistoryPage:
    queried = read_timeline_history(
        connection, user_id, action.action_id, before, limit
    )
    return _page(
        action,
        "older_timeline",
        queried.rows,
        queried.has_more,
        user_id,
        None,
    )


def _page(
    action: ActionConversationSummary,
    scope: ActionConversationHistoryScope,
    rows: tuple[TimelineHistoryRow, ...],
    has_later: bool,
    user_id: str,
    boundary: _LatestRunBoundary | None,
    timeline_ceiling: ActionConversationRunBoundary | None = None,
    unadopted_ceiling: ActionConversationUnadoptedFrontier | None = None,
) -> ActionConversationHistoryPage:
    next_cursor = None
    if has_later:
        last = rows[-1]
        try:
            payload: ActionConversationCursor
            if scope == "unadopted":
                if not isinstance(last, ActionHistoryUserRow):
                    raise ActionConversationIntegrityError(
                        "Unadopted scope contains a non-USER row"
                    )
                if unadopted_ceiling is None:
                    raise ActionConversationIntegrityError(
                        "Unadopted scope has no durable ceiling"
                    )
                payload = ActionConversationUnadoptedCursor(
                    user_id=user_id,
                    action_id=action.action_id,
                    scope="unadopted",
                    accepted_sequence=last.accepted_sequence,
                    step_id=last.step_id,
                    timeline_boundary=(
                        ActionConversationRunBoundary(
                            run_id=boundary.run_id,
                            step_number=boundary.anchor.step_number,
                            step_id=boundary.anchor.step_id,
                        )
                        if boundary is not None
                        else None
                    ),
                    timeline_ceiling=timeline_ceiling,
                    unadopted_ceiling=unadopted_ceiling,
                )
            else:
                anchor = _row_timeline_anchor(last)
                payload = ActionConversationTimelineCursor(
                    user_id=user_id,
                    action_id=action.action_id,
                    scope=scope,
                    step_number=anchor.step_number,
                    step_id=anchor.step_id,
                    timeline_ceiling=timeline_ceiling,
                )
            next_cursor = encode_action_conversation_cursor(payload)
        except ValidationError as error:
            raise ActionConversationIntegrityError(
                "Action conversation row cannot form an exact cursor"
            ) from error
    return ActionConversationHistoryPage(action, scope, rows, next_cursor)


def _validate_cursor_owner(
    cursor: ActionConversationCursor, user_id: str, action_id: str
) -> None:
    if cursor.user_id != user_id or cursor.action_id != action_id:
        raise ActionConversationCursorConflictError(
            "Action conversation cursor owner does not match the request"
        )


def _require_exact_anchor(
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    scope: Literal["timeline", "unadopted"],
    position: int,
    step_id: str,
) -> None:
    if not history_anchor_exists(
        connection, user_id, action_id, scope, position, step_id
    ):
        raise ActionConversationCursorConflictError(
            "Action conversation cursor anchor is stale"
        )


def _require_same_run_boundary(
    cursor_boundary: ActionConversationRunBoundary | None,
    current_boundary: _LatestRunBoundary | None,
) -> None:
    if cursor_boundary is None:
        matches = current_boundary is None
    else:
        matches = current_boundary == _LatestRunBoundary(
            cursor_boundary.run_id,
            TimelineAnchor(cursor_boundary.step_number, cursor_boundary.step_id),
        )
    if not matches:
        raise ActionConversationCursorConflictError(
            "Action conversation run boundary changed after scope transition"
        )


def _require_same_timeline_ceiling(
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    boundary: _LatestRunBoundary | None,
    cursor_ceiling: ActionConversationRunBoundary | None,
) -> None:
    if (
        _read_timeline_ceiling(connection, user_id, action_id, boundary)
        != cursor_ceiling
    ):
        raise ActionConversationCursorConflictError(
            "Action conversation timeline changed after pagination started"
        )


def _require_same_unadopted_ceiling(
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    cursor_ceiling: ActionConversationUnadoptedFrontier,
) -> None:
    latest = read_unadopted_history(connection, user_id, action_id, None, 1).rows
    if not latest or (
        latest[0].accepted_sequence,
        latest[0].step_id,
    ) != (cursor_ceiling.accepted_sequence, cursor_ceiling.step_id):
        raise ActionConversationCursorConflictError(
            "Action conversation unadopted scope changed after pagination started"
        )


def _read_timeline_ceiling(
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    boundary: _LatestRunBoundary | None,
) -> ActionConversationRunBoundary | None:
    frontier = read_action_timeline_frontier(connection, user_id, action_id)
    if frontier is None:
        if boundary is not None:
            raise ActionConversationIntegrityError(
                "Action latest run has no durable timeline frontier"
            )
        return None
    if boundary is None or frontier < boundary.anchor:
        raise ActionConversationIntegrityError(
            "Action timeline frontier has no latest-run boundary"
        )
    return ActionConversationRunBoundary(
        run_id=boundary.run_id,
        step_number=frontier.step_number,
        step_id=frontier.step_id,
    )


def _history_run_id(row: TimelineHistoryRow) -> str:
    run_id = (
        row.adopted_process_id
        if isinstance(row, ActionHistoryUserRow)
        else row.owning_run_id
    )
    if not isinstance(run_id, str) or not run_id.strip():
        raise ActionConversationIntegrityError("Timeline row has no owning run")
    return run_id


def _row_timeline_anchor(row: TimelineHistoryRow) -> TimelineAnchor:
    return _timeline_anchor(row.step_number, row.step_id)


def _timeline_anchor(step_number: object, step_id: object) -> TimelineAnchor:
    if type(step_number) is not int or step_number <= 0:
        raise ActionConversationIntegrityError("Timeline position is invalid")
    if not isinstance(step_id, str) or not step_id.strip():
        raise ActionConversationIntegrityError("Timeline identity is invalid")
    return TimelineAnchor(step_number, step_id)
