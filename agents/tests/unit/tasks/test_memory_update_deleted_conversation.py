from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog import draft as memory_draft
from pantaray_agents.local_runtime.memory_catalog import repository
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    build_memory_context_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryLinkValidationError,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.agent_experience import (
    ActionTurnWindow,
    AgentExperienceActionHistoryTools,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.memory_update_context import prepare_memory_update_run
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

from .test_memory_update_publication import (
    BUSY_TIMEOUT_MS,
    NOW,
    USER_ID,
    _bootstrap,
    _claim,
    _payload,
    _publish,
    _rows,
    _runtime,
)


@pytest.mark.asyncio
async def test_a_deleted_action_is_skipped_and_its_history_reads_as_not_found(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    payload["action_terminals"] = [
        {
            "source_id": "action-deleted",
            "action_id": "action-deleted",
            "action_completed_at": NOW,
            "turn_start_step_number": 1,
            "turn_end_step_number": 2,
            "action_prompt_name": "action/executing",
            "action_prompt_version": "1.0",
        }
    ]
    _claim(runtime, payload)

    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )

    assert prepared.context.action_turns == ""
    assert prepared.has_evidence  # the short-term Insight is still there
    # A history tool reached after the Action was deleted mid-run answers normally.
    tools = AgentExperienceActionHistoryTools(
        db_path=runtime.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        turns=(ActionTurnWindow("action-deleted", 1, 2),),
    ).definitions()
    outputs = []
    for tool, args in zip(
        tools, ({}, {"query": "uv"}, {"refs": ["S-1-USER"]}), strict=True
    ):
        call_args: dict[str, JSONValue] = {"action_id": "action-deleted", **args}
        envelope = ToolCallEnvelope(tool_id=tool.name, reason=None, args=call_args)
        result = await tool.execute(
            ReactToolCall(tool.name, call_args, tool_call_envelope=envelope), 1
        )
        assert isinstance(result.output, dict)
        outputs.append(
            result.output.get("steps", result.output.get("matches"))
            if result.status == "success"
            else result.output.get("error_code")
        )
    assert outputs == [[], [], "ACTION_HISTORY_NOT_FOUND"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kept_target", ["active", "tombstoned"])
async def test_refs_to_a_copy_deleted_mid_run_are_dropped_and_the_rest_published(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kept_target: str
) -> None:
    runtime = _runtime(tmp_path)
    _bootstrap(runtime)
    payload = _payload()
    _claim(runtime, payload)
    with open_memory_catalog_connection(
        db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            copies = [
                register_inline_domain_memory(
                    connection=connection,
                    user_id=USER_ID,
                    source=source,
                    source_record_id=record_id,
                    content=f"Copy of {record_id}.",
                )
                for source, record_id in (
                    ("action", "act-1"),
                    ("activity_log", "log-1"),
                )
            ]
        visible = tuple(
            (
                repository.load_node(
                    connection=connection, user_id=USER_ID, node_id=copy.node_id
                ),
                repository.list_revision_fragments(
                    connection=connection, user_id=USER_ID, revision_id=copy.revision_id
                )[-1],
                "evidence",
            )
            for copy in copies
        )
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )
        route = next(item for item in prepared.router.routes if item.source == "fact")
        draft = memory_draft.replace_draft_documents(
            draft=route.session.draft,
            documents=(
                memory_draft.MemoryDocument(
                    "facts/index.md", "# Facts\n- Prefers uv.\n- Drinks tea.\n"
                ),
            ),
        )
        epoch = build_memory_context_epoch(
            run_id="run-1",
            user_id=USER_ID,
            visible=visible,  # type: ignore[arg-type]
        )
        for index, anchor in enumerate(("Prefers uv.", "Drinks tea.")):
            draft = memory_draft.link_memory(
                draft=draft,
                command=memory_draft.seal_link_command(
                    epoch=epoch,
                    draft=draft,
                    tool_invocation_id=f"link-{index}",
                    target_handle=epoch.items[index].item.context_handle,
                    source_path="facts/index.md",
                    exact_text=anchor,
                    occurrence=1,
                    note="observed",
                    expected_draft_revision=draft.draft_revision,
                ),
            )
        route.session.draft = draft
        kept_ref = draft.links[1].local_ref_id
        # The conversation behind the Action copy is deleted while the run works.
        with (
            open_memory_catalog_connection(
                db_path=runtime.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
            ) as connection,
            connection,
        ):
            connection.execute(
                "DELETE FROM memory_revisions WHERE node_id = ?", (copies[0].node_id,)
            )
            connection.execute(
                "DELETE FROM memory_nodes WHERE node_id = ?", (copies[0].node_id,)
            )
            connection.execute(
                "UPDATE memory_nodes SET lifecycle = ? WHERE node_id = ?",
                (kept_target, copies[1].node_id),
            )

        caplog.set_level(logging.INFO)
        if kept_target == "tombstoned":
            # Only a missing target is dropped; any other invalid ref still fails.
            with pytest.raises(MemoryLinkValidationError):
                await _publish(runtime, payload, prepared)
            return
        await _publish(runtime, payload, prepared)

    assert _rows(
        runtime,
        """SELECT fragments.content_text, links.local_ref_id
           FROM memory_nodes AS nodes
           JOIN memory_fragments AS fragments
             ON fragments.revision_id = nodes.current_revision_id
            AND fragments.block_kind = 'document_root'
           JOIN memory_links AS links ON links.source_revision_id = nodes.current_revision_id
           WHERE nodes.source_type = 'fact'""",
    ) == [
        (
            f'# Facts\n- Prefers uv.\n- Drinks tea. [[ref:{kept_ref} note:"observed"]]\n',
            kept_ref,
        )
    ]
    assert "Memory update dropped 1 refs to deleted memory" in caplog.messages
