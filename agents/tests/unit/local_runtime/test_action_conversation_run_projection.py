import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pantaray_agents.action_status import build_finalize_action_terminal_command
from pantaray_agents.local_runtime.action_conversation import run_projection
from pantaray_agents.local_runtime.action_conversation.history_queries import (
    ActionHistoryToolRow,
    ActionHistoryUserRow,
    TimelineHistoryRow,
)
from pantaray_agents.local_runtime.action_conversation.repository import (
    ActionConversationHistoryPage,
    ActionConversationHistoryScope,
    ActionConversationIntegrityError,
)
from pantaray_agents.local_runtime.runtime.action_logical_run_authority import (
    ActionLogicalRunAuthority,
    ActionLogicalRunSelection,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_USER_STEP_NAME,
)
from pantaray_agents.schema.action_conversation import (
    ActionConversationPage,
    ActionConversationSummary,
    ActionStatus,
)
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
    serialize_action_user_message,
)

_OLD_START = "2026-08-30T00:00:00Z"
_OLD_END = "2026-08-30T00:01:00Z"


def _user(
    step: str, run: str, sequence: int, step_name: str = ACTION_USER_STEP_NAME
) -> ActionHistoryUserRow:
    return ActionHistoryUserRow(
        step,
        sequence,
        sequence,
        None,
        None,
        f"request {step}",
        run,
        None,
        None,
        step_name,
    )


def _authority(
    run: str,
    process_status: str = "running",
    job_status: str = "running",
    *,
    started_at: str = "2026-08-30T00:02:00Z",
) -> ActionLogicalRunAuthority:
    return ActionLogicalRunAuthority(
        "action-1",
        run,
        1,
        started_at,
        process_status,
        f"job-{run}",
        job_status,
        None,
        None,
        None,
    )


def _terminal_authority(run: str) -> ActionLogicalRunAuthority:
    command = build_finalize_action_terminal_command(
        process_completed_event_id=f"event-{run}",
        suggestion_id="suggestion-1",
        user_id="user-1",
        command_id=f"command-{run}",
        process_id=run,
        action_id="action-1",
        accepted_at=_OLD_START,
        completed_at=_OLD_END,
        action_status="success",
        final_output="done",
    )
    payload = dict(command.process_completed_payload["data"])
    payload["persisted_sequence"] = 9
    return replace(
        _authority(run, "completed", "completed", started_at=_OLD_START),
        completed_at=_OLD_END,
        terminal_event_id=f"event-{run}",
        terminal_event_payload_json=json.dumps(payload),
    )


def _read(
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[TimelineHistoryRow, ...],
    authorities: tuple[ActionLogicalRunAuthority, ...],
    *,
    status: ActionStatus = "processing",
    scope: ActionConversationHistoryScope = "current_timeline",
    next_run_takes_after: int | None = None,
) -> tuple[ActionConversationPage, Mock]:
    history = ActionConversationHistoryPage(
        ActionConversationSummary(
            action_id="action-1",
            suggestion_id="suggestion-1",
            approved_suggestion=None,
            status=status,
            latest_run_id="run-new",
            resumable=False,
        ),
        scope,
        rows,
        "next-page",
    )
    read_history = Mock(return_value=history)
    select_authority = Mock(return_value=authorities)
    monkeypatch.setattr(
        run_projection,
        "read_action_conversation_history_page_in_connection",
        read_history,
    )
    monkeypatch.setattr(
        run_projection,
        "select_action_logical_run_authorities_in_connection",
        select_authority,
    )
    monkeypatch.setattr(
        run_projection,
        "next_run_takes_messages_after_in_connection",
        Mock(return_value=next_run_takes_after),
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        page = run_projection.read_action_conversation_page_in_connection(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            cursor="cursor-1",
            limit=20,
        )
    read_history.assert_called_once()
    return page, select_authority


def test_composes_mixed_runs_in_page_order(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: tuple[TimelineHistoryRow, ...] = (
        _user("step-5", "run-new", 5),
        ActionHistoryToolRow("step-4", 4, "bash", "processing", None, None, "run-new"),
        _user("step-3", "run-new", 3),
        ActionHistoryToolRow("step-2", 2, "bash", "success", None, None, "run-old"),
        _user("step-1", "run-old", 1),
    )
    page, selector = _read(
        monkeypatch,
        rows,
        (_terminal_authority("run-old"), _authority("run-new")),
    )

    assert [run.run_id for run in page.runs] == ["run-new", "run-old"]
    assert [run.completion_event_id for run in page.runs] == [None, "event-run-old"]
    assert [[entry.step_id for entry in run.entries] for run in page.runs] == [
        ["step-5", "step-4", "step-3"],
        ["step-2", "step-1"],
    ]
    assert (page.runs[0].status, page.runs[1].final_output) == ("running", "done")
    selector.assert_called_once()
    assert selector.call_args.kwargs["selections"] == frozenset(
        ActionLogicalRunSelection("action-1", run) for run in ("run-new", "run-old")
    )


@pytest.mark.parametrize(
    ("process_status", "job_status", "expected"),
    [
        ("enqueued", "queued", "running"),
        ("running", "running", "running"),
        ("paused", "paused", "approval_pending"),
    ],
)
def test_maps_active_authority_status(
    monkeypatch: pytest.MonkeyPatch,
    process_status: str,
    job_status: str,
    expected: str,
) -> None:
    page, _ = _read(
        monkeypatch,
        (_user("step-1", "run-new", 1),),
        (_authority("run-new", process_status, job_status),),
    )

    assert page.runs[0].status == expected


def test_rejects_noncontiguous_run_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ActionConversationIntegrityError):
        _read(
            monkeypatch,
            (
                _user("step-3", "run-new", 3),
                _user("step-2", "run-old", 2),
                _user("step-1", "run-new", 1),
            ),
            (_authority("run-new"), _authority("run-old")),
        )


def test_unadopted_uses_empty_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    message = ActionUserMessageInput(message_id="message-1", content="Follow up")
    row = ActionHistoryUserRow(
        "step-1",
        None,
        1,
        message.message_id,
        serialize_action_user_message(message),
        render_action_user_request_text(message),
        None,
        None,
        None,
        ACTION_USER_STEP_NAME,
    )
    page, selector = _read(
        monkeypatch,
        (row,),
        (),
        status="queued",
        scope="unadopted",
    )

    assert (page.runs, page.unadopted_messages[0].status) == ((), "pending")
    selector.assert_called_once()
    assert selector.call_args.kwargs["selections"] == frozenset()


def _not_executed(step_id: str, accepted_sequence: int) -> ActionHistoryUserRow:
    message = ActionUserMessageInput(message_id=f"message-{step_id}", content="Later")
    return ActionHistoryUserRow(
        step_id,
        None,
        accepted_sequence,
        message.message_id,
        serialize_action_user_message(message),
        render_action_user_request_text(message),
        None,
        "run-old",
        "2026-10-11T00:00:00Z",
        ACTION_USER_STEP_NAME,
    )


@pytest.mark.parametrize(
    ("takes_after", "expected"),
    [
        # Only a message sent after the latest run's root joins the next run.
        (2, ("not_executed", "runs_with_next_message")),
        # No restorable checkpoint: the next run takes none of them.
        (None, ("not_executed", "not_executed")),
    ],
)
def test_a_not_run_message_says_whether_the_next_send_takes_it(
    monkeypatch: pytest.MonkeyPatch,
    takes_after: int | None,
    expected: tuple[str, str],
) -> None:
    page, _ = _read(
        monkeypatch,
        (_not_executed("older", 1), _not_executed("newer", 3)),
        (),
        status="canceled",
        scope="unadopted",
        next_run_takes_after=takes_after,
    )

    assert tuple(entry.status for entry in page.unadopted_messages) == expected
