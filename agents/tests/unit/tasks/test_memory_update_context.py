from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.artifact_react import ReactToolCall, ToolCallEnvelope
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    MEMORY_RUN_MAX_NEW_EXPERIENCES,
    MemoryRunActionEvidence,
    derived_experience_ids,
    memory_run_binding_from_payload,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    append_memory_category_published_in_connection,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.action_user_message import serialize_action_user_message
from pantaray_agents.tasks.memory_update_context import (
    derived_first_fact_id,
    prepare_memory_update_run,
)
from pantaray_agents.tasks.types import (
    MemoryUpdateActionTerminal,
    MemoryUpdateJobPayload,
)

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
JOB_ID = "job-1"
PROCESS_ID = "process-1"
NOW = "2026-09-07T00:00:00Z"


def _bootstrap(db_path: Path) -> None:
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES (?, 'ja', ?, ?)
                """,
                (USER_ID, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO agent_insights(
                    insight_id, user_id, status, short_term_insight_data, facts,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES ('insight-1', ?, 'success', 'Prefers uv over pip.', '',
                          'insight', '1.0', ?, ?)
                """,
                (USER_ID, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO activity_summaries(
                    summary_id, user_id, summary_type, period_start, period_end,
                    summary, status, prompt_name, prompt_version,
                    created_at, updated_at
                ) VALUES ('summary-1', ?, '24h', '2026-09-06T00:00:00Z', ?,
                          'Worked on the memory agent.', 'success',
                          'activity_summary', '1.0', ?, ?)
                """,
                (USER_ID, NOW, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO processes(
                    process_id, user_id, kind, status, started_at, updated_at,
                    heartbeat_at, next_event_seq
                ) VALUES (?, ?, 'memory', 'running', ?, ?, ?, 1)
                """,
                (PROCESS_ID, USER_ID, NOW, NOW, NOW),
            )
            # The Action the terminals name; a deleted one is skipped.
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id, user_id, initial_user_message_id,
                    execution_target_json, status, final_output, prompt_name,
                    prompt_version, created_at, updated_at
                ) VALUES ('action-1', ?, 'message-1', '{"kind":"scratch"}',
                          'success', 'done', 'action', '1', ?, ?)
                """,
                (USER_ID, NOW, NOW),
            )


def _payload() -> MemoryUpdateJobPayload:
    return {
        "job_id": JOB_ID,
        "process_id": PROCESS_ID,
        "user_id": USER_ID,
        "enqueued_at": NOW,
        "short_insight_ids": ["insight-1"],
        "summary_ids": ["summary-1"],
        "action_terminals": [],
    }


def _runtime(tmp_path: Path) -> LocalMemoryFileEditorRuntime:
    return LocalMemoryFileEditorRuntime(
        artifact_root=tmp_path / "artifacts",
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )


def test_prepare_opens_every_category_and_inlines_derived_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=_payload(), workspace_scope=scope
        )

    assert prepared.editable_sources == (
        "fact",
        "long_term_insight",
        "agent_experience",
    )
    assert prepared.router.editable_policy.allowed_globs == (
        "facts/**",
        "insights/**",
        "agent_experience/entries/*.md",
    )
    assert "Prefers uv over pip." in prepared.context.short_term_insights
    assert "Worked on the memory agent." in prepared.context.activity_summaries
    assert prepared.context.action_turns == ""
    assert prepared.context.new_experience_ids == derived_experience_ids(JOB_ID)
    paths = tuple(document.source_path for document in prepared.router.draft.documents)
    assert paths == (
        "facts/index.md",
        "insights/index.md",
        "agent_experience/index.md",
    )
    tool_names = {definition.name for definition in prepared.tool_definitions}
    assert "memory_search" in tool_names
    # Without an Action terminal the run has no Action history to read.
    assert "history_fetch" not in tool_names


def test_a_category_this_job_already_published_is_not_reopened(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with connection:
            append_memory_category_published_in_connection(
                connection=connection,
                process_id=PROCESS_ID,
                source="fact",
                revision_id="rev-1",
                created_at=NOW,
            )

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=_payload(), workspace_scope=scope
        )

    assert prepared.editable_sources == ("long_term_insight", "agent_experience")
    assert all(
        not document.source_path.startswith("facts/")
        for document in prepared.router.draft.documents
    )


def test_a_published_experience_category_offers_no_new_entry_ids(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with connection:
            append_memory_category_published_in_connection(
                connection=connection,
                process_id=PROCESS_ID,
                source="agent_experience",
                revision_id="rev-1",
                created_at=NOW,
            )

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=_payload(), workspace_scope=scope
        )

    assert prepared.editable_sources == ("fact", "long_term_insight")
    assert prepared.context.new_experience_ids == ()


def test_action_terminals_expose_scoped_history_tools(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    payload = _payload()
    payload["action_terminals"] = [
        {
            "source_id": "turn-1",
            "action_id": "action-1",
            "action_completed_at": NOW,
            "turn_start_step_number": 1,
            "turn_end_step_number": 4,
            "action_prompt_name": "action",
            "action_prompt_version": "2.0",
        }
    ]

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )

    assert "action-1" in prepared.context.action_turns
    assert "steps 1-4," in prepared.context.action_turns
    fetch = next(
        definition
        for definition in prepared.tool_definitions
        if definition.name == "history_fetch"
    )
    properties = fetch.request_schema["properties"]
    assert isinstance(properties, dict)
    assert properties["action_id"] == {"type": "string", "enum": ["action-1"]}


def _insert_user_step(db_path: Path, *, step_number: int, text: str) -> None:
    message = ActionUserMessageInput(message_id=f"message-{step_number}", content=text)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, local_step_number,
                short_step_id, step_type, step_name, status, goal_handle,
                retry_count, prompt_tokens, completion_tokens, user_message_id,
                user_message_json, user_request_text, accepted_sequence,
                adopted_process_id, started_at, completed_at, created_at
            ) VALUES (?, 'action-1', ?, ?, ?, ?, 'user_request', 'user_request',
                      'success', 'S', 0, 0, 0, ?, ?, ?, ?, 'action-process', ?, ?, ?)
            """,
            (
                f"step-{step_number}",
                USER_ID,
                step_number,
                step_number,
                f"S-{step_number}-USER",
                message.message_id,
                serialize_action_user_message(message),
                render_action_user_request_text(message),
                step_number,
                NOW,
                NOW,
                NOW,
            ),
        )


def _turn(start: int, end: int, revision_id: str | None) -> MemoryUpdateActionTerminal:
    terminal: MemoryUpdateActionTerminal = {
        "source_id": f"action-1:{end}",
        "action_id": "action-1",
        "action_completed_at": f"2026-09-07T00:00:0{end // 3}Z",
        "turn_start_step_number": start,
        "turn_end_step_number": end,
        "action_prompt_name": "action",
        "action_prompt_version": f"{end // 3}.0",
    }
    if revision_id is not None:
        terminal["source_action_revision_id"] = revision_id
    return terminal


def _tool_call(name: str, **args: JSONValue) -> ReactToolCall:
    return ReactToolCall(
        tool_name=name,
        tool_args=dict(args),
        tool_call_envelope=ToolCallEnvelope(tool_id=name, reason=None, args=dict(args)),
    )


async def test_every_turn_one_run_coalesced_for_an_action_stays_readable(
    tmp_path: Path,
) -> None:
    # A run that queued while another was running carries several turns of one
    # Action; the user's request in an earlier turn must stay in reach.
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    with sqlite3.connect(runtime.db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, action_id, next_event_seq,
                started_at, updated_at, heartbeat_at
            ) VALUES ('action-process', ?, 'action', 'running', 'action-1', 1,
                      ?, ?, ?)
            """,
            (USER_ID, NOW, NOW, NOW),
        )
    for step_number, text in (
        (1, "Remember the first request"),
        (4, "second"),
        (7, "third"),
    ):
        _insert_user_step(runtime.db_path, step_number=step_number, text=text)
    payload = _payload()
    payload["action_terminals"] = [
        _turn(1, 3, "rev-1"),
        _turn(4, 6, "rev-2"),
        _turn(7, 9, "rev-3"),
    ]

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )

    (listed,) = prepared.context.action_turns.splitlines()
    assert listed.startswith("- action_id: action-1 ")
    assert listed.endswith(", steps 1-9, prompt action@3.0)")
    tools = {definition.name: definition for definition in prepared.tool_definitions}
    fetched = await tools["history_fetch"].execute(
        _tool_call("history_fetch", action_id="action-1", refs=["S-1-USER"]), 1
    )
    assert fetched.status == "success"
    assert "Remember the first request" in json.dumps(fetched.output)
    found = await tools["search_action_steps"].execute(
        _tool_call("search_action_steps", action_id="action-1", query="Remember"), 1
    )
    assert found.status == "success"
    assert isinstance(found.output, dict)
    matches = found.output["matches"]
    assert isinstance(matches, list)
    assert [match["short_step_id"] for match in matches] == ["S-1-USER"]
    # The Action still cites one revision: the newest turn's.
    assert memory_run_binding_from_payload(payload).action_evidence == (
        MemoryRunActionEvidence(
            action_id="action-1", source_action_revision_id="rev-3"
        ),
    )


async def test_a_later_turn_starts_at_its_new_steps_and_reaches_back_to_step_1(
    tmp_path: Path,
) -> None:
    # "Remember this" in a later turn only makes sense against the request
    # that started the Action, which an earlier memory run already recorded.
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    with sqlite3.connect(runtime.db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, action_id, next_event_seq,
                started_at, updated_at, heartbeat_at
            ) VALUES ('action-process', ?, 'action', 'running', 'action-1', 1,
                      ?, ?, ?)
            """,
            (USER_ID, NOW, NOW, NOW),
        )
    for step_number, text in (
        (1, "Draft the launch plan"),
        (4, "Remember to keep the launch plan short"),
        (7, "Launch plan follow-up still in progress"),
    ):
        _insert_user_step(runtime.db_path, step_number=step_number, text=text)
    payload = _payload()
    payload["action_terminals"] = [_turn(4, 6, "rev-2")]

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )

    assert ", steps 4-6, " in prepared.context.action_turns
    tools = {definition.name: definition for definition in prepared.tool_definitions}

    async def refs(name: str, key: str, **args: JSONValue) -> list[JSONValue]:
        result = await tools[name].execute(
            _tool_call(name, action_id="action-1", **args), 1
        )
        assert isinstance(result.output, dict)
        items = result.output[key]
        assert isinstance(items, list)
        return [item["short_step_id"] for item in items if isinstance(item, dict)]

    # The run starts at the steps it records; step 7 is past the bound.
    assert await refs("list_action_steps", "steps") == ["S-4-USER"]
    assert await refs("search_action_steps", "matches", query="launch") == ["S-4-USER"]
    assert await refs("list_action_steps", "steps", from_step=1) == [
        "S-1-USER",
        "S-4-USER",
    ]
    assert await refs(
        "search_action_steps", "matches", query="launch", from_step=1
    ) == ["S-1-USER", "S-4-USER"]
    fetched = await tools["history_fetch"].execute(
        _tool_call("history_fetch", action_id="action-1", refs=["S-1-USER"]), 1
    )
    assert fetched.status == "success"
    assert "Draft the launch plan" in json.dumps(fetched.output)
    beyond = await tools["history_fetch"].execute(
        _tool_call("history_fetch", action_id="action-1", refs=["S-7-USER"]), 1
    )
    assert beyond.status == "error"


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        # An error or cancel turn publishes no revision, so the Action's current
        # revision is still the one the earlier successful turn published.
        (("rev-1", "rev-2", None), "rev-2"),
        ((None, None, None), None),
    ],
)
def test_the_action_cites_the_newest_revision_its_turns_published(
    revisions: tuple[str | None, str | None, str | None], expected: str | None
) -> None:
    payload = _payload()
    payload["action_terminals"] = [
        _turn(1, 3, revisions[0]),
        _turn(4, 6, revisions[1]),
        _turn(7, 9, revisions[2]),
    ]

    assert memory_run_binding_from_payload(payload).action_evidence == (
        MemoryRunActionEvidence(
            action_id="action-1", source_action_revision_id=expected
        ),
    )


def test_the_first_fact_node_id_is_derived_from_the_job(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)

    with MemoryRunWorkspaceScope() as scope:
        prepare_memory_update_run(
            runtime=runtime, payload=_payload(), workspace_scope=scope
        )

    with sqlite3.connect(runtime.db_path) as connection:
        row = connection.execute(
            """
            SELECT source_record_id FROM memory_nodes
            WHERE user_id = ? AND source_type = 'fact'
            """,
            (USER_ID,),
        ).fetchone()
    assert row == (derived_first_fact_id(JOB_ID),)


def test_derived_experience_ids_are_stable_per_job() -> None:
    assert derived_experience_ids(JOB_ID) == derived_experience_ids(JOB_ID)
    assert derived_experience_ids(JOB_ID)[0] != derived_experience_ids("job-2")[0]
    assert len(set(derived_experience_ids(JOB_ID))) == MEMORY_RUN_MAX_NEW_EXPERIENCES


def test_memory_receives_activity_details_for_each_short_insight(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute(
            """
            INSERT INTO activity_logs(
                log_id, user_id, period_start, period_end, description, status,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES ('insight-1', ?, ?, ?, ?, 'success', 'insight', '2.2', ?, ?)
            """,
            (
                USER_ID,
                NOW,
                NOW,
                "Client requested the revised estimate by Friday.",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO activity_logs(
                log_id, user_id, period_start, period_end, description, status,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES ('unselected', ?, '2026-09-06T23:45:00Z', ?, 'Unrelated private detail',
                      'success', 'insight', '2.2', ?, ?)
            """,
            (USER_ID, NOW, NOW, NOW),
        )
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=_payload(), workspace_scope=scope
        )
    evidence = prepared.context.short_term_insights
    assert "Prefers uv over pip." in evidence
    assert "Client requested the revised estimate by Friday." in evidence
    assert "Objective Activity" in evidence
    assert "Unrelated private detail" not in evidence


@pytest.mark.usefixtures("tokyo_local_zone")
def test_memory_seed_evidence_shows_local_times(tmp_path: Path) -> None:
    # 21:50Z is already the next morning in Tokyo; Memory must see the 27th.
    late = "2026-09-26T21:50:00.000Z"
    runtime = _runtime(tmp_path)
    _bootstrap(runtime.db_path)
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_insights(
                insight_id, user_id, status, short_term_insight_data, facts,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES ('insight-late', ?, 'success', 'Late insight.', '',
                      'insight', '1.0', ?, ?)
            """,
            (USER_ID, late, late),
        )
        connection.execute(
            """
            INSERT INTO activity_logs(
                log_id, user_id, period_start, period_end, description, status,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES ('insight-late', ?, ?, '2026-09-26T21:54:00.000Z',
                      'Late activity.', 'success', 'insight', '2.2', ?, ?)
            """,
            (USER_ID, late, late, late),
        )
        connection.execute(
            """
            INSERT INTO activity_summaries(
                summary_id, user_id, summary_type, period_start, period_end,
                summary, status, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES ('summary-late', ?, '24h', '2026-09-25T21:00:00Z',
                      '2026-09-26T21:00:00Z', 'Late summary.', 'success',
                      'activity_summary', '1.0', ?, ?)
            """,
            (USER_ID, late, late),
        )
    payload = _payload()
    payload["short_insight_ids"] = ["insight-late"]
    payload["summary_ids"] = ["summary-late"]
    payload["action_terminals"] = [
        {
            "source_id": "turn-1",
            "action_id": "action-1",
            "action_completed_at": late,
            "turn_start_step_number": 1,
            "turn_end_step_number": 4,
            "action_prompt_name": "action",
            "action_prompt_version": "2.0",
        }
    ]

    with MemoryRunWorkspaceScope() as scope:
        context = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        ).context

    assert context.local_time_note == "Times are local (Asia/Tokyo)."
    assert context.short_term_insights.startswith(
        "### short-term Insight insight-late (2026-09-27T06:50+09:00)\n"
    )
    assert (
        "#### Objective Activity (2026-09-27T06:50+09:00 - 2026-09-27T06:54+09:00)"
        in context.short_term_insights
    )
    assert context.activity_summaries.startswith(
        "### 24h Activity Summary summary-late "
        "(2026-09-26T06:00+09:00 - 2026-09-27T06:00+09:00)\n"
    )
    assert "(completed at 2026-09-27T06:50+09:00, " in context.action_turns
