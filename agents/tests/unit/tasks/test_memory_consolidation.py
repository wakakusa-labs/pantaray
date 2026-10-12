"""Consolidation of the oversized files a Memory update just published."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.memory_agent import MemoryConsolidationRequest
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
    AgentExperienceContent,
    AgentExperienceScope,
    experience_entry_path,
    parse_agent_experience_markdown,
    render_agent_experience_markdown,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    consolidated_experience_ids,
    derived_experience_ids,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks import memory_consolidation
from pantaray_agents.tasks.experience_consolidation import (
    MERGE_EXPERIENCES_TOOL_NAME,
)
from pantaray_agents.tasks.internal_jobs.memory_update import execute_memory_update_job
from pantaray_agents.tasks.memory_consolidation import (
    EXPERIENCE_CONSOLIDATION_PROMPT_NAME,
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


def _published_fact_files(
    runtime: LocalMemoryFileEditorRuntime, source: MemorySource = "fact"
) -> dict[str, str]:
    with sqlite3.connect(runtime.db_path) as connection:
        (root,) = connection.execute(
            """
            SELECT revisions.artifact_root_path
            FROM memory_nodes AS nodes
            JOIN memory_revisions AS revisions
              ON revisions.revision_id = nodes.current_revision_id
            WHERE nodes.source_type = ? AND nodes.lifecycle = 'active'
            """,
            (source,),
        ).fetchone()
    base = runtime.artifact_root / str(root)
    return {
        str(path.relative_to(base)): path.read_text(encoding="utf-8")
        for path in base.rglob("*.md")
    }


def _fact_revision_count(
    runtime: LocalMemoryFileEditorRuntime, source: MemorySource = "fact"
) -> int:
    with sqlite3.connect(runtime.db_path) as connection:
        (count,) = connection.execute(
            """
            SELECT COUNT(*) FROM memory_revisions AS revisions
            JOIN memory_nodes AS nodes ON nodes.node_id = revisions.node_id
            WHERE nodes.source_type = ?
            """,
            (source,),
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


def _lesson(experience_id: str, applies_when: str, *, key: str | None = None) -> str:
    return render_agent_experience_markdown(
        AgentExperienceContent(
            experience_id=experience_id,
            scope=AgentExperienceScope(
                kind="user" if key is None else "workspace", key=key
            ),
            applies_when=applies_when,
            observed_approach="Ran the suite with the system interpreter.",
            outcome="ineffective",
            next_time_rule="Run the suite through uv run.",
            observed_result="Imports failed until uv run was used.",
        )
    )


_MERGED: dict[str, JSONValue] = {
    "applies_when": "Running the Python test suite of a uv-managed repository.",
    "observed_approach": "Ran the suite with the system interpreter, twice.",
    "outcome": "ineffective",
    "next_time_rule": "Run the suite through uv run.",
    "observed_result": "Imports failed until uv run was used.",
}


class _MergingAgent(_ScriptedMemoryAgent):
    """Publishes the given lessons, then answers the consolidation with `merges`."""

    def __init__(
        self,
        edits: tuple[tuple[str, str], ...],
        merges: list[JSONValue] | Exception,
    ) -> None:
        super().__init__(edits)
        self._merges = merges
        self.requests: list[MemoryConsolidationRequest] = []
        self.results: list[JSONValue] = []

    async def consolidate(
        self,
        request: MemoryConsolidationRequest,
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        tool_result_directory_fd: int,
    ) -> None:
        del tool_result_directory_fd
        self.requests.append(request)
        if isinstance(self._merges, Exception):
            raise self._merges
        (merge,) = tool_definitions
        assert merge.name == MERGE_EXPERIENCES_TOOL_NAME
        args: dict[str, JSONValue] = {"merges": self._merges}
        result = await merge.execute(
            ReactToolCall(
                tool_name=merge.name,
                tool_args=args,
                tool_call_envelope=ToolCallEnvelope(
                    tool_id=merge.name, reason=None, args=args
                ),
            ),
            1,
        )
        self.results.append(result.output)


def _three_lessons(job_id: str) -> tuple[tuple[str, str], ...]:
    first, second, third, _ = derived_experience_ids(job_id)
    return (
        (experience_entry_path(first), _lesson(first, "Running pytest with uv.")),
        (experience_entry_path(second), _lesson(second, "Running tests via uv.")),
        (
            experience_entry_path(third),
            _lesson(third, "Running tests via uv.", key="/work/other"),
        ),
    )


@pytest.mark.asyncio
async def test_an_index_over_the_limit_merges_lessons_into_one_that_supersedes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _bootstrap(tmp_path)
    payload = _claimed_payload(runtime)
    first, second, third, _ = derived_experience_ids(payload["job_id"])
    monkeypatch.setattr(
        memory_consolidation, "EXPERIENCE_INDEX_CONSOLIDATION_THRESHOLD_CHARS", 300
    )
    agent = _MergingAgent(
        _three_lessons(payload["job_id"]),
        merges=[
            # Different Scopes stay apart; the valid merge still lands.
            {"supersedes": [second, third], **_MERGED},
            {"supersedes": [first, second], **_MERGED},
            {"supersedes": [first, second], **_MERGED},
        ],
    )

    await execute_memory_update_job(
        payload=payload, runtime=runtime, build_agent=_builder(agent)
    )

    (request,) = agent.requests
    assert request.prompt_name == EXPERIENCE_CONSOLIDATION_PROMPT_NAME
    assert request.content.count("<entry written=") == 3
    rejected = json.loads(json.dumps(agent.results[0]))["rejected"]
    assert [item["merge"] for item in rejected] == [0, 2]
    merged_id = consolidated_experience_ids(payload["job_id"])[0]
    files = _published_fact_files(runtime, "agent_experience")
    assert sorted(files) == sorted(
        (
            AGENT_EXPERIENCE_INDEX_PATH,
            experience_entry_path(third),
            experience_entry_path(merged_id),
        )
    )
    merged = parse_agent_experience_markdown(
        files[experience_entry_path(merged_id)], expected_experience_id=merged_id
    )
    assert merged.supersedes_experience_ids == (first, second)
    assert merged.scope == AgentExperienceScope(kind="user", key=None)
    index = files[AGENT_EXPERIENCE_INDEX_PATH]
    assert merged_id in index and third in index
    assert first not in index and second not in index
    assert _fact_revision_count(runtime, "agent_experience") == 2


@pytest.mark.parametrize(
    "merges",
    [RuntimeError("the model stopped at its turn limit"), []],
    ids=["failed", "no_merge"],
)
@pytest.mark.asyncio
async def test_an_experience_consolidation_that_merges_nothing_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    merges: list[JSONValue] | Exception,
) -> None:
    runtime = _bootstrap(tmp_path)
    payload = _claimed_payload(runtime)
    monkeypatch.setattr(
        memory_consolidation, "EXPERIENCE_INDEX_CONSOLIDATION_THRESHOLD_CHARS", 300
    )
    agent = _MergingAgent(_three_lessons(payload["job_id"]), merges=merges)

    await execute_memory_update_job(
        payload=payload, runtime=runtime, build_agent=_builder(agent)
    )

    assert len(agent.requests) == 1
    assert len(_published_fact_files(runtime, "agent_experience")) == 4
    assert _fact_revision_count(runtime, "agent_experience") == 1


@pytest.mark.asyncio
async def test_an_index_under_the_limit_is_not_consolidated(tmp_path: Path) -> None:
    runtime = _bootstrap(tmp_path)
    payload = _claimed_payload(runtime)
    agent = _MergingAgent(_three_lessons(payload["job_id"]), merges=[])

    await execute_memory_update_job(
        payload=payload, runtime=runtime, build_agent=_builder(agent)
    )

    assert agent.requests == []
