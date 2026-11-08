from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents import todo_ux_feature
from pantaray_agents.local_runtime.action_conversation.cursor_codec import (
    OpaqueCursorError,
)
from pantaray_agents.local_runtime.action_conversation.history_candidates import (
    _ANSWERS_SQL,
    ActionHistoryCandidate,
    ConversationHistoryCandidatePage,
    SuggestionHistoryCandidate,
    _candidate_query,
    read_conversation_history_answers_in_connection,
    read_conversation_history_candidates_in_connection,
    register_conversation_history_casefold_sqlite,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
    TodoHandoffInput,
)

from .local_action_repository_support import bootstrap_action_repository_db

_RUNNING_AT = "2026-08-30T00:04:00.123Z"
_NOISE_CANDIDATES_PER_LANE = 64


@pytest.fixture
def history_connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(bootstrap_action_repository_db(tmp_path))
    register_conversation_history_casefold_sqlite(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        UPDATE agent_suggestions SET answer='Linked answer must be deduplicated',user_reaction='accepted',
          created_at='2026-08-30T00:06:00.123Z',updated_at='2026-08-30T00:06:00.123Z' WHERE suggestion_id='sug-1';
        INSERT INTO agent_suggestions(suggestion_id,user_id,status,answer,has_suggestion,interaction_contract,user_reaction,created_at,updated_at) VALUES
          ('suggestion-invalid','user-1','success',NULL,1,'action_offer','accepted','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z'),
          ('suggestion-message','user-1','success','Standalone 1000Xdone ÜBER',1,'action_offer',NULL,'2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z');
        UPDATE agent_actions SET initial_user_message_id='message-running',status='processing',
          created_at='2026-08-30T00:02:00.123Z',updated_at='2026-08-30T00:04:00.123Z' WHERE action_id='act-1';
        INSERT INTO agent_actions(action_id,user_id,suggestion_id,initial_user_message_id,execution_target_json,status,final_output,prompt_name,prompt_version,created_at,updated_at) VALUES
          ('action-terminal','user-1',NULL,'message-terminal','{"kind":"scratch"}','success','','test','1','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z');
        INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,updated_at,completed_at,heartbeat_at,terminal_event_id,next_event_seq) VALUES
          ('run-running','user-1','action','running','act-1','2026-08-30T00:04:00.123Z','2026-08-30T00:04:00.123Z',NULL,'2026-08-30T00:04:00.123Z',NULL,2),
          ('run-terminal','user-1','action','completed','action-terminal','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z','event-terminal',2);
        INSERT INTO jobs(job_id,user_id,job_type,process_id,status,scheduled_at,started_at,completed_at,logical_key) VALUES
          ('job-running','user-1','execute_action','run-running','running','2026-08-30T00:04:00.123Z','2026-08-30T00:04:00.123Z',NULL,'act-1'),
          ('job-terminal','user-1','execute_action','run-terminal','completed','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z','action-terminal');
        INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,local_step_number,short_step_id,step_type,step_name,status,goal_handle,user_message_id,user_message_json,user_request_text,accepted_sequence,adopted_process_id,started_at,completed_at,created_at) VALUES
          ('step-terminal','action-terminal','user-1',1,1,'S-1-USER','user_request','user_request','success','S','message-terminal','{}','raw secret needle',1,'run-terminal','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z');
        INSERT INTO process_events(process_id,event_seq,event_id,event_name,payload_json,created_at) VALUES
          ('run-terminal',1,'event-terminal','stream_end','{"final_output":"Terminal Needle RÉSUMÉ"}','2026-08-30T00:03:00.123Z');
        """
    )
    visible_message = ActionUserMessageInput(
        message_id="message-running",
        content="Ship CAFÉ 100%_done",
        supplement="Carefully",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="sug-1", approved_at=_RUNNING_AT
        ),
    ).model_dump_json()
    connection.execute(
        """INSERT INTO agent_action_steps(
               step_id,action_id,user_id,step_number,local_step_number,short_step_id,step_type,
               step_name,status,goal_handle,user_message_id,user_message_json,user_request_text,
               accepted_sequence,adopted_process_id,started_at,completed_at,created_at)
           VALUES (?,?,'user-1',1,1,'S-1-USER','user_request','user_request',
                   'success','S',?,?,?,?,?,?,?,?)""",
        (
            "step-running",
            "act-1",
            "message-running",
            visible_message,
            "rendered metadata must not be searched",
            1,
            "run-running",
            _RUNNING_AT,
            _RUNNING_AT,
            _RUNNING_AT,
        ),
    )
    connection.commit()
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.close()


def _read(
    connection: sqlite3.Connection,
    *,
    search_text: str = "",
    cursor: str | None = None,
    limit: int = 20,
) -> ConversationHistoryCandidatePage:
    return read_conversation_history_candidates_in_connection(
        connection=connection,
        user_id="user-1",
        search_text=search_text,
        cursor=cursor,
        limit=limit,
    )


def test_query_page_contract(history_connection: sqlite3.Connection) -> None:
    first = _read(history_connection, limit=2)

    assert tuple((item.kind, item.stable_id) for item in first.candidates) == (
        ("conversation", "act-1"),
        ("conversation", "action-terminal"),
    )
    assert first.next_cursor is not None

    running = first.candidates[0]
    assert isinstance(running, ActionHistoryCandidate)
    assert (running.updated_at, running.latest_run_id) == (_RUNNING_AT, "run-running")
    terminal = first.candidates[1]
    assert isinstance(terminal, ActionHistoryCandidate)
    assert terminal.initial_user is not None
    assert terminal.initial_user.user_message_json == "{}"
    assert terminal.latest_completion_event_id == "event-terminal"

    second = _read(history_connection, cursor=first.next_cursor, limit=2)
    assert tuple((item.kind, item.stable_id) for item in second.candidates) == (
        ("suggestion", "suggestion-invalid"),
        ("suggestion", "suggestion-message"),
    )
    assert second.next_cursor is None
    invalid = second.candidates[0]
    assert isinstance(invalid, SuggestionHistoryCandidate)
    assert (invalid.answer, invalid.interaction_contract, invalid.user_reaction) == (
        None,
        "action_offer",
        "accepted",
    )

    with pytest.raises(OpaqueCursorError, match="another history query"):
        _read(history_connection, search_text="needle", cursor=first.next_cursor)


def test_search_contract(history_connection: sqlite3.Connection) -> None:
    cases: tuple[tuple[str, tuple[str, ...]], ...] = (
        (" 100%_DONE ", ("act-1",)),
        ("carefully", ("act-1",)),
        ("café", ("act-1",)),
        ("terminal needle", ("action-terminal",)),
        ("résumé", ("action-terminal",)),
        ("standalone 1000xdone", ("suggestion-message",)),
        ("über", ("suggestion-message",)),
        ("raw secret needle", ()),
        ("linked answer", ()),
    )
    for search_text, expected in cases:
        page = _read(history_connection, search_text=search_text)
        assert tuple(item.stable_id for item in page.candidates) == expected


def test_large_lanes_filter_and_seek_before_bounded_outer_sort(
    history_connection: sqlite3.Connection,
) -> None:
    action_rows = (
        (f"noise-action-{index}", f"noise-message-{index}", _RUNNING_AT, _RUNNING_AT)
        for index in range(_NOISE_CANDIDATES_PER_LANE)
    )
    history_connection.executemany(
        """INSERT INTO agent_actions(
        action_id,user_id,initial_user_message_id,execution_target_json,status,
        final_output,prompt_name,prompt_version,created_at,updated_at)
        VALUES (?,'user-1',?,'{"kind":"scratch"}','success','','test','1',?,?)""",
        action_rows,
    )
    suggestion_rows = (
        (f"noise-suggestion-{index}", _RUNNING_AT, _RUNNING_AT)
        for index in range(_NOISE_CANDIDATES_PER_LANE)
    )
    history_connection.executemany(
        """INSERT INTO agent_suggestions(
        suggestion_id,user_id,status,answer,has_suggestion,interaction_contract,
        user_reaction,created_at,updated_at)
        VALUES (?,'user-1','success','irrelevant',1,'action_offer','accepted',?,?)""",
        suggestion_rows,
    )

    cases: tuple[tuple[str, str], ...] = (
        ("terminal needle", "action-terminal"),
        ("standalone", "suggestion-message"),
    )
    for search_text, expected in cases:
        candidate = _read(
            history_connection, search_text=search_text, limit=1
        ).candidates[0]
        assert candidate.stable_id == expected

    details = tuple(
        str(row[3])
        for row in history_connection.execute(
            "EXPLAIN QUERY PLAN "
            + _candidate_query(cursor_bound=True, item_bound=False),
            {
                "user_id": "user-1",
                "search_text": "",
                "search_pattern": "%%",
                "before_updated_at": _RUNNING_AT,
                "before_kind": "conversation",
                "before_id": "act-1",
                "fetch_limit": 2,
            },
        )
    )

    for lane, index in (
        ("action_lane", "idx_agent_actions_user_history_recency"),
        ("suggestion_lane", "idx_agent_suggestions_user_history_recency"),
    ):
        assert f"CO-ROUTINE {lane}" in details
        assert any(f"{index} (user_id=? AND updated_at<?)" in row for row in details)
    # The lanes stay bounded co-routines: nothing materializes.
    assert not {row for row in details if row.startswith("MATERIALIZE")}
    assert details[-2:] == ("SCAN candidates", "USE TEMP B-TREE FOR ORDER BY")


def test_search_keeps_one_deterministic_canonical_proof(
    history_connection: sqlite3.Connection,
) -> None:
    user_match = _read(history_connection, search_text="carefully").candidates[0]
    assert isinstance(user_match, ActionHistoryCandidate)
    assert user_match.matched_user is not None
    assert user_match.matched_user.step_id == "step-running"
    assert user_match.matched_terminal_root_run_id is None

    terminal_match = _read(
        history_connection, search_text="terminal needle"
    ).candidates[0]
    assert isinstance(terminal_match, ActionHistoryCandidate)
    assert terminal_match.matched_user is None
    assert terminal_match.matched_terminal_root_run_id == "run-terminal"

    no_search = _read(history_connection)
    for candidate in no_search.candidates:
        if isinstance(candidate, ActionHistoryCandidate):
            assert candidate.matched_user is None
            assert candidate.matched_terminal_root_run_id is None

    matched_message = ActionUserMessageInput(
        message_id="message-terminal-match",
        content="Terminal Needle",
    ).model_dump_json()
    history_connection.execute(
        """INSERT INTO agent_action_steps(
               step_id,action_id,user_id,step_number,local_step_number,short_step_id,
               step_type,step_name,status,goal_handle,user_message_id,user_message_json,
               user_request_text,accepted_sequence,adopted_process_id,created_at)
           VALUES ('step-terminal-match','action-terminal','user-1',2,2,'S-2-USER',
                   'user_request','user_request','success','S','message-terminal-match',
                   ?,'rendered metadata',2,'run-terminal','2026-08-30T00:03:00.123Z')""",
        (matched_message,),
    )
    both_match = _read(history_connection, search_text="terminal needle").candidates[0]
    assert isinstance(both_match, ActionHistoryCandidate)
    assert both_match.matched_user is not None
    assert both_match.matched_user.step_id == "step-terminal-match"
    assert both_match.matched_terminal_root_run_id is None


def test_answers_are_each_completed_run_once_in_run_order(
    history_connection: sqlite3.Connection,
) -> None:
    later_at = "2026-08-30T00:05:00.123Z"
    history_connection.executescript(
        f"""
        INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,updated_at,completed_at,heartbeat_at,terminal_event_id,next_event_seq) VALUES
          ('run-later','user-1','action','completed','action-terminal','{later_at}','{later_at}','{later_at}','{later_at}','event-later',2),
          ('run-failed','user-1','action','failed','action-terminal','{later_at}','{later_at}','{later_at}','{later_at}','event-failed',2);
        INSERT INTO jobs(job_id,user_id,job_type,process_id,status,scheduled_at,started_at,completed_at,logical_key) VALUES
          ('job-later','user-1','execute_action','run-later','completed','{later_at}','{later_at}','{later_at}','action-terminal'),
          ('job-failed','user-1','execute_action','run-failed','failed','{later_at}','{later_at}','{later_at}','action-terminal');
        INSERT INTO process_events(process_id,event_seq,event_id,event_name,payload_json,created_at) VALUES
          ('run-later',1,'event-later','stream_end','{{"final_output":"Later answer"}}','{later_at}'),
          ('run-failed',1,'event-failed','stream_end','{{"final_output":"Not an answer"}}','{later_at}');
        """
    )
    # A steer adopted into the later run is not another answer.
    history_connection.executemany(
        """INSERT INTO agent_action_steps(
               step_id,action_id,user_id,step_number,local_step_number,short_step_id,
               step_type,step_name,status,goal_handle,user_message_id,user_message_json,
               user_request_text,accepted_sequence,adopted_process_id,created_at)
           VALUES (?,'action-terminal','user-1',?,?,?,'user_request','user_request',
                   'success','S',?,'{}','text',?,?,?)""",
        tuple(
            (f"step-{number}", number, number, f"S-{number}-USER", f"m-{number}")
            + (number, run_id, later_at)
            for number, run_id in (
                (5, "run-later"),
                (6, "run-later"),
                (3, "run-failed"),
            )
        ),
    )

    page = _read(history_connection)
    answers = read_conversation_history_answers_in_connection(
        connection=history_connection, user_id="user-1", candidates=page.candidates
    )
    assert answers == {
        "act-1": (),
        "action-terminal": ("Terminal Needle RÉSUMÉ", "Later answer"),
    }
    assert read_conversation_history_answers_in_connection(
        connection=history_connection, user_id="user-2", candidates=page.candidates
    ) == {"act-1": (), "action-terminal": ()}
    # One read for the page, driven by the listed conversations' runs.
    details = [
        str(row[3])
        for row in history_connection.execute(
            "EXPLAIN QUERY PLAN " + _ANSWERS_SQL,
            {"user_id": "user-1", "action_ids": '["action-terminal"]'},
        )
    ]
    scans = [row for row in details if row.startswith("SCAN")]
    assert len(scans) == 2 and scans[0].startswith("SCAN json_each")
    assert scans[1] == "SCAN run"
    assert any(row.startswith("SEARCH answer USING INDEX") for row in details)


def test_requires_caller_owned_transaction() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.commit()
        with pytest.raises(MigrationError, match="caller-owned transaction"):
            _read(connection)


_TODO_AT = "2026-08-30T00:05:00.123Z"


def _insert_todo(
    connection: sqlite3.Connection,
    todo_id: str,
    *,
    suggestion_id: str | None = None,
    deleted: bool = False,
) -> None:
    connection.execute(
        """INSERT INTO todo_items(
               todo_id,user_id,text,author,origin_kind,suggestion_id,week_start,
               deleted_by,deleted_at,created_at)
           VALUES (?,'user-1','A TODO','ai',?,?,'2026-08-24',?,?,?)""",
        (
            todo_id,
            "written" if suggestion_id is None else "suggestion",
            suggestion_id,
            "user" if deleted else None,
            _TODO_AT if deleted else None,
            _TODO_AT,
        ),
    )


def _insert_action(
    connection: sqlite3.Connection,
    action_id: str,
    *,
    message: ActionUserMessageInput,
    step_name: str = "user_request",
    opening_text: str | None = None,
    reply_to: str | None = None,
) -> None:
    run_id = f"run-{action_id}"
    connection.execute(
        """INSERT INTO agent_actions(
           action_id,user_id,initial_user_message_id,execution_target_json,status,
           final_output,prompt_name,prompt_version,created_at,updated_at)
           VALUES (?,'user-1',?,'{"kind":"scratch"}','success','','test','1',?,?)""",
        (action_id, message.message_id, _TODO_AT, _TODO_AT),
    )
    connection.execute(
        """INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,
           updated_at,completed_at,heartbeat_at,terminal_event_id,next_event_seq)
           VALUES (?,'user-1','action','completed',?,?,?,?,?,NULL,1)""",
        (run_id, action_id, _TODO_AT, _TODO_AT, _TODO_AT, _TODO_AT),
    )
    user_number = 1
    if opening_text is not None:
        user_number = 2
        connection.execute(
            """INSERT INTO agent_action_steps(
               step_id,action_id,user_id,step_number,local_step_number,short_step_id,
               step_type,step_name,status,goal_handle,llm_response_text,
               adopted_process_id,source_suggestion_id,started_at,completed_at,created_at)
               VALUES (?,?,'user-1',1,1,'S-1-ASSISTANT','assistant_message',
                       'assistant_message','success','S',?,?,?,?,?,?)""",
            (f"opening-{action_id}", action_id, opening_text, run_id, reply_to)
            + (_TODO_AT,) * 3,
        )
    connection.execute(
        """INSERT INTO agent_action_steps(
           step_id,action_id,user_id,step_number,local_step_number,short_step_id,
           step_type,step_name,status,goal_handle,user_message_id,user_message_json,
           user_request_text,accepted_sequence,adopted_process_id,created_at)
           VALUES (?,?,'user-1',?,?,?,'user_request',?,'success','S',?,?,?,1,?,?)""",
        (
            f"user-{action_id}",
            action_id,
            user_number,
            user_number,
            f"S-{user_number}-USER",
            step_name,
            message.message_id,
            message.model_dump_json(),
            message.content,
            run_id,
            _TODO_AT,
        ),
    )


def _listed(connection: sqlite3.Connection, search_text: str = "") -> set[str]:
    return {
        item.stable_id for item in _read(connection, search_text=search_text).candidates
    }


def test_a_todos_conversation_leaves_the_dated_list_and_its_search(
    history_connection: sqlite3.Connection,
) -> None:
    # Pantaray picks up a TODO: its first USER message names the TODO it serves.
    _insert_action(
        history_connection,
        "action-pickup",
        message=ActionUserMessageInput(
            message_id="message-pickup",
            content="Pantaray started this turn",
            todo_handoff=TodoHandoffInput(todo_id="todo-picked", project=None),
        ),
        step_name="todo_pickup_request",
        opening_text="Pickup needle",
    )
    # Replying to a Suggestion makes the reply's Action its conversation.
    _insert_action(
        history_connection,
        "action-reply",
        message=ActionUserMessageInput(message_id="message-reply", content="Go"),
        opening_text="Reply needle",
        reply_to="suggestion-message",
    )
    # An Action the user started that serves another TODO's id stays listed:
    # only a TODO row in the list owns a conversation.
    _insert_action(
        history_connection,
        "action-orphan-handoff",
        message=ActionUserMessageInput(
            message_id="message-orphan",
            content="Orphan needle",
            todo_handoff=TodoHandoffInput(todo_id="todo-purged", project=None),
        ),
    )
    everything = {
        "act-1",
        "action-terminal",
        "action-pickup",
        "action-reply",
        "action-orphan-handoff",
        "suggestion-invalid",
    }
    assert _listed(history_connection) == everything
    assert _listed(history_connection, "pickup needle") == {"action-pickup"}

    _insert_todo(history_connection, "todo-picked", deleted=True)
    # act-1 accepted sug-1; action-reply replied to suggestion-message.
    _insert_todo(history_connection, "todo-accepted", suggestion_id="sug-1")
    _insert_todo(history_connection, "todo-replied", suggestion_id="suggestion-message")
    _insert_todo(
        history_connection, "todo-unanswered", suggestion_id="suggestion-invalid"
    )

    assert _listed(history_connection) == {"action-terminal", "action-orphan-handoff"}
    for search_text in ("pickup needle", "reply needle", "café", "standalone"):
        assert _listed(history_connection, search_text) == set()
    assert _listed(history_connection, "orphan needle") == {"action-orphan-handoff"}
    assert _listed(history_connection, "terminal needle") == {"action-terminal"}

    # A link to the conversation still names it.
    for item in (
        ("conversation", "action-pickup"),
        ("conversation", "act-1"),
        ("conversation", "action-reply"),
        ("suggestion", "suggestion-invalid"),
    ):
        page = read_conversation_history_candidates_in_connection(
            connection=history_connection,
            user_id="user-1",
            search_text="",
            cursor=None,
            limit=1,
            item=item,
        )
        assert [candidate.stable_id for candidate in page.candidates] == [item[1]]


def test_a_todos_conversation_is_a_dated_one_while_the_todo_list_is_off(
    history_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(todo_ux_feature, "TODO_UX_ENABLED", False)
    before = _listed(history_connection)
    # TODOs written while it was on keep their conversations, which nothing
    # else would list.
    _insert_todo(history_connection, "todo-accepted", suggestion_id="sug-1")
    _insert_todo(
        history_connection, "todo-unanswered", suggestion_id="suggestion-invalid"
    )

    assert {"act-1", "suggestion-invalid"} <= before
    assert _listed(history_connection) == before
