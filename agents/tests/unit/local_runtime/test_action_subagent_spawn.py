from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ActionSubagentMessageInputError,
    ActionSubagentMessageRequest,
    send_action_subagent_message,
)
from pantaray_agents.local_runtime.runtime.action_subagent_spawn import (
    ActionSubagentSpawnAuthorityError,
    ActionSubagentSpawnConflictError,
    ActionSubagentSpawnRequest,
    ActionSubagentSpawnRequestError,
    ActionSubagentSpawnResult,
    ExternalSpawnResourceClaim,
    WorkspaceSpawnResourceClaim,
    spawn_action_subagent,
)
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    ACTION_SUBAGENT_PAYLOAD_MAX_BYTES,
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    ActionSubagentResourceClaimConflictError,
)
from pantaray_agents.local_runtime.tooling.action_subagent_resource_identity import (
    ActionSubagentResourceIdentityError,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin

from .action_seed import insert_agent_action
from .resource_recovery_test_support import bootstrap_runtime_db

BUSY_TIMEOUT_MS = 1_000
TIMESTAMP = "2026-09-01T00:00:00Z"


def _runtime(
    tmp_path: Path, *, seed_think: bool = True
) -> tuple[Path, ActionExecutionContext]:
    db_path, raw_context = bootstrap_runtime_db(tmp_path)
    context = cast(ActionExecutionContext, raw_context)
    with _connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO processes(
                    process_id, user_id, kind, status, action_id, current_job_id,
                    started_at, updated_at, heartbeat_at, next_event_seq
                ) VALUES (
                    'parent-process', 'user-1', 'action', 'running', 'action-1',
                    'parent-job', ?, ?, ?, 1
                )
                """,
                (TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, user_id, job_type, process_id, status, attempt,
                    claimed_by, claimed_at, heartbeat_at, scheduled_at,
                    started_at, logical_key
                ) VALUES (
                    'parent-job', 'user-1', 'execute_action', 'parent-process',
                    'running', 1, 'test-worker', ?, ?, ?, ?, 'action-1'
                )
                """,
                (TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO job_payloads(job_id, payload_json) VALUES (?, ?)",
                (
                    "parent-job",
                    json.dumps(
                        build_action_job_payload(
                            {
                                "job_id": "parent-job",
                                "process_id": "parent-process",
                                "action_id": "action-1",
                                "user_id": "user-1",
                                "continuation_ref": {
                                    "kind": "user_step",
                                    "user_step_id": "root-user-step",
                                },
                            }
                        )
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, step_number, local_step_number,
                    short_step_id, step_type, step_name, status, goal_handle,
                    user_request_text, accepted_sequence, adopted_process_id,
                    started_at, completed_at, created_at
                ) VALUES (
                    'root-user-step', 'action-1', 'user-1', 1, 1, 'S-1-USER',
                    'user_request', 'user_request', 'success', 'S', 'Start work',
                    1, 'parent-process', ?, ?, ?
                )
                """,
                (TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
    if seed_think:
        _insert_think(db_path, _request(context))
    return db_path, context


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    configure_connection(connection, BUSY_TIMEOUT_MS)
    connection.row_factory = sqlite3.Row
    return connection


def _request(
    context: ActionExecutionContext,
    *,
    llm_step_id: str = "supervisor-think-1",
    claim_key: str = "repository:src",
) -> ActionSubagentSpawnRequest:
    return ActionSubagentSpawnRequest(
        user_id="user-1",
        action_id="action-1",
        parent_process_id="parent-process",
        parent_job_id="parent-job",
        root_execution_session_id=context.execution_session_id,
        manifest_id=context.manifest_id,
        origin=ActionToolCallOrigin(llm_step_id=llm_step_id, call_id="call-1"),
        model_selector="gpt-6-luna",
        action_context="# Workspace Paths\nparent context",
        task="Inspect the assigned boundary",
        context_refs=("conversation:step-1",),
        resource_claims=(ExternalSpawnResourceClaim("workspace", claim_key),),
        spawned_at=TIMESTAMP,
    )


def _insert_think(
    db_path: Path,
    request: ActionSubagentSpawnRequest,
    *,
    parent_step_id: str = "root-user-step",
    step_number: int = 2,
) -> None:
    with _connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, parent_step_id, step_number,
                    local_step_number, short_step_id, step_type, step_name, status,
                    goal_handle, started_at, completed_at, created_at
                ) VALUES (?, 'action-1', 'user-1', ?, ?, ?,
                          'S-2-THINK', 'llm_output', 'supervisor_think', 'success',
                          'S', ?, ?, ?)
                """,
                (
                    request.origin.llm_step_id,
                    parent_step_id,
                    step_number,
                    step_number,
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            )


def _spawn(
    db_path: Path,
    request: ActionSubagentSpawnRequest,
) -> ActionSubagentSpawnResult:
    return spawn_action_subagent(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        request=request,
    )


def _row_counts(db_path: Path) -> tuple[int, int, int]:
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM processes WHERE kind='action_subagent'),
                (SELECT COUNT(*) FROM jobs WHERE job_type='execute_action_subagent'),
                (SELECT COUNT(*) FROM action_subagent_resource_claims)
            """
        ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def test_exact_replay_is_stable_and_changed_content_conflicts(
    tmp_path: Path,
) -> None:
    db_path, context = _runtime(tmp_path)
    request = _request(context)

    created = _spawn(db_path, request)
    replay = _spawn(db_path, request)

    assert created.inserted_new is True
    assert replay == replace(created, inserted_new=False)
    assert _row_counts(db_path) == (1, 1, 1)
    for changed in (
        replace(request, task="Different task"),
        replace(request, context_refs=("conversation:different",)),
        replace(request, model_selector="gpt-5.6-sol"),
        replace(
            request,
            resource_claims=(ExternalSpawnResourceClaim("workspace", "other"),),
        ),
    ):
        with pytest.raises(ActionSubagentSpawnConflictError):
            _spawn(db_path, changed)
    assert _row_counts(db_path) == (1, 1, 1)

    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    copy_db, copy_context = _runtime(copy_root)
    copied = _spawn(copy_db, _request(copy_context))
    assert (copied.child_process_id, copied.job_id) == (
        created.child_process_id,
        created.job_id,
    )
    with _connect(db_path) as connection, _connect(copy_db) as copy_connection:
        claim_id = connection.execute(
            "SELECT claim_id FROM action_subagent_resource_claims"
        ).fetchone()[0]
        copied_claim_id = copy_connection.execute(
            "SELECT claim_id FROM action_subagent_resource_claims"
        ).fetchone()[0]
    assert copied_claim_id == claim_id


def test_identical_read_only_requests_with_distinct_call_ids_spawn_separate_children(
    tmp_path: Path,
) -> None:
    db_path, context = _runtime(tmp_path)
    first = replace(_request(context), resource_claims=())
    second = replace(
        first,
        origin=ActionToolCallOrigin(
            llm_step_id=first.origin.llm_step_id, call_id="call-2"
        ),
    )
    first_result = _spawn(db_path, first)
    second_result = _spawn(db_path, second)
    assert first_result.child_process_id != second_result.child_process_id
    assert first_result.job_id != second_result.job_id
    assert _spawn(db_path, second) == replace(second_result, inserted_new=False)
    assert _row_counts(db_path) == (2, 2, 0)


def test_spawn_accepts_think_from_second_user_origin_run(tmp_path: Path) -> None:
    db_path, context = _runtime(tmp_path)
    request = replace(
        _request(context, llm_step_id="second-think"),
        parent_process_id="second-process",
        parent_job_id="second-job",
    )
    with _connect(db_path) as connection:
        with connection:
            connection.executescript(
                f"""
                UPDATE processes SET status='completed',current_job_id=NULL,completed_at='{TIMESTAMP}' WHERE process_id='parent-process';
                UPDATE jobs SET status='completed',completed_at='{TIMESTAMP}' WHERE job_id='parent-job';
                INSERT INTO processes(process_id,user_id,kind,status,action_id,current_job_id,started_at,updated_at,heartbeat_at,next_event_seq)
                VALUES ('second-process','user-1','action','running','action-1',
                    'second-job','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}',1);
                INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,claimed_by,claimed_at,heartbeat_at,scheduled_at,started_at,logical_key)
                VALUES ('second-job','user-1','execute_action','second-process','running',
                    1,'test-worker','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}',
                    '{TIMESTAMP}','action-1');
                INSERT INTO agent_action_steps(step_id,action_id,user_id,parent_step_id,step_number,local_step_number,short_step_id,step_type,step_name,status,goal_handle,user_request_text,accepted_sequence,adopted_process_id,started_at,completed_at,created_at)
                VALUES ('second-user-step','action-1','user-1','supervisor-think-1',3,3,
                    'S-3-USER','user_request','user_request','success','S','Continue work',
                    2,'second-process','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}');
                """
            )
            connection.execute(
                "INSERT INTO job_payloads(job_id,payload_json) VALUES (?, ?)",
                (
                    "second-job",
                    json.dumps(
                        build_action_job_payload(
                            {
                                "job_id": "second-job",
                                "process_id": "second-process",
                                "action_id": "action-1",
                                "user_id": "user-1",
                                "continuation_ref": {
                                    "kind": "user_step",
                                    "user_step_id": "second-user-step",
                                },
                            }
                        )
                    ),
                ),
            )
    _insert_think(db_path, request, parent_step_id="second-user-step", step_number=4)

    assert _spawn(db_path, request).inserted_new is True
    with pytest.raises(ActionSubagentSpawnAuthorityError):
        _spawn(db_path, replace(request, origin=_request(context).origin))


def test_job_insert_failure_rolls_back_child_registration(tmp_path: Path) -> None:
    db_path, context = _runtime(tmp_path)
    with _connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TRIGGER reject_child_job BEFORE INSERT ON jobs
            WHEN NEW.job_type = 'execute_action_subagent'
            BEGIN SELECT RAISE(ABORT, 'injected child job failure'); END;
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected child job failure"):
        _spawn(db_path, _request(context))
    assert _row_counts(db_path) == (0, 0, 0)


def test_invalid_request_and_claim_failure_roll_back_before_commit(
    tmp_path: Path,
) -> None:
    db_path, context = _runtime(tmp_path)
    _spawn(db_path, _request(context))

    overlap = _request(
        context,
        llm_step_id="supervisor-think-overlap",
    )
    overlap = replace(
        overlap,
        resource_claims=(
            ExternalSpawnResourceClaim("workspace", "repository:other"),
            overlap.resource_claims[0],
        ),
    )
    _insert_think(db_path, overlap)
    with pytest.raises(ActionSubagentResourceClaimConflictError):
        _spawn(db_path, overlap)
    invalid = replace(
        _request(context, llm_step_id="supervisor-think-invalid"),
        resource_claims=(ExternalSpawnResourceClaim(" ", "invalid"),),
    )
    _insert_think(db_path, invalid)
    with pytest.raises(ValueError, match="must not be blank"):
        _spawn(db_path, invalid)
    outside = replace(
        _request(context, llm_step_id="supervisor-think-outside"),
        resource_claims=(
            WorkspaceSpawnResourceClaim("../../outside", context.workspace_path),
        ),
    )
    _insert_think(db_path, outside)
    with pytest.raises(ActionSubagentResourceIdentityError, match="outside"):
        _spawn(db_path, outside)
    oversized = replace(
        _request(context), context_refs=("x" * ACTION_SUBAGENT_PAYLOAD_MAX_BYTES,)
    )
    with pytest.raises(ActionSubagentSpawnRequestError, match="payload exceeds"):
        _spawn(db_path, oversized)

    assert _row_counts(db_path) == (1, 1, 1)


def test_child_message_replay_conflict_and_terminal_boundary(tmp_path: Path) -> None:
    db_path, context = _runtime(tmp_path)
    child = _spawn(db_path, _request(context))
    insert_agent_action(
        db_path=db_path, suggestion_id="suggestion-2", action_id="action-2"
    )
    with _connect(db_path) as connection, connection:
        connection.executescript(
            f"""
            INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,
                updated_at,heartbeat_at,next_event_seq,parent_process_id) VALUES
                ('other-parent','user-1','action','running','action-2','{TIMESTAMP}',
                 '{TIMESTAMP}','{TIMESTAMP}',1,NULL);
            INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,
                updated_at,heartbeat_at,next_event_seq,parent_process_id) VALUES
                ('foreign-child','user-1','action_subagent','enqueued','action-2',
                 '{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}',1,'other-parent');
            """
        )

    def send(
        message_id: str,
        content: str,
        child_process_id: str = child.child_process_id,
    ) -> None:
        send_action_subagent_message(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            request=ActionSubagentMessageRequest(
                "user-1",
                "action-1",
                "parent-process",
                "parent-job",
                child_process_id,
                message_id,
                content,
            ),
        )

    send("message-1", "Use the updated boundary")
    with pytest.raises(ActionSubagentMessageInputError, match="active parent"):
        send("cross-parent", "Wrong parent", "foreign-child")
    send("message-1", "Use the updated boundary")
    with pytest.raises(ActionSubagentMessageInputError, match="different content"):
        send("message-1", "Different correction")
    with _connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE processes SET status='completed' WHERE process_id=?",
            (child.child_process_id,),
        )
    send("message-1", "Use the updated boundary")
    with pytest.raises(ActionSubagentMessageInputError, match="terminal"):
        send("message-2", "Too late")

    with _connect(db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM process_events WHERE process_id=?",
            (child.child_process_id,),
        ).fetchone()[0]
    assert event_count == 1


@pytest.mark.parametrize(
    ("model_selector", "profile_id"),
    (
        ("gpt-6-luna", "action.subagent.luna"),
        ("gpt-5.6-sol", "action.subagent.sol"),
    ),
)
def test_spawn_persists_the_selected_configured_profile(
    tmp_path: Path,
    model_selector: str,
    profile_id: str,
) -> None:
    db_path, context = _runtime(tmp_path)
    request = replace(
        _request(context, llm_step_id=f"think-{model_selector}"),
        model_selector=model_selector,
    )
    _insert_think(db_path, request)

    result = _spawn(db_path, request)

    with _connect(db_path) as connection:
        payload_json = connection.execute(
            "SELECT payload_json FROM job_payloads WHERE job_id = ?",
            (result.job_id,),
        ).fetchone()[0]
    assert (
        parse_action_subagent_job_payload_json(payload_json)["inference_profile_id"]
        == profile_id
    )


@pytest.mark.parametrize(
    "authority_case",
    (
        "non_running",
        "cancel_requested",
        "cross_owner",
        "session_mismatch",
        "manifest_mismatch",
        "think_not_durable",
        "cwd_mismatch",
    ),
)
def test_spawn_requires_exact_active_parent_runtime_authority(
    tmp_path: Path,
    authority_case: str,
) -> None:
    db_path, context = _runtime(tmp_path)
    request = _request(context)
    if authority_case == "non_running":
        with _connect(db_path) as connection:
            with connection:
                connection.execute(
                    "UPDATE jobs SET status='completed' WHERE job_id='parent-job'"
                )
    elif authority_case == "cancel_requested":
        with _connect(db_path) as connection:
            with connection:
                connection.execute(
                    "UPDATE jobs SET cancel_requested_at=? WHERE job_id='parent-job'",
                    (TIMESTAMP,),
                )
    elif authority_case == "cross_owner":
        request = replace(request, user_id="user-2")
    elif authority_case == "session_mismatch":
        request = replace(request, root_execution_session_id="another-session")
    elif authority_case == "manifest_mismatch":
        request = replace(request, manifest_id="another-manifest")
    elif authority_case == "think_not_durable":
        with _connect(db_path) as connection:
            with connection:
                connection.execute(
                    "UPDATE agent_action_steps SET status='error' WHERE step_id=?",
                    (request.origin.llm_step_id,),
                )
    else:
        durable_request = replace(
            request,
            origin=ActionToolCallOrigin(
                llm_step_id="supervisor-think-cwd", call_id="call-1"
            ),
            resource_claims=(
                WorkspaceSpawnResourceClaim(
                    raw_path="src",
                    current_cwd=context.workspace_path,
                ),
            ),
        )
        _insert_think(db_path, durable_request)
        request = replace(
            durable_request,
            resource_claims=(
                WorkspaceSpawnResourceClaim(
                    raw_path="src",
                    current_cwd=tmp_path / "another-workspace",
                ),
            ),
        )

    with pytest.raises(ActionSubagentSpawnAuthorityError):
        _spawn(db_path, request)
    assert _row_counts(db_path) == (0, 0, 0)
