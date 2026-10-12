"""apply_patch keeps the refs a model rewrites and names the ones it cannot keep."""

from __future__ import annotations

import pytest

from pantaray_agents.local_runtime.memory_catalog.draft import (
    create_memory_draft,
    validate_memory_draft_checkpoint,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    DraftLink,
    MemoryDocument,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    MemoryDraftToolSession,
    build_local_memory_file_tools,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ReactToolResult,
    ToolCallEnvelope,
)
from pantaray_agents.tools.memory.retrieval import MemoryContextSession

_POLICY = EditablePathPolicy(("facts/**",))
_SQLITE = '- Chose SQLite. [[ref:ref_a note:"why \\"local\\" won"]]'
_WAL = '- Uses WAL. [[ref:ref_b note:"bench"]]'


def _session() -> MemoryDraftToolSession:
    def link(ref_id: str, path: str, note: str) -> DraftLink:
        return DraftLink(
            local_ref_id=ref_id,
            target_fragment_id=f"target-{ref_id}",
            source_path=path,
            source_anchor_text="- anchor",
            source_anchor_occurrence=1,
            reference_note=note,
            created_at="2026-01-01",
            state="carried",
        )

    return MemoryDraftToolSession(
        editable_policy=_POLICY,
        draft=create_memory_draft(
            user_id="user-1",
            owner_node_id="fact-1",
            base_revision_id=None,
            documents=(
                MemoryDocument("facts/a.md", f"# A\n\n{_SQLITE}\n{_WAL}\n"),
                MemoryDocument(
                    "facts/b.md", '# B\n\n- Other. [[ref:ref_c note:"c"]]\n'
                ),
            ),
            carried_links=(
                link("ref_a", "facts/a.md", 'why "local" won'),
                link("ref_b", "facts/a.md", "bench"),
                link("ref_c", "facts/b.md", "c"),
            ),
        ),
        memory_context=MemoryContextSession(user_id="user-1", run_id="run-1"),
    )


async def _merge_lines(
    session: MemoryDraftToolSession, added: tuple[str, ...]
) -> ReactToolResult:
    registry = ReactToolRegistry(
        build_local_memory_file_tools(editable_policy=_POLICY, memory_session=session)
    )
    await registry.execute(
        _call("read_file", {"root": "memory_draft", "path": "facts/a.md"}), 1
    )
    lines: list[JSONValue] = [
        {"op": "remove", "text": _SQLITE},
        {"op": "remove", "text": _WAL},
        *({"op": "add", "text": text} for text in added),
    ]
    return await registry.execute(
        _call("apply_patch", {"path": "facts/a.md", "chunks": [{"lines": lines}]}), 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "merged",
    [
        '- SQLite with WAL. [[ref:ref_a note:"why local won"]] '
        '[[ref:ref_b note:"benchmark"]]',
        "- SQLite with WAL. [[ref:ref_a]] [[ref:ref_b]]",
    ],
    ids=["retyped_notes", "omitted_notes"],
)
async def test_a_kept_tag_keeps_its_stored_note(merged: str) -> None:
    session = _session()
    links = session.draft.links

    result = await _merge_lines(session, (merged,))

    assert result.status == "success"
    assert session.draft.documents[0].content == (
        '# A\n\n- SQLite with WAL. [[ref:ref_a note:"why \\"local\\" won"]] '
        '[[ref:ref_b note:"bench"]]\n'
    )
    assert session.draft.links == links
    validate_memory_draft_checkpoint(session.draft)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("added", "named"),
    [
        (
            ("- SQLite. [[ref:ref_a]]", "- Again SQLite. [[ref:ref_a]] [[ref:ref_b]]"),
            "ref IDs used more than once: ref_a",
        ),
        (
            ('- SQLite. [[ref:ref_a]] [[ref:ref_new note:"made up"]] [[ref:ref_b]]',),
            "ref IDs not in this file: ref_new",
        ),
        (
            ('- SQLite. [[ref:ref_a]] [[ref:ref_b]] [[ref:ref_c note:"c"]]',),
            "ref IDs not in this file: ref_c",
        ),
    ],
    ids=["repeated", "invented", "from_another_file"],
)
async def test_a_tag_that_cannot_be_kept_is_named(
    added: tuple[str, ...], named: str
) -> None:
    session = _session()
    before = session.draft

    result = await _merge_lines(session, added)

    assert result.status == "error"
    assert result.output["error_code"] == "MEMORY_LINK_INVALID"
    assert result.output["message"] == named
    assert session.draft == before


def _call(tool_name: str, args: dict[str, JSONValue]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=tool_name, reason=None, args=args),
    )
