"""Consolidation of the oversized files a Memory update just published."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.memory_agent import MemoryConsolidationRequest
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs.memory_update import execute_memory_update_job
from pantaray_agents.tasks.memory_consolidation import (
    MEMORY_FILE_CONSOLIDATION_THRESHOLD_CHARS,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ToolCallEnvelope,
)

from .test_memory_update_job_run import (
    _bootstrap,
    _builder,
    _claimed_payload,
    _ScriptedMemoryAgent,
)

OVERSIZED = "# Tooling\n\n" + "- An event that happened.\n" * (
    MEMORY_FILE_CONSOLIDATION_THRESHOLD_CHARS // 20
)
CONSOLIDATED = "# Tooling\n\n- The current state.\n"


class _ConsolidatingAgent(_ScriptedMemoryAgent):
    """Publishes the given files, then answers each consolidation with `rewrite`."""

    def __init__(
        self, edits: tuple[tuple[str, str], ...], rewrite: str | None | Exception
    ) -> None:
        super().__init__(edits)
        self._rewrite = rewrite
        self.requests: list[MemoryConsolidationRequest] = []

    async def consolidate(
        self,
        request: MemoryConsolidationRequest,
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        tool_result_directory_fd: int,
    ) -> None:
        del tool_result_directory_fd
        self.requests.append(request)
        if isinstance(self._rewrite, Exception):
            raise self._rewrite
        if self._rewrite is None:
            return
        (rewrite,) = tool_definitions
        args: dict[str, JSONValue] = {"text": self._rewrite}
        result = await rewrite.execute(
            ReactToolCall(
                tool_name=rewrite.name,
                tool_args=args,
                tool_call_envelope=ToolCallEnvelope(
                    tool_id=rewrite.name, reason=None, args=args
                ),
            ),
            1,
        )
        assert result.status == "success", result.output


def _published_fact_files(runtime: LocalMemoryFileEditorRuntime) -> dict[str, str]:
    with sqlite3.connect(runtime.db_path) as connection:
        (root,) = connection.execute(
            """
            SELECT revisions.artifact_root_path
            FROM memory_nodes AS nodes
            JOIN memory_revisions AS revisions
              ON revisions.revision_id = nodes.current_revision_id
            WHERE nodes.source_type = 'fact'
            """
        ).fetchone()
    base = runtime.artifact_root / str(root)
    return {
        str(path.relative_to(base)): path.read_text(encoding="utf-8")
        for path in base.rglob("*.md")
    }


def _fact_revision_count(runtime: LocalMemoryFileEditorRuntime) -> int:
    with sqlite3.connect(runtime.db_path) as connection:
        (count,) = connection.execute(
            """
            SELECT COUNT(*) FROM memory_revisions AS revisions
            JOIN memory_nodes AS nodes ON nodes.node_id = revisions.node_id
            WHERE nodes.source_type = 'fact'
            """
        ).fetchone()
    return int(count)


@pytest.mark.asyncio
async def test_only_a_changed_file_over_the_limit_is_consolidated(
    tmp_path: Path,
) -> None:
    runtime = _bootstrap(tmp_path)
    payload = _claimed_payload(runtime)
    agent = _ConsolidatingAgent(
        (
            ("facts/tooling.md", OVERSIZED),
            ("facts/people.md", "# People\n\n- Works alone.\n"),
            ("insights/todos.md", "# TODO\n\n" + "- Follow up.\n" * 2_000),
        ),
        rewrite=CONSOLIDATED,
    )

    await execute_memory_update_job(
        payload=payload, runtime=runtime, build_agent=_builder(agent)
    )

    assert [(item.path, item.content) for item in agent.requests] == [
        ("facts/tooling.md", OVERSIZED)
    ]
    assert _published_fact_files(runtime) == {
        "facts/index.md": "",
        "facts/people.md": "# People\n\n- Works alone.\n",
        "facts/tooling.md": CONSOLIDATED,
    }
    # The update's revision stays in history under the consolidated one.
    assert _fact_revision_count(runtime) == 2


@pytest.mark.parametrize(
    "rewrite",
    [
        RuntimeError("the model stopped at its turn limit"),
        None,
        OVERSIZED + "- Longer than before.\n",
    ],
    ids=["failed", "no_rewrite", "grew"],
)
@pytest.mark.asyncio
async def test_a_consolidation_that_does_not_shrink_its_file_publishes_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    rewrite: str | None | Exception,
) -> None:
    runtime = _bootstrap(tmp_path)
    payload = _claimed_payload(runtime)
    agent = _ConsolidatingAgent((("facts/tooling.md", OVERSIZED),), rewrite=rewrite)
    caplog.set_level(logging.ERROR)

    await execute_memory_update_job(
        payload=payload, runtime=runtime, build_agent=_builder(agent)
    )

    assert _published_fact_files(runtime)["facts/tooling.md"] == OVERSIZED
    assert _fact_revision_count(runtime) == 1
    failures = [
        json.loads(record.getMessage())
        for record in caplog.records
        if "MEMORY_CONSOLIDATION_FAILED" in record.getMessage()
    ]
    assert [(item["path"], item["job_id"]) for item in failures] == [
        ("facts/tooling.md", payload["job_id"])
    ]
