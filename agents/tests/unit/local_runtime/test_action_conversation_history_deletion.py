from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation import history_deletion
from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    ConversationBusyError,
    HistoryItemKind,
    delete_history_item,
)
from pantaray_agents.local_runtime.memory_catalog import (
    agent_experience_content as experience,
)
from pantaray_agents.local_runtime.memory_catalog import (
    agent_experience_delta,
    agent_experience_validation,
    artifact_workspace,
    domain_registration,
    draft,
    epoch,
    publication,
    reconciler,
    repository,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryEvidenceEdge,
    MemoryRevision,
    MemorySource,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
    register_after_commit,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER = "user-1"
T = "2026-09-01T00:00:00Z"


def _image(name: str) -> str:
    return f"{USER}/2026-09-01/{name}-0000-4000-8000-000000000000.png"


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    _seed(
        db_path,
        f"""INSERT INTO users(user_id,ui_language,created_at,updated_at)
            VALUES ('{USER}','ja','{T}','{T}');
          INSERT INTO tool_definitions(tool_id,tool_name,tool_description,category,
            risk_level,input_schema_json,is_enabled,version,created_at,updated_at)
            VALUES ('shell','shell','run','exec','high','{{}}',1,'1','{T}','{T}');""",
    )
    return db_path


def _seed(db_path: Path, script: str) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(script)


def _suggestion(suggestion_id: str, contract: str = "action_offer") -> str:
    return f"""
      INSERT INTO agent_suggestions(suggestion_id,user_id,status,answer,has_suggestion,
        interaction_contract,created_at,updated_at)
        VALUES ('{suggestion_id}','{USER}','success','answer',1,'{contract}','{T}','{T}');
      INSERT INTO processes(process_id,user_id,kind,status,suggestion_id,started_at,
        updated_at,heartbeat_at,next_event_seq)
        VALUES ('run-{suggestion_id}','{USER}','suggestion','completed',
                '{suggestion_id}','{T}','{T}','{T}',1);"""


def _action(
    action_id: str,
    *,
    suggestion_id: str | None = None,
    images: tuple[str, str] = (_image("aaaaaaaa"), _image("bbbbbbbb")),
) -> str:
    """A finished conversation that ran an approved command beside a subagent."""

    suggestion = f"'{suggestion_id}'" if suggestion_id else "NULL"
    user_message = json.dumps(
        {"images": [{"kind": "image", "storage_path": images[0]}]}
    )
    tool_output = (
        '{"schema_version":1,"status":"success","output_storage_kind":"inline_json",'
        '"output_owner_kind":"action","output":{"attachments":[{"source_kind":'
        f'"local_image_blob","mime_type":"image/png","storage_path":"{images[1]}"}}]}}}}'
    )
    a = action_id
    return f"""
      INSERT INTO agent_actions(action_id,user_id,suggestion_id,initial_user_message_id,
        execution_target_json,status,final_output,prompt_name,prompt_version,created_at,
        updated_at) VALUES ('{a}','{USER}',{suggestion},'message-{a}','{{"kind":"scratch"}}',
        'success','done','p','1','{T}','{T}');
      INSERT INTO processes(process_id,user_id,kind,status,suggestion_id,action_id,
        started_at,updated_at,heartbeat_at,next_event_seq,parent_process_id) VALUES
        ('run-{a}','{USER}','action','running',{suggestion},'{a}','{T}','{T}','{T}',1,NULL),
        ('sub-{a}','{USER}','action_subagent','running',NULL,'{a}','{T}','{T}','{T}',1,
         'run-{a}');
      UPDATE processes SET status='completed' WHERE action_id='{a}';
      INSERT INTO jobs(job_id,user_id,job_type,process_id,status,scheduled_at) VALUES
        ('job-run-{a}','{USER}','execute_action','run-{a}','completed','{T}'),
        ('job-sub-{a}','{USER}','execute_action','sub-{a}','completed','{T}');
      INSERT INTO job_payloads(job_id,payload_json) VALUES ('job-run-{a}','{{}}');
      INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,local_step_number,
        short_step_id,step_type,step_name,status,goal_handle,user_message_id,
        user_message_json,user_request_text,accepted_sequence,adopted_process_id,created_at)
        VALUES ('user-{a}','{a}','{USER}',1,1,'S-1-USER','user_request','user_request',
        'success','S','message-{a}','{user_message}','request',1,'run-{a}','{T}');
      INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,step_type,
        step_name,status,tool_output,created_at) VALUES ('tool-{a}','{a}','{USER}',2,
        'tool_execution','shell','success','{tool_output}','{T}');
      INSERT INTO execution_sessions(execution_session_id,user_id,action_id,exec_mode,
        cwd_path,network_policy,capability_snapshot_json,status,started_at) VALUES
        ('session-{a}','{USER}','{a}','workspace_command','/tmp','deny','{{}}','completed',
         '{T}');
      INSERT INTO tool_invocations(invocation_id,user_id,action_id,tool_id,
        execution_session_id,intent_class,started_at,status) VALUES ('inv-{a}','{USER}',
        '{a}','shell','session-{a}','process_exec_local','{T}','completed');
      INSERT INTO approval_sessions(approval_session_id,user_id,action_id,tool_request_id,
        tool_invocation_id,tool_id,intent_class,approval_source,status,
        approved_capabilities_json,command_summary_json,requested_at,decided_at,created_at,
        claimed_at) VALUES ('approval-{a}','{USER}','{a}','req-{a}','inv-{a}','shell',
        'process_exec_local','prompt','approved_once','{{}}','{{}}','{T}','{T}','{T}','{T}');
      INSERT INTO command_invocation_audits(invocation_id,approval_session_id,
        execution_kind,executable_source_kind,resolved_executable_path,terminal_outcome,
        stdout_max_bytes,stderr_max_bytes,temp_storage_limit_bytes,child_count_limit,
        open_file_lease_limit,created_at,updated_at) VALUES ('inv-{a}','approval-{a}',
        'workspace_command','trusted_system_executable','/bin/sh','exited',1,1,1,1,1,
        '{T}','{T}');
      INSERT INTO tool_runtime_resources(resource_id,execution_session_id,action_id,
        resource_kind,status,created_at,updated_at) VALUES ('resource-{a}','session-{a}',
        '{a}','temp_dir','cleaned','{T}','{T}');
      INSERT INTO tool_runtime_resource_events(event_id,resource_id,action_id,event_type,
        message,created_at) VALUES ('event-{a}','resource-{a}','{a}','cleanup','done','{T}');
      INSERT INTO memory_agent_triggers(user_id,trigger_kind,source_id,status,created_at,
        action_id,action_completed_at,turn_start_step_number,turn_end_step_number,
        action_prompt_name,action_prompt_version) VALUES ('{USER}',
        'memory_from_action_terminal','{a}','pending','{T}','{a}','{T}',1,2,'p','1');"""


def _delete(db_path: Path, kind: HistoryItemKind, item_id: str) -> None:
    delete_history_item(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=db_path.parent / "artifacts",
        user_id=USER,
        kind=kind,
        item_id=item_id,
    )


def _counts(db_path: Path, action_id: str, suggestion_id: str) -> tuple[int, ...]:
    with closing(sqlite3.connect(db_path)) as connection:
        return tuple(
            connection.execute(
                """SELECT
                  (SELECT COUNT(*) FROM agent_actions WHERE action_id = :a),
                  (SELECT COUNT(*) FROM agent_action_steps WHERE action_id = :a),
                  (SELECT COUNT(*) FROM tool_invocations WHERE action_id = :a),
                  (SELECT COUNT(*) FROM approval_sessions WHERE action_id = :a),
                  (SELECT COUNT(*) FROM command_invocation_audits
                     WHERE invocation_id = 'inv-' || :a),
                  (SELECT COUNT(*) FROM tool_runtime_resource_events
                     WHERE event_id = 'event-' || :a),
                  (SELECT COUNT(*) FROM processes
                     WHERE action_id = :a OR suggestion_id = :s),
                  (SELECT COUNT(*) FROM jobs WHERE job_id LIKE 'job-%' || :a),
                  (SELECT COUNT(*) FROM memory_agent_triggers WHERE action_id = :a),
                  (SELECT COUNT(*) FROM agent_suggestions WHERE suggestion_id = :s),
                  (SELECT COUNT(*) FROM pragma_foreign_key_check)""",
                {"a": action_id, "s": suggestion_id},
            ).fetchone()
        )


@pytest.mark.parametrize(
    ("link", "kind", "item"),
    [
        ("offer", "conversation", "act-1"),
        ("offer", "suggestion", "sug-1"),
        ("reply", "conversation", "act-1"),
        ("reply", "suggestion", "sug-1"),
        ("standalone", "suggestion", "sug-1"),
        ("user", "conversation", "act-1"),
    ],
)
def test_delete_removes_the_whole_conversation_and_its_other_half(
    tmp_path: Path, link: str, kind: HistoryItemKind, item: str
) -> None:
    db_path = _db(tmp_path)
    # Memory updates running and queued over this Action do not block the delete.
    payload = json.dumps({"action_terminals": [{"action_id": "act-1"}]})
    script = _suggestion("sug-other") + _action("act-other")
    for status in ("running", "queued"):
        script += f"""
          INSERT INTO jobs(job_id,user_id,job_type,status,scheduled_at)
            VALUES ('memory-{status}','{USER}','memory_update','{status}','{T}');
          INSERT INTO job_payloads(job_id,payload_json)
            VALUES ('memory-{status}','{payload}');"""
    if link != "user":
        script += _suggestion(
            "sug-1", "message_only" if link == "reply" else "action_offer"
        )
    if link != "standalone":
        script += _action("act-1", suggestion_id="sug-1" if link == "offer" else None)
    if link == "reply":
        script += f"""
          INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,
            local_step_number,short_step_id,step_type,step_name,status,goal_handle,
            llm_response_text,adopted_process_id,source_suggestion_id,started_at,
            completed_at,created_at) VALUES ('reply','act-1','{USER}',3,2,
            'S-2-ASSISTANT','assistant_message','assistant','success','S','reply',
            'run-act-1','sug-1','{T}','{T}','{T}');"""
    _seed(db_path, script)
    other = _counts(db_path, "act-other", "sug-other")

    _delete(db_path, kind, item)
    _delete(db_path, kind, item)

    assert set(_counts(db_path, "act-1", "sug-1")) == {0}
    assert _counts(db_path, "act-other", "sug-other") == other


@pytest.mark.parametrize(
    "busy",
    [
        "UPDATE agent_actions SET status='processing'",
        "UPDATE processes SET status='paused'",
        "UPDATE jobs SET status='queued'",
    ],
)
def test_active_conversation_is_refused_and_kept(tmp_path: Path, busy: str) -> None:
    db_path = _db(tmp_path)
    _seed(db_path, f"{_action('act-1')}{busy};")
    before = _counts(db_path, "act-1", "")

    with pytest.raises(ConversationBusyError):
        _delete(db_path, "conversation", "act-1")

    assert _counts(db_path, "act-1", "") == before


def _files(db_path: Path, images: tuple[str, ...]) -> tuple[Path, ...]:
    paths = resolve_action_storage_paths(
        db_path=db_path, user_id=USER, action_id="act-1"
    )
    paths.workspace.mkdir(parents=True)
    (paths.workspace / "notes.txt").write_text("scratch")
    files = [paths.action_root]
    for image in images:
        files.append(db_path.parent / "artifacts/generated/images" / image)
        files[-1].parent.mkdir(parents=True, exist_ok=True)
        files[-1].write_bytes(b"png")
    return tuple(files)


@pytest.mark.parametrize("fails", [False, True])
def test_files_go_only_after_commit_and_shared_images_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    db_path = _db(tmp_path)
    own, shared = _image("aaaaaaaa"), _image("cccccccc")
    _seed(
        db_path,
        _action("act-1", images=(own, shared))
        + _action("act-other", images=(shared, _image("dddddddd"))),
    )
    files = _files(db_path, (own, shared))
    if fails:
        # The transaction fails after the file removal was scheduled.
        def register_then_fail(
            *, connection: sqlite3.Connection, callback: Callable[[], None]
        ) -> None:
            register_after_commit(connection=connection, callback=callback)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(
            history_deletion, "register_after_commit", register_then_fail
        )
        with pytest.raises(sqlite3.OperationalError):
            _delete(db_path, "conversation", "act-1")
    else:
        _delete(db_path, "conversation", "act-1")

    assert _counts(db_path, "act-1", "")[0] == int(fails)
    assert [path.exists() for path in files] == [fails, fails, True]


def test_an_image_the_chat_still_shows_stays(tmp_path: Path) -> None:
    """The chat hands its images to a task by the same path and keeps showing them."""

    db_path = _db(tmp_path)
    own, handed = _image("aaaaaaaa"), _image("eeeeeeee")
    chat_payload = json.dumps(
        {
            "kind": "user_message",
            "quote_item_id": None,
            "images": [{"kind": "image", "storage_path": handed}],
        }
    )
    _seed(
        db_path,
        _action("act-1", images=(own, handed))
        + f"""INSERT INTO chat_items(item_id,user_id,message_id,kind,payload,created_at)
              VALUES ('item-1','{USER}','chat-1','user_message','{chat_payload}','{T}');""",
    )
    files = _files(db_path, (own, handed))

    _delete(db_path, "conversation", "act-1")

    assert [path.exists() for path in files] == [False, False, True]


def _publish_linked(
    db_path: Path,
    *,
    source: MemorySource,
    record_id: str,
    documents: tuple[MemoryDocument, ...],
    anchor: tuple[str, str, str],
    target: MemoryRevision,
) -> None:
    """Publish learned memory whose ``anchor`` (path, text, note) links to a copy."""

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            node = repository.ensure_preparing_node(
                connection=connection,
                user_id=USER,
                source=source,
                source_record_id=record_id,
            )
        target_node = repository.load_node(
            connection=connection, user_id=USER, node_id=target.node_id
        )
        fragment = repository.list_revision_fragments(
            connection=connection, user_id=USER, revision_id=target.revision_id
        )[-1]
    assert target_node is not None
    context = epoch.build_memory_context_epoch(
        run_id="run-1", user_id=USER, visible=((target_node, fragment, "evidence"),)
    )
    linked = draft.create_memory_draft(
        user_id=USER,
        owner_node_id=node.node_id,
        base_revision_id=None,
        documents=documents,
    )
    path, text, note = anchor
    linked = draft.link_memory(
        draft=linked,
        command=draft.seal_link_command(
            epoch=context,
            draft=linked,
            tool_invocation_id=f"link-{source}",
            target_handle=context.items[0].item.context_handle,
            source_path=path,
            exact_text=text,
            occurrence=1,
            note=note,
            expected_draft_revision=linked.draft_revision,
        ),
    )
    revision_id = f"rev_{source}"
    publication.publish_artifact_revision(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=db_path.parent / "artifacts",
        build_request=lambda _: publication.MemoryPublicationRequest(
            source=source,
            source_record_id=record_id,
            draft=linked,
            body_kind="artifact_tree",
            revision_id=revision_id,
            evidence_edges=(
                MemoryEvidenceEdge(
                    USER, revision_id, target.revision_id, "source_action"
                ),
            ),
            intent_kind="fact" if source == "fact" else "agent_experience",
            domain_payload_json="{}",
        ),
        write_domain_projection=lambda _connection, _revision: None,
    )


def test_search_copies_go_and_learned_memory_stays_valid(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    artifact_root = tmp_path / "artifacts"
    _seed(
        db_path,
        _suggestion("sug-1")
        + _action("act-1", suggestion_id="sug-1")
        + f"""INSERT INTO agent_facts(fact_id,user_id,status,facts_profile_brief,
                prompt_name,prompt_version) VALUES ('fact-1','{USER}','success','brief',
                'p','1');""",
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            copies = {
                source: domain_registration.register_inline_domain_memory(
                    connection=connection,
                    user_id=USER,
                    source=source,
                    source_record_id=record_id,
                    content=f"Zebracopy {source}.",
                )
                for source, record_id in (
                    ("action", "act-1"),
                    ("suggestion", "sug-1"),
                    ("action_file_read", "inv-act-1"),
                )
            }
    lesson = experience.AgentExperienceContent.model_validate_json(
        '{"experience_id":"lesson-0001","scope":{"kind":"user","key":null},'
        '"applies_when":"Testing","observed_approach":"Used uv","outcome":"effective",'
        '"next_time_rule":"Use uv","observed_result":"Passed",'
        '"supersedes_experience_id":null}'
    )
    entry = experience.experience_entry_path("lesson-0001")
    lesson_text = experience.render_agent_experience_markdown(lesson)
    evidence = "- Evidence: Action act-1"
    _publish_linked(
        db_path,
        source="agent_experience",
        record_id=USER,
        documents=(
            MemoryDocument(
                experience.AGENT_EXPERIENCE_INDEX_PATH,
                experience.render_agent_experience_index((lesson,)),
            ),
            MemoryDocument(entry, f"{lesson_text}{evidence}\n"),
        ),
        anchor=(entry, evidence, "source action"),
        target=copies["action"],
    )
    fact_text = "# Facts\n- Prefers tea.\n"
    _publish_linked(
        db_path,
        source="fact",
        record_id="fact-1",
        documents=(MemoryDocument("facts/index.md", fact_text),),
        anchor=("facts/index.md", "Prefers tea.", "suggested"),
        target=copies["suggestion"],
    )

    _delete(db_path, "conversation", "act-1")

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        assert tuple(
            connection.execute(
                """SELECT
                  (SELECT COUNT(*) FROM memory_nodes
                     WHERE source_type IN ('action', 'suggestion', 'action_file_read')),
                  (SELECT COUNT(*) FROM memory_revisions WHERE revision_id NOT IN
                     ('rev_fact', 'rev_agent_experience')),
                  (SELECT COUNT(*) FROM memory_fragments_fts
                     WHERE memory_fragments_fts MATCH 'Zebracopy'),
                  (SELECT COUNT(*) FROM memory_links),
                  (SELECT COUNT(*) FROM memory_evidence_edges),
                  (SELECT COUNT(*) FROM pragma_foreign_key_check)"""
            ).fetchone()
        ) == (0, 0, 0, 0, 0, 0)
        drafts = {
            source: artifact_workspace.prepare_artifact_draft(
                connection=connection,
                artifact_root=artifact_root,
                user_id=USER,
                source=source,
                source_record_id=record_id,
                initial_documents=(),
            )
            for source, record_id in (("agent_experience", USER), ("fact", "fact-1"))
        }
        # A Memory run opens both: the lesson loses only the deleted Action's evidence.
        agent_experience_delta.parse_agent_experience_tree(
            drafts["agent_experience"].documents
        )
        agent_experience_validation.validate_agent_experience_evidence_links(
            connection=connection, draft=drafts["agent_experience"]
        )
        assert drafts["agent_experience"].documents[0] == MemoryDocument(
            entry, lesson_text
        )
        assert drafts["fact"].documents == (
            MemoryDocument("facts/index.md", fact_text),
        )

    # The periodic repair publishes each learned head without the lost references.
    assert reconciler.reconcile_memory_catalog(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        scan_limit=100,
    ) == (2, 2)
    with closing(sqlite3.connect(db_path)) as connection:
        heads = connection.execute(
            """SELECT nodes.source_type, nodes.integrity, fragments.content_text
               FROM memory_nodes AS nodes JOIN memory_fragments AS fragments
                 ON fragments.user_id = nodes.user_id
                AND fragments.revision_id = nodes.current_revision_id
               WHERE fragments.source_path IN (?, 'facts/index.md')
                 AND fragments.block_kind = 'document_root'
               ORDER BY nodes.source_type""",
            (entry,),
        ).fetchall()
    assert heads == [
        ("agent_experience", "healthy", lesson_text),
        ("fact", "healthy", fact_text),
    ]
