from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.cursor_codec import (
    OpaqueCursorError,
)
from pantaray_agents.local_runtime.action_conversation.history_candidates import (
    ActionHistoryCandidate,
    ConversationHistoryCandidatePage,
    SuggestionHistoryCandidate,
    _candidate_query,
    read_conversation_history_candidates_in_connection,
    register_conversation_history_casefold_sqlite,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_STEP_NAME,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
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
        INSERT INTO agent_suggestions(suggestion_id,user_id,status,answer,has_suggestion,interaction_contract,user_reaction,delivery_state,created_at,updated_at) VALUES
          ('suggestion-invalid','user-1','success',NULL,1,'action_offer','accepted','released','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z'),
          ('suggestion-message','user-1','success','Standalone 1000Xdone ÜBER',1,'action_offer',NULL,'released','2026-08-30T00:03:00.123Z','2026-08-30T00:03:00.123Z');
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


def test_history_lists_only_suggestions_that_were_shown(
    history_connection: sqlite3.Connection,
) -> None:
    history_connection.executemany(
        """INSERT INTO agent_suggestions(suggestion_id,user_id,status,answer,
        has_suggestion,interaction_contract,delivery_state,created_at,updated_at)
        VALUES (?,'user-1','success','Standalone unshown',1,'message_only',?,
                '2026-08-30T00:09:00.123Z','2026-08-30T00:09:00.123Z')""",
        [(f"unshown-{state}", state) for state in ("held", "expired", "superseded")],
    )

    for search_text, expected in (
        ("", {"suggestion-invalid", "suggestion-message"}),
        ("standalone", {"suggestion-message"}),
    ):
        candidates = _read(history_connection, search_text=search_text).candidates
        assert {c.stable_id for c in candidates if c.kind == "suggestion"} == expected


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
        user_reaction,delivery_state,created_at,updated_at)
        VALUES (?,'user-1','success','irrelevant',1,'action_offer','accepted',
                'released',?,?)""",
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
            "EXPLAIN QUERY PLAN " + _candidate_query(cursor_bound=True),
            {
                "user_id": "user-1",
                "search_text": "",
                "search_pattern": "%%",
                "hidden_step_name": ACTION_RESUME_STEP_NAME,
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


def test_requires_caller_owned_transaction() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.commit()
        with pytest.raises(MigrationError, match="caller-owned transaction"):
            _read(connection)
