"""Common Native ReAct memory retrieval tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_epoch,
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.context_search import (
    merge_memory_search_context,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemoryContextItem,
    MemoryDraftCheckpoint,
    MemoryReferenceDepth,
    MemorySearchResult,
    MemorySource,
    ResolvedContextItem,
    ResolvedMemoryLink,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MemorySearchRequest,
    MemorySearchResponse,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    MemoryDraftToolSession,
)
from pantaray_agents.local_runtime.tooling.memory_retrieval import (
    MemoryContextSession,
    MemoryRetrievalPolicy,
    MemoryRetrievalSession,
)
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

MEMORY_RETRIEVAL_CALL_LIMIT = 6


def _install_empty_memory_search(monkeypatch: pytest.MonkeyPatch) -> None:
    async def execute_memory_search(**kwargs) -> MemorySearchResponse:
        request = kwargs["request"]
        assert isinstance(request, MemorySearchRequest)
        assert request.current_epoch is not None
        return MemorySearchResponse(
            results=(), epoch=request.current_epoch, semantic_status="not_ready"
        )

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session."
        "execute_memory_search",
        execute_memory_search,
    )


@pytest.mark.asyncio
async def test_memory_search_commits_epoch_only_after_result_presentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _memory_session()
    original_epoch = session.memory_context.require_epoch()
    replacement_epoch = MemoryContextEpoch(
        epoch_id="replacement",
        run_id="run-1",
        user_id="user-1",
        items=(),
    )

    async def execute_memory_search(**_kwargs) -> MemorySearchResponse:
        malformed = {
            "source": "facts",
            "record_id": "fact-1",
            "content": "content",
            "source_path": "facts/index.md",
            "heading_path": None,
            "observed_at": "2026-07-31T00:00:00Z",
            "match_kind": "lexical",
        }
        return MemorySearchResponse(
            results=(malformed,),  # type: ignore[arg-type]
            epoch=replacement_epoch,
            semantic_status="available",
        )

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session."
        "execute_memory_search",
        execute_memory_search,
    )
    tools = _retrieval_tools(tmp_path, session)

    with pytest.raises(ValueError, match="no context handle"):
        await tools.search(_tool_call("memory_search", {"query": "needle"}), 1)

    assert session.memory_context.epoch is original_epoch


def test_memory_epoch_checkpoint_preserves_reference_depth() -> None:
    epoch = MemoryContextEpoch(
        epoch_id="epoch-1",
        run_id="run-1",
        user_id="user-1",
        items=(
            _resolved_item(
                handle="ctx_reference",
                fragment_id="fragment-reference",
                reference_depth=1,
            ),
        ),
    )

    restored = deserialize_memory_epoch(serialize_memory_epoch(epoch))

    assert restored == epoch


def test_memory_epoch_checkpoint_preserves_agent_experience_source() -> None:
    epoch = MemoryContextEpoch(
        epoch_id="epoch-1",
        run_id="run-1",
        user_id="user-1",
        items=(
            _resolved_item(
                handle="ctx_agent_experience",
                fragment_id="fragment-agent-experience",
                source="agent_experience",
                source_path="agent_experience/index.md",
            ),
        ),
    )

    restored = deserialize_memory_epoch(serialize_memory_epoch(epoch))

    assert restored == epoch


@pytest.mark.asyncio
@pytest.mark.usefixtures("tokyo_local_zone")
async def test_memory_search_reuses_handle_and_promotes_direct_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    visible = _resolved_item(
        handle="ctx_existing", fragment_id="fragment-1", reference_depth=1
    )
    duplicate = _resolved_item(handle="ctx_duplicate", fragment_id="fragment-1")
    memory_session = MemoryDraftToolSession(
        editable_policy=EditablePathPolicy(("facts/**",)),
        draft=MemoryDraftCheckpoint(
            draft_session_id="draft-1",
            user_id="user-1",
            owner_node_id="node-owner",
            base_revision_id=None,
            draft_revision="sha256:draft",
            documents=(),
            links=(),
            applied_commands=(),
        ),
        memory_context=MemoryContextSession(
            user_id="user-1",
            run_id="run-1",
            epoch=MemoryContextEpoch(
                epoch_id="epoch-1",
                run_id="run-1",
                user_id="user-1",
                items=(visible,),
            ),
        ),
    )
    added_epoch = MemoryContextEpoch(
        epoch_id="epoch-search",
        run_id="run-1",
        user_id="user-1",
        items=(duplicate,),
    )
    results: list[MemorySearchResult] = [
        {
            "source": "facts",
            "record_id": "fact-1",
            "content": "same fragment",
            "source_path": "facts/index.md",
            "heading_path": None,
            "observed_at": "2026-09-26T21:50:00.000000Z",
            "match_kind": "lexical",
            "context_handle": "ctx_duplicate",
        }
    ]
    captured: list[MemorySearchRequest] = []

    async def execute_memory_search(**kwargs) -> MemorySearchResponse:
        request = kwargs["request"]
        assert isinstance(request, MemorySearchRequest)
        captured.append(request)
        merged_results, merged_epoch = merge_memory_search_context(
            results=results,
            current_epoch=request.current_epoch,
            search_epoch=added_epoch,
        )
        return MemorySearchResponse(
            results=tuple(merged_results),
            epoch=merged_epoch,
            semantic_status="available",
        )

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session."
        "execute_memory_search",
        execute_memory_search,
    )
    tools = _retrieval_tools(tmp_path, memory_session)
    original_epoch = memory_session.memory_context.epoch

    result = await tools.search(_tool_call("memory_search", {"query": "same"}), 1)

    assert result.status == "success"
    assert result.output["semantic_status"] == "available"
    assert result.output["semantic_error_code"] is None
    request = captured[0]
    assert request.user_id == "user-1"
    assert request.run_id == "run-1"
    assert request.query == "same"
    assert request.focus == "all"
    assert request.limit == 8
    assert request.current_epoch is original_epoch
    assert result.output["results"][0]["context_handle"] == "ctx_existing"
    assert result.output["results"][0]["observed_at"] == "2026-09-27T06:50+09:00"
    assert (
        memory_session.memory_context.require_epoch().items[0].item.context_handle
        == "ctx_existing"
    )
    assert memory_session.memory_context.require_epoch().items[0].reference_depth == 0


@pytest.mark.asyncio
async def test_memory_search_enforces_result_limit_at_execution_boundary(
    tmp_path: Path,
) -> None:
    tools = _retrieval_tools(tmp_path, _memory_session())

    result = await tools.search(
        _tool_call("memory_search", {"query": "x", "limit": 9}), 1
    )

    assert result.status == "error"
    assert result.output["error_code"] == "MEMORY_SEARCH_INVALID"


@pytest.mark.asyncio
async def test_retrieval_budget_is_shared_and_reference_results_cannot_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_empty_memory_search(monkeypatch)
    source = _resolved_item(handle="ctx_source", fragment_id="fragment-source")
    session = _memory_session(items=(source,))
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {"node_id": "node-target"}
    connection_context = MagicMock()
    connection_context.__enter__.return_value = connection
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session.open_memory_catalog_connection",
        lambda **_kwargs: connection_context,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session.follow_memory_reference",
        lambda **kwargs: _followed_reference(
            epoch=kwargs["epoch"], target_source="long_term_insight"
        ),
    )
    tools = _retrieval_tools(tmp_path, session)
    for step in range(1, MEMORY_RETRIEVAL_CALL_LIMIT):
        result = await tools.search(_tool_call("memory_search", {"query": "x"}), step)
        assert result.status == "success"

    followed = await tools.follow_reference(
        _tool_call(
            "get_memory_reference",
            {"source_handle": "ctx_source", "local_ref_id": "ref_1"},
        ),
        MEMORY_RETRIEVAL_CALL_LIMIT,
    )
    target_handle = followed.output["reference"]["target_context_handle"]
    chained = await tools.follow_reference(
        _tool_call(
            "get_memory_reference",
            {"source_handle": target_handle, "local_ref_id": "ref_2"},
        ),
        MEMORY_RETRIEVAL_CALL_LIMIT + 1,
    )

    assert followed.status == "success"
    assert followed.output["calls_remaining"] == 0
    assert chained.status == "error"
    assert chained.output["error_code"] == "MEMORY_RETRIEVAL_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_reference_result_cannot_be_used_for_second_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _resolved_item(handle="ctx_source", fragment_id="fragment-source")
    session = _memory_session(items=(source,))
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {"node_id": "node-target"}
    connection_context = MagicMock()
    connection_context.__enter__.return_value = connection
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session.open_memory_catalog_connection",
        lambda **_kwargs: connection_context,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session.follow_memory_reference",
        lambda **kwargs: _followed_reference(epoch=kwargs["epoch"]),
    )
    tools = _retrieval_tools(tmp_path, session)
    first = await tools.follow_reference(
        _tool_call(
            "get_memory_reference",
            {"source_handle": "ctx_source", "local_ref_id": "ref_1"},
        ),
        1,
    )
    target_handle = first.output["reference"]["target_context_handle"]

    second = await tools.follow_reference(
        _tool_call(
            "get_memory_reference",
            {"source_handle": target_handle, "local_ref_id": "ref_2"},
        ),
        2,
    )

    assert second.status == "error"
    assert second.output["error_code"] == "MEMORY_REFERENCE_DEPTH_EXCEEDED"


@pytest.mark.asyncio
async def test_memory_reference_commits_epoch_only_after_result_presentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _resolved_item(handle="ctx_source", fragment_id="fragment-source")
    session = _memory_session(items=(source,))
    original_epoch = session.memory_context.require_epoch()
    replacement_epoch = MemoryContextEpoch(
        epoch_id="replacement",
        run_id="run-1",
        user_id="user-1",
        items=(),
    )
    connection_context = MagicMock()
    connection_context.__enter__.return_value = MagicMock()
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session."
        "open_memory_catalog_connection",
        lambda **_kwargs: connection_context,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session."
        "follow_memory_reference",
        lambda **_kwargs: (replacement_epoch, {}),
    )
    tools = _retrieval_tools(tmp_path, session)

    with pytest.raises(ValueError, match="target_context_handle"):
        await tools.follow_reference(
            _tool_call(
                "get_memory_reference",
                {"source_handle": "ctx_source", "local_ref_id": "ref_1"},
            ),
            1,
        )

    assert session.memory_context.epoch is original_epoch


@pytest.mark.asyncio
async def test_directly_visible_reference_target_keeps_root_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _resolved_item(handle="ctx_source", fragment_id="fragment-source")
    target = _resolved_item(handle="ctx_target", fragment_id="fragment-target")
    session = _memory_session(items=(source, target))
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {"node_id": "node-target"}
    connection_context = MagicMock()
    connection_context.__enter__.return_value = connection
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session.open_memory_catalog_connection",
        lambda **_kwargs: connection_context,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.memory_retrieval.session.follow_memory_reference",
        lambda **kwargs: _followed_reference(epoch=kwargs["epoch"]),
    )
    tools = _retrieval_tools(tmp_path, session)

    first = await tools.follow_reference(
        _tool_call(
            "get_memory_reference",
            {"source_handle": "ctx_source", "local_ref_id": "ref_1"},
        ),
        1,
    )
    second = await tools.follow_reference(
        _tool_call(
            "get_memory_reference",
            {"source_handle": "ctx_target", "local_ref_id": "ref_2"},
        ),
        2,
    )

    assert first.status == "success"
    assert second.status == "success"
    assert session.memory_context.require_epoch().items[1].reference_depth == 0


def _resolved_item(
    *,
    handle: str,
    fragment_id: str,
    reference_depth: MemoryReferenceDepth = 0,
    source: MemorySource = "fact",
    source_path: str = "facts/index.md",
) -> ResolvedContextItem:
    return ResolvedContextItem(
        item=MemoryContextItem(
            context_handle=handle,
            source=source,
            label=source,
            source_path=source_path,
            heading_path=None,
            content="same fragment",
            observed_at="2026-07-31T00:00:00Z",
        ),
        user_id="user-1",
        fragment_id=fragment_id,
        revision_id="revision-1",
        node_id="node-1",
        reference_depth=reference_depth,
    )


def _followed_reference(
    *,
    epoch: MemoryContextEpoch,
    target_source: MemorySource = "fact",
) -> tuple[MemoryContextEpoch, ResolvedMemoryLink]:
    extended, target = append_memory_context_item(
        epoch=epoch,
        source=target_source,
        label="memory ref ref_1 target",
        source_path="facts/target.md",
        heading_path=None,
        content="target content",
        fragment_id="fragment-target",
        revision_id="revision-target",
        node_id="node-target",
        reference_depth=1,
    )
    resolved: ResolvedMemoryLink = {
        "local_ref_id": "ref_1",
        "reference_note": "supports",
        "source_path": "facts/index.md",
        "source_heading_path": None,
        "target_fragment_id": "fragment-target",
        "target_source": target_source,
        "target_content": "target content",
        "target_path": "facts/target.md",
        "target_heading_path": None,
        "target_revision_id": "revision-target",
        "target_is_current": True,
        "target_lifecycle": "active",
        "target_integrity": "healthy",
        "current_target_revision_id": "revision-target",
        "target_context_handle": target.item.context_handle,
    }
    return extended, resolved


def _memory_session(
    *, items: tuple[ResolvedContextItem, ...] = ()
) -> MemoryDraftToolSession:
    return MemoryDraftToolSession(
        editable_policy=EditablePathPolicy(("facts/**",)),
        draft=MemoryDraftCheckpoint(
            draft_session_id="draft-1",
            user_id="user-1",
            owner_node_id="node-owner",
            base_revision_id=None,
            draft_revision="sha256:draft",
            documents=(),
            links=(),
            applied_commands=(),
        ),
        memory_context=MemoryContextSession(
            user_id="user-1",
            run_id="run-1",
            epoch=MemoryContextEpoch(
                epoch_id="epoch-1",
                run_id="run-1",
                user_id="user-1",
                items=items,
            ),
        ),
    )


def _retrieval_tools(
    tmp_path: Path, memory_session: MemoryDraftToolSession
) -> MemoryRetrievalSession:
    return MemoryRetrievalSession(
        db_path=tmp_path / "runtime.sqlite3",
        busy_timeout_ms=1_000,
        context=memory_session.memory_context,
        policy=MemoryRetrievalPolicy(
            allowed_focuses=("all",),
            default_focus="all",
            max_results=8,
            default_limit=8,
            call_limit=MEMORY_RETRIEVAL_CALL_LIMIT,
            pinned_revisions=None,
            search_content_max_chars=None,
            reference_content_max_chars=None,
            enqueue_repair_on_reference_failure=True,
            full_read_tools_available=True,
        ),
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
