from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.agents.artifact_react import ReactLoopPolicy, ReactLoopStep
from pantaray_agents.agents.artifact_react.transcript import (
    build_prompt_with_transcript,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.agents.memory_file_editor.runner import (
    MemoryFileEditorRunInput,
    run_memory_file_editor,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.models import (
    DraftLink,
    MemoryDocument,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    EditablePathPolicy,
    PatchChunk,
    PatchLine,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    MemoryDraftToolSession,
    build_local_memory_file_tools,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.read_gate import (
    build_read_snapshot,
    patch_uses_visible_lines,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ToolCallEnvelope,
)
from pantaray_agents.tools.memory.retrieval import (
    MemoryContextSession,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, OpenAiToolContinuation


@pytest.mark.asyncio
async def test_patch_reference_rejection_reaches_next_turn_and_can_be_corrected(
    tmp_path: Path,
) -> None:
    session, tools = _build_tools("- current\n")
    calls = iter(
        (
            _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}),
            _patch_call("- current", '- changed [[ref:ref_test note:"evidence"]]'),
            _patch_call("- current", "- corrected"),
            _tool_call("completed", {}),
        )
    )
    recorded: list[ReactLoopStep] = []
    prompts: list[str] = []

    async def call_llm(prompt, _tools, _continuation, _result):
        prompts.append(prompt)
        if len(prompts) == 3:
            assert "MEMORY_LINK_INVALID" in prompt
            assert "Use link_memory to add" in prompt
            assert _document_text(session) == "- current\n"
        call = next(calls)
        return LlmToolCallTurn(
            calls=(
                LlmToolCall(
                    call_id=str(len(prompts)),
                    name=call.tool_name,
                    arguments=call.tool_args,
                ),
            ),
            continuation=OpenAiToolContinuation(
                provider="openai",
                history_items=[{"role": "user", "content": "prompt"}],
            ),
        )

    async def record_step(step: ReactLoopStep) -> None:
        recorded.append(step)

    with MemoryRunWorkspaceScope() as workspace:
        workspace.create(artifact_root=tmp_path, user_id="user-1", run_id="run-1")
        result = await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-1",
                tool_result_directory_fd=workspace.require_tool_results_fd(),
                tool_definitions=tools,
                build_prompt=lambda results, error: build_prompt_with_transcript(
                    initial_prompt="Update memory",
                    tool_results=results,
                    last_error=error,
                ),
                call_llm=call_llm,
                record_step=record_step,
                policy=ReactLoopPolicy(max_llm_turns=5, max_tool_calls=4),
            )
        )

    assert result.loop_result.status == "success"
    assert _document_text(session) == "- corrected\n"
    rejected = next(step for step in recorded if step.status == "error")
    assert rejected.tool_output["details"]["path"] == "facts.md"


@pytest.mark.asyncio
async def test_apply_patch_without_prior_read_returns_needs_read_then_applies() -> None:
    session, tools = _build_tools("- current\n")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")
    call = _patch_call("- current", "- updated")

    first = await apply_patch.execute(call, 1)
    second = await apply_patch.execute(call, 2)

    assert first.output["status"] == "needs_read"
    assert first.output["patch_applied"] is False
    assert _document_text(session) == "- updated\n"
    assert second.output["status"] == "success"


@pytest.mark.asyncio
async def test_apply_patch_success_invalidates_snapshot() -> None:
    session, tools = _build_tools("- current\n")
    read_file = next(tool for tool in tools if tool.name == "read_file")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read_file.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )

    first = await apply_patch.execute(_patch_call("- current", "- updated"), 2)
    second = await apply_patch.execute(_patch_call("- updated", "- final"), 3)

    assert first.output["status"] == "success"
    assert second.output["status"] == "needs_read"
    assert _document_text(session) == "- updated\n"


@pytest.mark.asyncio
async def test_checkpoint_change_invalidates_snapshot() -> None:
    session, tools = _build_tools("- current\n")
    read_file = next(tool for tool in tools if tool.name == "read_file")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read_file.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    session.replace_document(
        expected_revision=session.draft.draft_revision,
        path="facts.md",
        content="- concurrent\n",
        expected_text="- current\n",
    )

    result = await apply_patch.execute(_patch_call("- concurrent", "- updated"), 2)

    assert result.output["status"] == "needs_read"
    assert result.output["text"] == "- concurrent\n"
    assert _document_text(session) == "- concurrent\n"


@pytest.mark.asyncio
async def test_search_does_not_satisfy_apply_patch_read_gate() -> None:
    session, tools = _build_tools("- current\n")
    search = next(tool for tool in tools if tool.name == "search_files")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")

    await search.execute(
        _tool_call("search_files", {"root": "memory_draft", "query": "current"}),
        1,
    )
    result = await apply_patch.execute(_patch_call("- current", "- updated"), 2)

    assert result.output["status"] == "needs_read"
    assert _document_text(session) == "- current\n"


@pytest.mark.asyncio
async def test_apply_patch_pages_through_target_windows() -> None:
    target_indexes = (99, 299, 499, 699, 899, 1099)
    lines = [f"- item {index} {'x' * 180}" for index in range(1, 1201)]
    for target_index in target_indexes:
        lines[target_index] = f"- target {target_index} old"
    session, tools = _build_tools("\n".join(lines) + "\n")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")
    chunks = [
        {
            "lines": [
                {"op": "remove", "text": f"- target {index} old"},
                {"op": "add", "text": f"- target {index} new"},
            ]
        }
        for index in target_indexes
    ]
    call = _tool_call("apply_patch", {"path": "facts.md", "chunks": chunks})

    first = await apply_patch.execute(call, 1)
    second = await apply_patch.execute(call, 2)
    third = await apply_patch.execute(call, 3)

    assert first.output["status"] == second.output["status"] == "needs_read"
    assert first.output["remaining_chunk_count"] > 0
    assert second.output["remaining_chunk_count"] == 0
    assert third.output["status"] == "success"
    for index in target_indexes:
        assert f"- target {index} new\n" in _document_text(session)


@pytest.mark.asyncio
async def test_apply_patch_rejects_target_line_larger_than_read_limit() -> None:
    oversized_line = "x" * 40_001
    session, tools = _build_tools(f"{oversized_line}\n")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")

    result = await apply_patch.execute(_patch_call(oversized_line, "short"), 1)

    assert result.status == "error"
    assert result.output["error_code"] == "READ_WINDOW_TOO_LARGE"
    assert _document_text(session) == f"{oversized_line}\n"


def test_partial_snapshot_rejects_lines_split_across_windows() -> None:
    snapshot = build_read_snapshot(
        path="facts.md",
        text="",
        visible_texts=("section A\nold 1\n", "old 2\nsection B\n"),
        full_file=False,
        step_number=1,
    )
    chunk = PatchChunk(
        lines=(
            PatchLine(op="context", text="section A"),
            PatchLine(op="remove", text="old 1"),
            PatchLine(op="remove", text="old 2"),
            PatchLine(op="context", text="section B"),
            PatchLine(op="add", text="new"),
        )
    )

    assert not patch_uses_visible_lines(chunks=(chunk,), snapshot=snapshot)


@pytest.mark.parametrize("full_file, expected", ((False, False), (True, True)))
def test_add_only_patch_requires_full_file_snapshot(
    full_file: bool, expected: bool
) -> None:
    snapshot = build_read_snapshot(
        path="facts.md",
        text="nearby\n",
        visible_texts=("nearby\n",),
        full_file=full_file,
        step_number=1,
    )
    chunk = PatchChunk(lines=(PatchLine(op="add", text="- inserted"),))

    assert patch_uses_visible_lines(chunks=(chunk,), snapshot=snapshot) is expected


def _build_tools(
    text: str,
    *,
    carried_links: tuple[DraftLink, ...] = (),
) -> tuple[MemoryDraftToolSession, tuple[ReactToolDefinition, ...]]:
    policy = EditablePathPolicy(("facts.md",))
    session = MemoryDraftToolSession(
        editable_policy=policy,
        draft=create_memory_draft(
            user_id="user-1",
            owner_node_id="fact-1",
            base_revision_id=None,
            documents=(MemoryDocument("facts.md", text),),
            carried_links=carried_links,
        ),
        memory_context=MemoryContextSession(
            user_id="user-1", run_id="run-1", epoch=None
        ),
    )
    return session, build_local_memory_file_tools(
        editable_policy=policy,
        memory_session=session,
    )


def _document_text(session: MemoryDraftToolSession) -> str:
    return session.draft.documents[0].content


@pytest.mark.asyncio
async def test_patch_rejects_change_between_read_gate_and_replacement(
    monkeypatch,
) -> None:
    session, tools = _build_tools("- current\n")
    read = next(tool for tool in tools if tool.name == "read_file")
    patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    original = MemoryDraftToolSession.replace_document

    def concurrent_replace(self, *, path, content, expected_text, expected_revision):
        original(
            self,
            path=path,
            content="- concurrent\n",
            expected_text=expected_text,
            expected_revision=expected_revision,
        )
        original(
            self,
            path=path,
            content=content,
            expected_text=expected_text,
            expected_revision=expected_revision,
        )

    with monkeypatch.context() as race:
        race.setattr(MemoryDraftToolSession, "replace_document", concurrent_replace)
        result = await patch.execute(_patch_call("- current", "- updated"), 2)

    assert result.status == "error"
    assert result.output["error_code"] == "MEMORY_DRAFT_REVISION_STALE"
    assert _document_text(session) == "- concurrent\n"
    retry = await patch.execute(_patch_call("- concurrent", "- updated"), 3)
    assert retry.output["status"] == "needs_read"
    assert _document_text(session) == "- concurrent\n"


@pytest.mark.asyncio
async def test_read_body_and_revision_come_from_one_snapshot(monkeypatch) -> None:
    from pantaray_agents.local_runtime.tooling.memory_file_editor import registry

    session, tools = _build_tools("- current\n")
    read = next(tool for tool in tools if tool.name == "read_file")
    revision = session.draft.draft_revision
    original = registry.read_draft_text_page

    def concurrent_read(**kwargs):
        page = original(**kwargs)
        session.replace_document(
            expected_revision=session.draft.draft_revision,
            path="facts.md",
            content="- concurrent\n",
            expected_text="- current\n",
        )
        return page

    monkeypatch.setattr(registry, "read_draft_text_page", concurrent_read)
    result = await read.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    assert result.output["text"] == "- current\n"
    assert result.output["draft_revision"] == revision
    assert result.output["draft_revision"] != session.draft.draft_revision


def _patch_call(old: str, new: str) -> ReactToolCall:
    return _tool_call(
        "apply_patch",
        {
            "path": "facts.md",
            "chunks": [
                {
                    "lines": [
                        {"op": "remove", "text": old},
                        {"op": "add", "text": new},
                    ]
                }
            ],
        },
    )


def _tool_call(tool_name: str, args: dict[str, object]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id=tool_name,
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_patch_preserves_three_references_while_appending_unrelated_fact() -> (
    None
):
    lines = [
        f'- Existing fact {index}. [[ref:ref_{index} note:"evidence {index}"]]'
        for index in range(1, 4)
    ]
    text = "# Context\n\n" + "\n".join(lines) + "\n\n- Unrelated note.\n"
    links = tuple(
        DraftLink(
            local_ref_id=f"ref_{index}",
            target_fragment_id=f"fragment_{index}",
            source_path="facts.md",
            source_anchor_text=f"- Existing fact {index}.",
            source_anchor_occurrence=1,
            reference_note=f"evidence {index}",
            created_at="2026-09-12T00:00:00Z",
            state="carried",
        )
        for index in range(1, 4)
    )
    session, tools = _build_tools(text, carried_links=links)
    read_file = next(tool for tool in tools if tool.name == "read_file")
    apply_patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read_file.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    result = await apply_patch.execute(
        _tool_call(
            "apply_patch",
            {
                "path": "facts.md",
                "chunks": [
                    {
                        "lines": [
                            {"op": "context", "text": "- Unrelated note."},
                            {"op": "add", "text": "- A new unrelated fact."},
                        ]
                    }
                ],
            },
        ),
        3,
    )
    assert result.status == "success"
    assert result.output["status"] == "success"
    assert _document_text(session) == text + "- A new unrelated fact.\n"

    assert session.draft.links == links


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old, new, removed",
    [
        ("# Context", "## Revised context", False),
        (
            '- Claim. [[ref:ref_1 note:"evidence"]]',
            '- Revised claim. [[ref:ref_1 note:"evidence"]]',
            False,
        ),
        ('- Claim. [[ref:ref_1 note:"evidence"]]', "- Claim.", True),
        (
            '- Claim. [[ref:ref_1 note:"evidence"]]',
            '- Claim. [[ note:"evidence"]]',
            True,
        ),
    ],
)
async def test_patch_updates_reference_text_and_reports_unlink(
    old: str, new: str, removed: bool
) -> None:
    link = DraftLink(
        local_ref_id="ref_1",
        target_fragment_id="fragment_1",
        source_path="facts.md",
        source_anchor_text="- Claim.",
        source_anchor_occurrence=1,
        reference_note="evidence",
        created_at="2026-09-13T00:00:00Z",
        state="carried",
    )
    text = '# Context\n- Claim. [[ref:ref_1 note:"evidence"]]\n'
    session, tools = _build_tools(text, carried_links=(link,))
    read = next(tool for tool in tools if tool.name == "read_file")
    patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    result = await patch.execute(_patch_call(old, new), 2)
    assert result.output["status"] == "success"
    assert _document_text(session) == text.replace(old, new)
    assert session.draft.links[0].target_fragment_id == link.target_fragment_id
    assert session.draft.links[0].state == ("removed" if removed else "carried")
    if removed:
        assert "ref_1" in result.output["diff_summary"]
        assert "unlinked" in result.output["diff_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "markdown",
    [
        "Read [ref:guide](https://example.test/guide).\n",
        "Read [ref:guide][guide].\n\n[guide]: https://example.test/guide\n",
        "Read [[ref]].\n",
        "Read [[ref guide]].\n",
    ],
)
async def test_patch_preserves_ordinary_ref_markdown_links(markdown: str) -> None:
    text = markdown + "- current\n"
    session, tools = _build_tools(text)
    read = next(tool for tool in tools if tool.name == "read_file")
    patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )

    result = await patch.execute(_patch_call("- current", "- updated"), 2)

    assert result.output["status"] == "success"
    assert _document_text(session) == markdown + "- updated\n"
    assert session.draft.links == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        '- Broken [[ref:ref_1 note:"evidence"]',
        '- Broken [ref:ref_1 note:"evidence"]]',
        '- Broken [[refx:ref_1 note:"evidence"]]',
        '- Broken [[refxref_1 note:"evidence"]]',
        '- Broken [[refref_1 note:"evidence"]]',
        '- Broken [[ref ref_1 note:"evidence"]]',
        '- Broken [[ ref:ref_1 note:"evidence"]]',
        '- Broken ((ref:ref_1 note:"evidence"))',
        '- Broken ref:ref_1 note:"evidence"]]',
        '- Unknown [[ref:ref_unknown note:"evidence"]]',
        '- Duplicate [[ref:ref_1 note:"evidence"]] [[ref:ref_1 note:"evidence"]]',
    ],
)
async def test_invalid_reference_patch_keeps_document_and_links(
    replacement: str,
) -> None:
    line = '- Claim. [[ref:ref_1 note:"evidence"]]'
    link = DraftLink(
        local_ref_id="ref_1",
        target_fragment_id="fragment_1",
        source_path="facts.md",
        source_anchor_text="- Claim.",
        source_anchor_occurrence=1,
        reference_note="evidence",
        created_at="2026-09-13T00:00:00Z",
        state="carried",
    )
    session, tools = _build_tools(line + "\n", carried_links=(link,))
    read = next(tool for tool in tools if tool.name == "read_file")
    patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    before = session.draft
    result = await patch.execute(_patch_call(line, replacement), 2)
    assert result.output["error_code"] == "MEMORY_LINK_INVALID"
    assert session.draft == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "literal, replacement",
    [
        ("literal ref_1\n", '- Claim. [[refx:ref_1 note:"evidence"]]'),
        ("literal ref_1; ", '- Claim. [[refxref_1 note:"evidence"]]'),
    ],
)
async def test_deleting_literal_id_cannot_mask_a_broken_tag(
    literal: str, replacement: str
) -> None:
    text = literal + '- Claim. [[ref:ref_1 note:"evidence"]]\n'
    link = DraftLink(
        local_ref_id="ref_1",
        target_fragment_id="fragment_1",
        source_path="facts.md",
        source_anchor_text="- Claim.",
        source_anchor_occurrence=1,
        reference_note="evidence",
        created_at="2026-09-13T00:00:00Z",
        state="carried",
    )
    session, tools = _build_tools(text, carried_links=(link,))
    read = next(tool for tool in tools if tool.name == "read_file")
    patch = next(tool for tool in tools if tool.name == "apply_patch")
    await read.execute(
        _tool_call("read_file", {"root": "memory_draft", "path": "facts.md"}), 1
    )
    before = session.draft
    result = await patch.execute(
        _tool_call(
            "apply_patch",
            {
                "path": "facts.md",
                "chunks": [
                    {
                        "lines": [
                            *(
                                {"op": "remove", "text": line}
                                for line in text.splitlines()
                            ),
                            {"op": "add", "text": replacement},
                        ]
                    }
                ],
            },
        ),
        2,
    )
    assert result.output["error_code"] == "MEMORY_LINK_INVALID"
    assert session.draft == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,expected_code",
    [("missing.md", "PATH_NOT_FOUND"), ("../outside.md", "PATH_DENIED")],
)
async def test_patch_keeps_specific_file_path_error_codes(path, expected_code):
    session, _ = _build_tools("- current\n")
    session.editable_policy = EditablePathPolicy(("*.md",))
    definitions = build_local_memory_file_tools(
        editable_policy=session.editable_policy, memory_session=session
    )
    patch = next(tool for tool in definitions if tool.name == "apply_patch")
    call = _patch_call("- current", "- changed")
    call.tool_args["path"] = path
    result = await patch.execute(call, 1)
    assert result.output["error_code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace_document", "append_document"])
async def test_mutation_returns_its_own_revision_before_another_writer(
    monkeypatch, operation
):
    session, _ = _build_tools("- current\n")
    session.editable_policy = EditablePathPolicy(("*.md",))
    definitions = build_local_memory_file_tools(
        editable_policy=session.editable_policy, memory_session=session
    )
    original = getattr(MemoryDraftToolSession, operation)
    replace_original = MemoryDraftToolSession.replace_document
    captured = []

    def concurrent_writer(self, **kwargs):
        original(self, **kwargs)
        captured.append(self.draft.draft_revision)
        replace_original(
            self,
            path=kwargs["path"],
            content="- concurrent\n",
            expected_text=kwargs["content"],
            expected_revision=self.draft.draft_revision,
        )
        return captured[-1]

    monkeypatch.setattr(MemoryDraftToolSession, operation, concurrent_writer)
    if operation == "replace_document":
        tool = next(item for item in definitions if item.name == "apply_patch")
        call = _patch_call("- current", "- changed")
        await tool.execute(call, 1)
    else:
        tool = next(item for item in definitions if item.name == "write_file")
        call = _tool_call("write_file", {"path": "new.md", "text": "- new\n"})
    result = await tool.execute(call, 2)
    assert result.output["status"] == "success"
    assert result.output["draft_revision"] == captured[0]
    assert result.output["draft_revision"] != session.draft.draft_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace_document", "append_document"])
@pytest.mark.parametrize("routed", [False, True])
async def test_edit_rejects_another_file_changing_after_the_tool_snapshot(
    monkeypatch, operation, routed
):
    from pantaray_agents.local_runtime.memory_catalog.draft import (
        replace_draft_documents,
    )
    from pantaray_agents.local_runtime.tooling.memory_file_editor.memory_draft_router import (
        MemoryDraftRoute,
        MemoryDraftRouter,
    )

    facts, _ = _build_tools("- current\n")
    facts.editable_policy = EditablePathPolicy(("facts/**",))
    facts.draft = replace_draft_documents(
        draft=facts.draft,
        documents=(MemoryDocument("facts/current.md", "- current\n"),),
    )
    peer, _ = _build_tools("- old peer\n")
    peer.editable_policy = EditablePathPolicy(("insights/**",))
    peer.draft = replace_draft_documents(
        draft=peer.draft,
        documents=(MemoryDocument("insights/peer.md", "- old peer\n"),),
    )
    if routed:
        session = MemoryDraftRouter(
            (
                MemoryDraftRoute("fact", "facts", facts),
                MemoryDraftRoute("long_term_insight", "insights", peer),
            )
        )
    else:
        session = facts
        session.editable_policy = EditablePathPolicy(("facts/**", "insights/**"))
        session.draft = replace_draft_documents(
            draft=session.draft,
            documents=(*session.draft.documents, *peer.draft.documents),
        )
        peer = session
    definitions = build_local_memory_file_tools(
        editable_policy=session.editable_policy, memory_session=session
    )
    original = getattr(type(session), operation)

    def concurrent_writer(self, **kwargs):
        peer.draft = replace_draft_documents(
            draft=peer.draft,
            documents=tuple(
                MemoryDocument(item.source_path, "- concurrent peer\n")
                if item.source_path == "insights/peer.md"
                else item
                for item in peer.draft.documents
            ),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(type(session), operation, concurrent_writer)
    if operation == "replace_document":
        tool = next(item for item in definitions if item.name == "apply_patch")
        call = _patch_call("- current", "- changed")
        call.tool_args["path"] = "facts/current.md"
        await tool.execute(call, 1)
    else:
        tool = next(item for item in definitions if item.name == "write_file")
        call = _tool_call("write_file", {"path": "facts/new.md", "text": "- new\n"})
    result = await tool.execute(call, 2)
    assert result.output["status"] == "error"
    assert result.output["error_code"] == "MEMORY_DRAFT_REVISION_STALE"
    assert {item.source_path: item.content for item in session.draft.documents} == {
        "facts/current.md": "- current\n",
        "insights/peer.md": "- concurrent peer\n",
    }
