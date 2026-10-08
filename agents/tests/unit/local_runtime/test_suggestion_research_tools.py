from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import pantaray_agents.local_runtime.tooling.suggestion_research.snapshot as snapshot_module
import pantaray_agents.tools.files.access as file_access_module
from pantaray_agents.local_runtime.memory_catalog.artifact_domain_publication import (
    FactArtifactPublication,
    LongTermInsightArtifactPublication,
    publish_fact_artifact,
    publish_long_term_insight_artifact,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.context_search import (
    merge_memory_search_context,
    search_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.draft import (
    create_memory_draft,
    replace_draft_documents,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    activate_embedding_generation_in_transaction,
    ensure_user_embedding_generation,
    load_active_embedding_generation,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemoryDocument,
    MemoryRevision,
    MemorySearchResult,
)
from pantaray_agents.local_runtime.memory_catalog.publication import (
    MemoryPublicationRequest,
    activate_artifact_revision,
    create_artifact_revision_intent,
    materialize_artifact_revision,
)
from pantaray_agents.local_runtime.memory_catalog.reconciler import (
    reconcile_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MemorySearchRequest,
    MemorySearchResponse,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    store_embedding_success,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
    list_workspace_settings,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    LocalSuggestionResearchTools,
    build_suggestion_research_snapshot,
)
from pantaray_agents.tools.contract import (
    BrokerPolicyError,
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)
from pantaray_agents.tools.files import (
    workspace_descriptor_access as descriptor_access,
)
from pantaray_agents.tools.files.access import ReadOnlyFileAccess
from pantaray_agents.tools.files.roots import (
    MemoryReadRoot,
    WorkspaceReadRoot,
    memory_revision_by_source,
)
from pantaray_agents.tools.web.session import WebResearchToolSession

from .embedding_test_support import TEST_EMBEDDING_SPECIFICATION
from .test_memory_artifact_publication import (
    _fact_draft,
    _memory_run,
    _publication,
    _runtime,
)
from .test_workspace_settings_repository import TIMESTAMP
from .test_workspace_settings_repository import _bootstrap_db as _bootstrap_app_db

BUSY_TIMEOUT_MS = 1_000
QUERY_EMBEDDING = (1.0, *(0.0 for _ in range(511)))


def _tool_call(tool_name: str, args: dict[str, object]) -> ReactToolCall:
    envelope = ToolCallEnvelope(
        tool_id=tool_name,
        reason=None,
        args=args,  # type: ignore[arg-type]
    )
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=envelope,
    )


def _search_result(
    *,
    run_id: str,
    fragment_id: str,
) -> tuple[list[dict[str, object]], MemoryContextEpoch]:
    epoch, item = append_memory_context_item(
        epoch=MemoryContextEpoch(f"epoch-{fragment_id}", run_id, "user-1", ()),
        source="fact",
        label=fragment_id,
        source_path=f"facts/{fragment_id}.md",
        heading_path=None,
        content=fragment_id,
        fragment_id=fragment_id,
        revision_id=f"revision-{fragment_id}",
        node_id=f"node-{fragment_id}",
        reference_depth=0,
    )
    return [
        {
            "source": "facts",
            "record_id": fragment_id,
            "content": fragment_id,
            "source_path": f"facts/{fragment_id}.md",
            "heading_path": None,
            "observed_at": "2026-08-09T00:00:00Z",
            "match_kind": "lexical",
            "context_handle": item.item.context_handle,
        }
    ], epoch


def _bootstrap_db(tmp_path: Path) -> Path:
    # App storage lives apart from the folders a test registers as the user's own.
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    return _bootstrap_app_db(app_data)


def _register_workspace(*, db_path: Path, root: Path) -> None:
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        real_path=root,
        display_name="Repo",
        organization_ids=(),
        project_ids=(),
        now=TIMESTAMP,
    )


def _workspace_settings(db_path: Path) -> WorkspaceSettings:
    return list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
    )


def _snapshot(*, db_path: Path, artifact_root: Path | None = None):
    return build_suggestion_research_snapshot(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root or db_path.parent / "artifacts",
        user_id="user-1",
        workspace_settings=_workspace_settings(db_path),
    )


def test_read_only_file_access_reads_only_registered_roots(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "design.md"
    source.write_text("first\nshared contract\nthird\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    _register_workspace(db_path=db_path, root=root)
    snapshot = _snapshot(db_path=db_path)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)
    root_id = snapshot.roots[0].root_id

    read_result = reader.read(
        root_id=root_id,
        path="design.md",
        offset=2,
        column=1,
        limit=1,
    )
    assert read_result["content"] == "shared contract\n"
    assert read_result["next_offset"] == 3
    assert read_result["truncated"] is True
    assert read_result["truncation_reason"] == "page_limit"
    assert read_result["retry_hint"] == (
        "Continue with offset=next_offset and column=next_column."
    )
    with pytest.raises(BrokerPolicyError, match="root-relative"):
        reader.read(
            root_id=root_id,
            path=str(outside),
            offset=1,
            column=1,
            limit=1,
        )
    symlink = root / "outside-link"
    symlink.symlink_to(outside)
    with pytest.raises(BrokerPolicyError, match="symlink"):
        reader.read(
            root_id=root_id,
            path="outside-link",
            offset=1,
            column=1,
            limit=1,
        )


def test_read_only_file_access_hides_private_app_storage_in_a_parent_folder(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    storage = db_path.parent
    (storage / "notes.txt").write_text("needle secret\n", encoding="utf-8")
    (tmp_path / "sibling.txt").write_text("needle sibling\n", encoding="utf-8")
    _register_workspace(db_path=db_path, root=tmp_path)
    snapshot = _snapshot(db_path=db_path)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)
    root_id = snapshot.roots[-1].root_id

    listed = reader.list(root_id=root_id, path=".", max_depth=3, offset=1, limit=50)
    globbed = reader.glob(
        root_id=root_id, base_path=".", pattern="**/*", offset=1, limit=50
    )

    assert [entry["path"] for entry in listed["entries"]] == ["sibling.txt"]  # type: ignore[index]
    assert globbed["matches"] == ["sibling.txt"]
    alias = storage.with_name(storage.name.upper())
    private_paths = [f"{storage.name}/notes.txt", f"{storage.name}/{db_path.name}"]
    if alias.exists() and alias.samefile(storage):
        private_paths.append(f"{alias.name}/notes.txt")
    for path in private_paths:
        with pytest.raises(BrokerPolicyError) as caught:
            reader.read(root_id=root_id, path=path, offset=1, column=1, limit=10)
        assert "private app storage" in str(caught.value), path
        assert "memory_search" in str(caught.value), path
        assert "memory_sql" not in str(caught.value), path
    for search in (
        lambda: reader.list(
            root_id=root_id, path=storage.name, max_depth=1, offset=1, limit=10
        ),
        lambda: reader.glob(
            root_id=root_id, base_path=storage.name, pattern="*", offset=1, limit=10
        ),
    ):
        with pytest.raises(BrokerPolicyError, match="private app storage"):
            search()


def test_read_only_file_access_never_walks_into_private_app_storage(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    storage = db_path.parent
    records = storage / "records"
    records.mkdir()
    for index in range(60):
        (records / f"{index}.txt").write_text("needle secret\n", encoding="utf-8")
    # Opening this would fail the whole scan, so the scan must not reach it.
    unreadable = storage / "unreadable.txt"
    unreadable.write_text("needle secret\n", encoding="utf-8")
    unreadable.chmod(0)
    (tmp_path / "sibling.txt").write_text("needle sibling\n", encoding="utf-8")
    _register_workspace(db_path=db_path, root=tmp_path)
    snapshot = _snapshot(db_path=db_path)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)
    root_id = snapshot.roots[-1].root_id

    try:
        results = (
            reader.list(root_id=root_id, path=".", max_depth=4, offset=1, limit=50),
            reader.glob(
                root_id=root_id, base_path=".", pattern="**/*", offset=1, limit=50
            ),
        )
    finally:
        unreadable.chmod(0o600)

    listed, globbed = results
    assert [entry["path"] for entry in listed["entries"]] == ["sibling.txt"]  # type: ignore[index]
    assert globbed["matches"] == ["sibling.txt"]
    for result in results:
        assert result["truncated"] is False
        assert result["warning"] is None


def test_workspace_read_remains_pinned_after_parent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    parent = root / "parent"
    parent.mkdir(parents=True)
    (parent / "document.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "document.txt").write_text("outside secret\n", encoding="utf-8")
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "document.txt" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(root / "original-parent")
            parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(descriptor_access.os, "open", racing_open)
    reader = ReadOnlyFileAccess(
        roots=(WorkspaceReadRoot("workspace", "Workspace", root, ()),)
    )

    result = reader.read(
        root_id="workspace",
        path="parent/document.txt",
        offset=1,
        column=1,
        limit=10,
    )

    assert result["content"] == "inside\n"
    assert "outside secret" not in str(result)


def test_workspace_list_remains_pinned_after_base_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    base = root / "base"
    base.mkdir(parents=True)
    (base / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    for index in range(30):
        (outside / f"outside-{index}.txt").write_text("secret\n", encoding="utf-8")
    real_scandir = os.scandir
    swapped = False

    def racing_scandir(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ):
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            base.rename(root / "original-base")
            base.symlink_to(outside, target_is_directory=True)
        return real_scandir(path)

    monkeypatch.setattr(descriptor_access.os, "scandir", racing_scandir)
    reader = ReadOnlyFileAccess(
        roots=(WorkspaceReadRoot("workspace", "Workspace", root, ()),)
    )

    result = reader.list(
        root_id="workspace",
        path="base",
        max_depth=1,
        offset=1,
        limit=100,
    )

    assert result["entries"] == [
        {"path": "base/inside.txt", "kind": "file", "name": "inside.txt"}
    ]
    assert result["truncation_reason"] is None


def test_workspace_glob_remains_pinned_after_base_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    base = root / "base"
    base.mkdir(parents=True)
    (base / "inside.py").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("secret\n", encoding="utf-8")
    real_scandir = os.scandir
    swapped = False

    def racing_scandir(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ):
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            base.rename(root / "original-base")
            base.symlink_to(outside, target_is_directory=True)
        return real_scandir(path)

    monkeypatch.setattr(descriptor_access.os, "scandir", racing_scandir)
    reader = ReadOnlyFileAccess(
        roots=(WorkspaceReadRoot("workspace", "Workspace", root, ()),)
    )

    result = reader.glob(
        root_id="workspace",
        base_path="base",
        pattern="**/*.py",
        offset=1,
        limit=100,
    )

    assert result["matches"] == ["base/inside.py"]
    assert "outside.py" not in str(result)


def test_fact_snapshot_seeds_index_and_reads_leaf_from_immutable_revision(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    base_draft = _fact_draft(db_path)
    draft = replace_draft_documents(
        draft=base_draft,
        documents=(
            MemoryDocument("facts/index.md", "# Facts\n- [Project](project.md)\n"),
            MemoryDocument("facts/project.md", "# Project\nLeaf-only detail\n"),
        ),
    )
    revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(draft),
    )

    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)

    assert (
        "### Structured Facts\nEntry: facts/index.md" in snapshot.stable_memory.prompt
    )
    assert "Profile brief:\nbrief" in snapshot.stable_memory.prompt
    assert "Documents:\n- facts/index.md\n- facts/project.md" in (
        snapshot.stable_memory.prompt
    )
    assert "[Project](project.md)" in snapshot.stable_memory.prompt
    assert "Leaf-only detail" not in snapshot.stable_memory.prompt
    assert reader.list(root_id="facts", path="facts", max_depth=2, offset=1, limit=10)[
        "entries"
    ]
    assert reader.glob(
        root_id="facts", base_path="facts", pattern="*.md", offset=1, limit=10
    )["matches"] == ["facts/index.md", "facts/project.md"]
    assert reader.grep(
        root_id="facts",
        base_path="facts",
        pattern="Leaf-only",
        include_glob="*.md",
        offset=1,
        max_matches=10,
    )["matches"] == [
        {"path": "facts/project.md", "line_number": 2, "line": "Leaf-only detail"}
    ]
    artifact_leaf = (
        artifact_root / str(revision.artifact_root_path) / "facts/project.md"
    )
    artifact_leaf.write_text("# Project\nChanged after snapshot\n", encoding="utf-8")

    result = reader.read(
        root_id="facts",
        path="facts/project.md",
        offset=1,
        column=1,
        limit=20,
    )
    assert result["content"] == "# Project\nLeaf-only detail\n"


def test_published_insight_snapshot_seeds_index_and_reads_leaf(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    _publish_insight_tree(db_path=db_path, artifact_root=artifact_root)

    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)

    assert "### Long-term Insights\nEntry: insights/index.md" in (
        snapshot.stable_memory.prompt
    )
    assert "Profile brief:\ninsight brief" in snapshot.stable_memory.prompt
    assert "Documents:\n- insights/index.md\n- insights/topic.md" in (
        snapshot.stable_memory.prompt
    )
    assert "Insight leaf detail" not in snapshot.stable_memory.prompt
    assert len(snapshot.stable_memory.prompt) <= 4_500
    assert (
        reader.read(
            root_id="insights",
            path="insights/topic.md",
            offset=1,
            column=1,
            limit=20,
        )["content"]
        == "# Topic\nInsight leaf detail\n"
    )


def test_memory_grep_stops_a_backtracking_pattern_at_the_search_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plain backtracking engine needs ~0.3 s here and the timed engine ~0.7 s,
    # so the shortened deadline must cut the search off instead of finishing it.
    monkeypatch.setattr(file_access_module, "SEARCH_TIMEOUT_SECONDS", 0.05)
    backtracking_line = ("来週の定例で見積もりの件を先方に確認" * 2)[:24]
    reader = ReadOnlyFileAccess(
        roots=(
            MemoryReadRoot(
                root_id="facts",
                display_name="Facts",
                revision_id="revision-1",
                node_id="node-1",
                entry_path="facts/index.md",
                documents=(
                    MemoryDocument(
                        "facts/notes.md",
                        f"release 2\n{backtracking_line}\nrelease 3\n",
                    ),
                ),
            ),
        )
    )

    result = reader.grep(
        root_id="facts",
        base_path=".",
        pattern=r"(?:\w|\w\w|\w\w\w)*[0-9]$",
        include_glob=None,
        offset=1,
        max_matches=10,
    )

    assert result["matches"] == [
        {"path": "facts/notes.md", "line_number": 1, "line": "release 2"}
    ]
    assert result["truncated"] is True
    assert result["truncation_reason"] == "timeout"


def test_stable_memory_bounds_include_truncation_markers() -> None:
    bounded = snapshot_module._bounded("x" * 1_000)  # noqa: SLF001
    tree = snapshot_module._render_document_tree(  # noqa: SLF001
        tuple(
            MemoryDocument(f"facts/topic-{index:03d}.md", "leaf")
            for index in range(100)
        )
    )

    assert len(bounded) <= 650
    assert bounded.endswith("[truncated; find the rest with memory_search]")
    assert len(tree) <= 450
    assert tree.endswith("[truncated]")


def test_snapshot_read_and_search_remain_pinned_after_fact_head_update(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    first_draft = replace_draft_documents(
        draft=_fact_draft(db_path),
        documents=(
            MemoryDocument("facts/index.md", "# Facts\n- [Old](old.md)\n"),
            MemoryDocument("facts/old.md", "# Old\nDurableobsolete marker\n"),
        ),
    )
    first_revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(first_draft),
    )
    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)

    second_revision = _publish_second_fact_revision(
        db_path=db_path,
        artifact_root=artifact_root,
    )
    reader = ReadOnlyFileAccess(roots=snapshot.roots)
    read_result = reader.read(
        root_id="facts",
        path="facts/old.md",
        offset=1,
        column=1,
        limit=20,
    )
    ensure_user_embedding_generation(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ) as connection:
        rows = connection.execute(
            """
            SELECT fragments.user_id, work.generation_id, work.chunk_index,
                   fragments.fragment_id,
                   fragments.content_sha256
            FROM memory_embedding_work AS work
            JOIN memory_fragments AS fragments
              ON fragments.user_id = work.user_id
             AND fragments.fragment_id = work.fragment_id
            WHERE work.state = 'pending'
            """
        ).fetchall()
        for row in rows:
            store_embedding_success(
                connection,
                user_id=str(row["user_id"]),
                generation_id=int(row["generation_id"]),
                fragment_id=str(row["fragment_id"]),
                chunk_index=int(row["chunk_index"]),
                fragment_content_sha256=str(row["content_sha256"]),
                vector=QUERY_EMBEDDING,
                created_at="2026-08-09T00:00:00Z",
            )
        building = connection.execute(
            """
            SELECT generation_id FROM memory_embedding_user_generations
            WHERE user_id = 'user-1' AND state = 'building'
            """
        ).fetchone()
        if building is not None:
            activate_embedding_generation_in_transaction(
                connection,
                user_id="user-1",
                generation_id=int(building[0]),
                activated_at="2026-08-09T00:00:00Z",
            )
        embedding_generation = load_active_embedding_generation(
            connection,
            user_id="user-1",
        )
        assert embedding_generation is not None
        pinned, epoch = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="suggestion-1",
            query="durableobsolete",
            query_embedding=QUERY_EMBEDDING,
            embedding_generation=embedding_generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
            pinned_revisions=memory_revision_by_source(snapshot.roots),
        )
        current, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="current-search",
            query="durableobsolete",
            query_embedding=QUERY_EMBEDDING,
            embedding_generation=embedding_generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert first_revision.revision_id != second_revision.revision_id
    assert read_result["content"] == "# Old\nDurableobsolete marker\n"
    assert "Durableobsolete marker" in [row["content"] for row in pinned]
    assert {item.revision_id for item in epoch.items} == {first_revision.revision_id}
    assert current
    assert all(row["match_kind"] == "semantic" for row in current)


def test_snapshot_omits_root_for_preparing_node(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    _fact_draft(db_path)

    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)

    assert all(root.root_id != "facts" for root in snapshot.roots)


def test_revision_integrity_rollback_restores_revision_owned_brief(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    first_revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    second_revision = _publish_second_fact_revision(
        db_path=db_path,
        artifact_root=artifact_root,
    )
    current_index = (
        artifact_root / str(second_revision.artifact_root_path) / "facts/index.md"
    )
    current_index.write_text("corrupt current head", encoding="utf-8")
    with pytest.raises(MemoryCatalogIntegrityError):
        _snapshot(db_path=db_path, artifact_root=artifact_root)

    _, completed = reconcile_memory_catalog(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        scan_limit=100,
    )

    with sqlite3.connect(db_path) as connection:
        restored = connection.execute(
            """
            SELECT nodes.current_revision_id, facts.structured_fact_sha256,
                   facts.facts_profile_brief
            FROM memory_nodes AS nodes
            JOIN agent_facts AS facts
              ON facts.user_id = nodes.user_id
             AND facts.fact_id = nodes.source_record_id
            WHERE nodes.user_id = 'user-1' AND nodes.source_type = 'fact'
            """
        ).fetchone()
    assert completed == 1
    assert restored == (
        first_revision.revision_id,
        first_revision.content_sha256,
        "brief",
    )


def test_snapshot_integrity_failure_enqueues_revision_repair(tmp_path: Path) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    artifact_index = artifact_root / str(revision.artifact_root_path) / "facts/index.md"
    artifact_index.write_text("changed", encoding="utf-8")

    with pytest.raises(MemoryCatalogIntegrityError):
        _snapshot(db_path=db_path, artifact_root=artifact_root)

    with sqlite3.connect(db_path) as connection:
        repair = connection.execute(
            "SELECT reason FROM memory_repair_queue WHERE user_id = 'user-1'"
        ).fetchone()
    assert repair == ("revision_integrity",)


def test_snapshot_os_error_does_not_enqueue_integrity_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )

    def fail_artifact_read(**_kwargs: object) -> None:
        raise OSError("temporary artifact I/O failure")

    monkeypatch.setattr(
        snapshot_module,
        "read_artifact_revision_documents",
        fail_artifact_read,
    )
    with pytest.raises(OSError, match="temporary artifact I/O failure"):
        _snapshot(db_path=db_path, artifact_root=artifact_root)

    with sqlite3.connect(db_path) as connection:
        repair_count = connection.execute(
            "SELECT COUNT(*) FROM memory_repair_queue WHERE user_id = 'user-1'"
        ).fetchone()[0]
    assert repair_count == 0


def test_snapshot_pins_catalog_heads_without_legacy_projection_rows(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    fact_revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    insight_revision = _publish_insight_tree(
        db_path=db_path,
        artifact_root=artifact_root,
    )
    experience_revision = _publish_experience_tree(
        db_path=db_path,
        artifact_root=artifact_root,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM agent_facts")
        connection.execute("DELETE FROM agent_long_term_insight_state")

    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)

    assert [root.root_id for root in snapshot.roots] == [
        "facts",
        "insights",
        "agent_experience",
    ]
    assert memory_revision_by_source(snapshot.roots) == {
        "fact": fact_revision.revision_id,
        "long_term_insight": insight_revision.revision_id,
        "agent_experience": experience_revision.revision_id,
    }
    assert "### Agent Experience\nEntry: agent_experience/index.md" in (
        snapshot.stable_memory.prompt
    )
    assert "Documents:\n- agent_experience/entries/exp-1.md" in (
        snapshot.stable_memory.prompt
    )
    assert "Experience leaf detail" not in snapshot.stable_memory.prompt
    assert len(snapshot.stable_memory.prompt) <= 4_500
    assert (
        reader.read(
            root_id="agent_experience",
            path="agent_experience/entries/exp-1.md",
            offset=1,
            column=1,
            limit=20,
        )["content"]
        == "# Experience\nExperience leaf detail\n"
    )


def test_snapshot_without_experience_head_excludes_later_experience_revision(
    tmp_path: Path,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)
    pinned_revisions = memory_revision_by_source(snapshot.roots)
    _publish_experience_tree(db_path=db_path, artifact_root=artifact_root)

    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ) as connection:
        pinned, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="suggestion-1",
            query="retryparallelism",
            query_embedding=None,
            embedding_generation=None,
            focus="agent_work",
            center_time=None,
            radius_hours=None,
            limit=8,
            pinned_revisions=pinned_revisions,
        )
        current, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="current-search",
            query="retryparallelism",
            query_embedding=None,
            embedding_generation=None,
            focus="agent_work",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert all(root.root_id != "agent_experience" for root in snapshot.roots)
    assert pinned_revisions["agent_experience"] is None
    assert pinned == []
    assert [row["source"] for row in current] == ["agent_experience"]


def test_suggestion_research_tool_set_is_read_only(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    _register_workspace(db_path=db_path, root=root)
    runtime = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    )

    definitions = runtime.build_tool_definitions(
        user_id="user-1",
        run_id="suggestion-1",
    )
    names = {definition.name for definition in definitions}

    assert names == {
        "memory_search",
        "get_memory_reference",
        "memory_sql",
        "read",
        "list",
        "glob",
        "grep",
        "web_search",
        "web_extract",
        "zanei_timeline",
        "zanei_query",
    }
    assert names.isdisjoint({"thinking", "apply_patch", "bash", "run_python"})
    for definition in definitions:
        required = definition.request_schema.get("required", [])
        assert isinstance(required, list)
        assert "hypothesis" not in required
        assert "evidence_goal" not in required


def test_suggestion_research_tool_build_uses_preloaded_snapshot(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    _register_workspace(db_path=db_path, root=root)
    snapshot = _snapshot(db_path=db_path)

    definitions = LocalSuggestionResearchTools(
        db_path=tmp_path / "unavailable.db",
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=snapshot,
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")

    assert {definition.name for definition in definitions} >= {
        "read",
        "list",
        "glob",
        "grep",
    }


@pytest.mark.asyncio
async def test_suggestion_memory_search_keeps_prior_handles_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    first_rows, first_epoch = _search_result(
        run_id="suggestion-1",
        fragment_id="first",
    )
    second_rows, second_epoch = _search_result(
        run_id="suggestion-1",
        fragment_id="second",
    )
    searches = iter(((first_rows, first_epoch), (second_rows, second_epoch)))

    async def execute_memory_search(**kwargs) -> MemorySearchResponse:
        request = kwargs["request"]
        assert isinstance(request, MemorySearchRequest)
        rows, search_epoch = next(searches)
        rows, epoch = merge_memory_search_context(
            results=rows,
            current_epoch=request.current_epoch,
            search_epoch=search_epoch,
        )
        return MemorySearchResponse(
            results=tuple(rows), epoch=epoch, semantic_status="available"
        )

    monkeypatch.setattr(
        "pantaray_agents.tools.memory.retrieval.execute_memory_search",
        execute_memory_search,
    )

    def follow_reference(**kwargs):
        epoch = kwargs["epoch"]
        assert [item.fragment_id for item in epoch.items] == ["first", "second"]
        assert kwargs["source_handle"] == first_rows[0]["context_handle"]
        return epoch, {
            "local_ref_id": "ref-1",
            "reference_note": "supports",
            "source_path": "facts/source.md",
            "source_heading_path": None,
            "target_fragment_id": "target-fragment",
            "target_source": "fact",
            "target_content": "target",
            "target_path": "facts/target.md",
            "target_heading_path": None,
            "target_revision_id": "target-revision",
            "target_lifecycle": "active",
            "target_integrity": "healthy",
            "current_target_revision_id": "target-revision",
            "target_context_handle": "ctx-target",
            "target_is_current": True,
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.memory.retrieval.follow_memory_reference",
        follow_reference,
    )
    definitions = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    registry = ReactToolRegistry(definitions)

    for step_number, query in enumerate(("first", "second"), start=1):
        result = await registry.execute(
            _tool_call(
                "memory_search",
                {
                    "query": query,
                    "focus": "all",
                    "limit": 1,
                },
            ),
            step_number,
        )
        assert result.status == "success"
        assert result.output["notes"][-1].startswith("The results stop at limit=1;")

    reference = await registry.execute(
        _tool_call(
            "get_memory_reference",
            {
                "source_handle": first_rows[0]["context_handle"],
                "local_ref_id": "ref-1",
            },
        ),
        3,
    )

    assert reference.status == "success"


@pytest.mark.asyncio
async def test_suggestion_memory_search_uses_snapshot_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    revision = publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=_publication(_fact_draft(db_path)),
    )
    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)

    captured: list[MemorySearchRequest] = []

    async def execute_memory_search(**kwargs) -> MemorySearchResponse:
        request = kwargs["request"]
        assert isinstance(request, MemorySearchRequest)
        captured.append(request)
        assert request.pinned_revisions == {
            "fact": revision.revision_id,
            "long_term_insight": None,
            "agent_experience": None,
        }
        return MemorySearchResponse(
            results=(),
            epoch=MemoryContextEpoch("epoch", "suggestion-1", "user-1", ()),
            semantic_status="not_ready",
        )

    monkeypatch.setattr(
        "pantaray_agents.tools.memory.retrieval.execute_memory_search",
        execute_memory_search,
    )
    definitions = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=snapshot,
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    search_tool = next(
        definition for definition in definitions if definition.name == "memory_search"
    )

    result = await search_tool.execute(
        _tool_call(
            "memory_search",
            {
                "query": "durable",
                "focus": "stable_knowledge",
                "limit": 8,
            },
        ),
        1,
    )

    assert result.status == "success"
    request = captured[0]
    assert request.user_id == "user-1"
    assert request.run_id == "suggestion-1"
    assert request.query == "durable"
    assert request.focus == "stable_knowledge"
    assert request.limit == 8
    assert request.current_epoch is None


@pytest.mark.asyncio
async def test_suggestion_reads_memory_through_memory_search_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    full_content = "あ" * 2_500
    epoch, item = append_memory_context_item(
        epoch=MemoryContextEpoch("epoch", "suggestion-1", "user-1", ()),
        source="activity_log",
        label="activity",
        source_path="activity/entry",
        heading_path=None,
        content=full_content,
        fragment_id="activity-fragment",
        revision_id="activity-revision",
        node_id="activity-node",
        reference_depth=0,
    )
    rows: tuple[MemorySearchResult, ...] = (
        {
            "source": "activity_log",
            "record_id": "activity-1",
            "content": full_content,
            "source_path": "activity/entry",
            "heading_path": None,
            "observed_at": "2026-08-09T00:00:00Z",
            "match_kind": "lexical",
            "context_handle": item.item.context_handle,
        },
    )

    async def execute_memory_search(**_kwargs) -> MemorySearchResponse:
        return MemorySearchResponse(
            results=rows, epoch=epoch, semantic_status="available"
        )

    monkeypatch.setattr(
        "pantaray_agents.tools.memory.retrieval.execute_memory_search",
        execute_memory_search,
    )
    definitions = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    registry = ReactToolRegistry(definitions)

    search_result = await registry.execute(
        _tool_call(
            "memory_search",
            {
                "query": "activity",
                "focus": "activity",
                "limit": 1,
            },
        ),
        1,
    )
    search_row = search_result.output["results"][0]
    by_handle = await registry.execute(
        _tool_call("read", {"path": search_row["context_handle"]}), 2
    )
    by_memory_path = await registry.execute(
        _tool_call("read", {"path": "insights/todos.md"}), 3
    )

    # memory_search is where memory is read: the passage comes back whole.
    assert search_row["content"] == full_content
    assert search_row["content_truncated"] is False
    for refused in (by_handle, by_memory_path):
        assert refused.status == "error"
        assert refused.output["error_code"] == "PATH_NOT_ABSOLUTE"
        assert "memory_search" in refused.output["details"]["fix_hint"]


@pytest.mark.asyncio
async def test_suggestion_web_search_returns_shared_client_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    captured: dict[str, object] = {}

    async def invoke_web_tools_wrapper(**kwargs):
        captured.update(kwargs)
        return {
            "status": "success",
            "result": {
                "status": "success",
                "query": "current evidence",
                "results": [],
                "images": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        invoke_web_tools_wrapper,
    )
    definitions = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    tools = {definition.name: definition for definition in definitions}

    result = await tools["web_search"].execute(
        _tool_call(
            "web_search",
            {
                "query": "current evidence",
                "offset": 1,
                "limit": 8,
            },
        ),
        1,
    )

    assert result.status == "success"
    assert result.output == {
        "status": "success",
        "query": "current evidence",
        "results": [],
        "next_offset": None,
        "truncated": False,
        "truncation_reason": None,
        "retry_hint": None,
    }
    assert captured["tool_id"] == "web_search"
    assert captured["max_retries"] == 1


@pytest.mark.asyncio
async def test_suggestion_web_search_pages_structured_results_from_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    calls = 0

    async def invoke_web_tools_wrapper(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "status": "success",
            "result": {
                "status": "success",
                "query": "current evidence",
                "results": [
                    {
                        "title": f"Title {index}",
                        "url": f"https://example.com/{index}",
                        "content": "x" * (700 if index == 1 else 20),
                        "score": 1.0 / index,
                    }
                    for index in range(1, 4)
                ],
                "images": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        invoke_web_tools_wrapper,
    )
    registry = ReactToolRegistry(
        LocalSuggestionResearchTools(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            snapshot=_snapshot(db_path=db_path),
            activity_start=None,
        ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    )

    first = await registry.execute(
        _tool_call(
            "web_search",
            {
                "query": "current evidence",
                "offset": 1,
                "limit": 2,
            },
        ),
        1,
    )
    second = await registry.execute(
        _tool_call(
            "web_search",
            {
                "query": "current evidence",
                "offset": first.output["next_offset"],
                "limit": 2,
            },
        ),
        2,
    )

    assert calls == 1
    assert [row["title"] for row in first.output["results"]] == [
        "Title 1",
        "Title 2",
    ]
    assert first.output["results"][0]["content_truncated"] is True
    assert first.output["next_offset"] == 3
    assert [row["title"] for row in second.output["results"]] == ["Title 3"]
    assert second.output["next_offset"] is None


@pytest.mark.asyncio
async def test_suggestion_web_extract_pages_content_from_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    calls = 0

    async def invoke_web_tools_wrapper(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "status": "success",
            "result": {
                "results": [
                    {
                        "url": "https://example.com/page",
                        "raw_content": "abcdefghij",
                    }
                ],
                "failed_results": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        invoke_web_tools_wrapper,
    )
    registry = ReactToolRegistry(
        LocalSuggestionResearchTools(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            snapshot=_snapshot(db_path=db_path),
            activity_start=None,
        ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    )
    common_args = {
        "urls": ["https://example.com/page"],
        "query": None,
        "limit": 4,
    }

    first = await registry.execute(
        _tool_call("web_extract", {**common_args, "offset": 1}),
        1,
    )
    second = await registry.execute(
        _tool_call(
            "web_extract",
            {**common_args, "offset": first.output["results"][0]["next_offset"]},
        ),
        2,
    )
    third = await registry.execute(
        _tool_call(
            "web_extract",
            {**common_args, "offset": second.output["results"][0]["next_offset"]},
        ),
        3,
    )

    assert calls == 1
    assert first.output["results"] == [
        {
            "url": "https://example.com/page",
            "raw_content": "abcd",
            "offset": 1,
            "end_offset": 4,
            "total_chars": 10,
            "next_offset": 5,
            "truncated": True,
            "truncation_reason": "page_limit",
            "retry_hint": "Continue this URL with offset=next_offset.",
        }
    ]
    assert second.output["results"][0]["raw_content"] == "efgh"
    assert second.output["results"][0]["next_offset"] == 9
    assert third.output["results"][0]["raw_content"] == "ij"
    assert third.output["results"][0]["next_offset"] is None
    assert (
        "".join(
            result.output["results"][0]["raw_content"]
            for result in (first, second, third)
        )
        == "abcdefghij"
    )


@pytest.mark.asyncio
async def test_suggestion_web_extract_with_query_reports_excerpts_not_full_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    excerpts = "pricing table [...] plan limits"

    async def invoke_web_tools_wrapper(**_kwargs):
        return {
            "status": "success",
            "result": {
                "results": [
                    {"url": "https://example.com/page", "raw_content": excerpts}
                ],
                "failed_results": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        invoke_web_tools_wrapper,
    )
    registry = ReactToolRegistry(
        LocalSuggestionResearchTools(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            snapshot=_snapshot(db_path=db_path),
            activity_start=None,
        ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    )

    result = await registry.execute(
        _tool_call(
            "web_extract",
            {
                "urls": ["https://example.com/page"],
                "query": "pricing",
                "offset": 1,
                "limit": 1_000,
            },
        ),
        1,
    )

    row = result.output["results"][0]
    assert row["raw_content"] == excerpts
    assert row["next_offset"] is None
    assert row["truncated"] is True
    assert row["truncation_reason"] == "query_excerpts"
    assert "query=null" in row["retry_hint"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "args", "result"),
    [
        (
            "web_search",
            {"query": "evidence", "offset": 1, "limit": 8},
            {"status": "success", "query": "other", "results": [], "images": []},
        ),
        (
            "web_extract",
            {"urls": ["https://example.com/a"], "query": None, "offset": 1, "limit": 9},
            {
                "results": [{"url": "https://e.com/b", "raw_content": "body"}],
                "failed_results": [],
            },
        ),
    ],
)
async def test_suggestion_web_tools_reject_a_result_for_another_request(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: dict[str, object],
    result: dict[str, object],
) -> None:
    invoke = AsyncMock(return_value={"result": result})
    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper", invoke
    )
    registry = ReactToolRegistry(WebResearchToolSession(user_id="user-1").definitions())

    results = [await registry.execute(_tool_call(tool_name, args), n) for n in (1, 2)]

    assert [r.output["error_code"] for r in results] == ["WEB_TOOL_FAILED"] * 2
    assert invoke.await_count == 2


def _publish_insight_tree(
    *,
    db_path: Path,
    artifact_root: Path,
    documents: tuple[MemoryDocument, ...] | None = None,
) -> MemoryRevision:
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ) as connection:
        with immediate_transaction(connection):
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="long_term_insight",
                source_record_id="user-1",
            )
    draft = create_memory_draft(
        user_id="user-1",
        owner_node_id=node.node_id,
        base_revision_id=node.current_revision_id,
        documents=documents
        if documents is not None
        else (
            MemoryDocument("insights/index.md", "# Insights\n- [Topic](topic.md)\n"),
            MemoryDocument("insights/topic.md", "# Topic\nInsight leaf detail\n"),
        ),
    )
    return publish_long_term_insight_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=LongTermInsightArtifactPublication(
            memory_run=_memory_run(),
            user_id="user-1",
            source_run_id=_memory_run().job_id,
            draft=draft,
            profile_brief="insight brief",
            prompt_name="memory_update",
            prompt_version="1.0",
        ),
    )


def _publish_experience_tree(*, db_path: Path, artifact_root: Path) -> MemoryRevision:
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ) as connection:
        with immediate_transaction(connection):
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="agent_experience",
                source_record_id="user-1",
            )
            prepared = create_artifact_revision_intent(
                connection=connection,
                request=MemoryPublicationRequest(
                    source="agent_experience",
                    source_record_id="user-1",
                    draft=create_memory_draft(
                        user_id="user-1",
                        owner_node_id=node.node_id,
                        base_revision_id=node.current_revision_id,
                        documents=(
                            MemoryDocument(
                                "agent_experience/index.md",
                                "# Agent Experience Index\n- Retryparallelism\n",
                            ),
                            MemoryDocument(
                                "agent_experience/entries/exp-1.md",
                                "# Experience\nExperience leaf detail\n",
                            ),
                        ),
                    ),
                    body_kind="artifact_tree",
                    intent_kind="agent_experience",
                    domain_payload_json="{}",
                ),
            )
    materialize_artifact_revision(artifact_root=artifact_root, prepared=prepared)
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ) as connection:
        with immediate_transaction(connection):
            return activate_artifact_revision(
                connection=connection,
                prepared=prepared,
            )


def _publish_second_fact_revision(
    *, db_path: Path, artifact_root: Path
) -> MemoryRevision:
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ) as connection:
        with immediate_transaction(connection):
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="fact",
                source_record_id="fact-1",
            )
    draft = create_memory_draft(
        user_id="user-1",
        owner_node_id=node.node_id,
        base_revision_id=node.current_revision_id,
        documents=(
            MemoryDocument("facts/index.md", "# Facts\n- [New](new.md)\n"),
            MemoryDocument("facts/new.md", "# New\nCurrent marker\n"),
        ),
    )
    return publish_fact_artifact(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        publication=FactArtifactPublication(
            memory_run=_memory_run(),
            user_id="user-1",
            fact_id="fact-1",
            draft=draft,
            profile_brief="new brief",
            prompt_name="memory_update",
            prompt_version="1.0",
            source_insight_ids=(),
            created_at="2026-08-09T00:00:00Z",
        ),
    )


@pytest.mark.parametrize("has_direction", [False, True])
def test_todo_file_and_full_read_do_not_invent_long_term_context(
    tmp_path: Path,
    has_direction: bool,
) -> None:
    db_path, artifact_root = _runtime(tmp_path)
    todo = (
        "# Pending work\n"
        + "- Observed: a concrete pending commitment.\n" * 180
        + "- Other project: submit the estimate.\n"
    )
    _publish_insight_tree(
        db_path=db_path,
        artifact_root=artifact_root,
        documents=(
            MemoryDocument(
                "insights/index.md",
                "# Direction\nBuild sustainably.\n" if has_direction else "",
            ),
            MemoryDocument("insights/todos.md", todo),
        ),
    )
    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)
    assert snapshot.stable_memory.has_insights is has_direction
    # A file within the bound reaches the prompt whole.
    assert snapshot.stable_memory.pending_work == todo.strip()
    reader = ReadOnlyFileAccess(roots=snapshot.roots)
    full = reader.read(
        root_id="insights", path="insights/todos.md", offset=1, column=1, limit=200
    )
    assert full["truncated"] is True
    remainder = reader.read(
        root_id="insights",
        path="insights/todos.md",
        offset=full["next_offset"],
        column=full["next_column"],
        limit=200,
    )
    assert remainder["truncated"] is False
    assert full["content"] + remainder["content"] == todo
    assert "Other project: submit the estimate." in remainder["content"]


def test_oversized_todo_file_is_cut_and_does_not_block_the_suggestion_prompt(
    tmp_path: Path,
) -> None:
    from pantaray_agents.agents.suggestion_agent import SuggestionAgent
    from pantaray_agents.agents.suggestion_agent.agent import (
        SUGGESTION_INITIAL_PROMPT_MAX_CHARS,
    )
    from pantaray_agents.mock.mock_agent_repository import (
        MockSuggestionAgentRepository,
    )
    from pantaray_agents.mock.mock_llm_client import MockLLMClient
    from pantaray_agents.mock.suggestion_research import (
        build_mock_suggestion_research_tools,
    )

    db_path, artifact_root = _runtime(tmp_path)
    todo = "# TODOs\n" + "- **Item**: next step and evidence.\n" * 5_000
    _publish_insight_tree(
        db_path=db_path,
        artifact_root=artifact_root,
        documents=(
            MemoryDocument("insights/index.md", "# Direction\n"),
            MemoryDocument("insights/todos.md", todo),
        ),
    )
    snapshot = _snapshot(db_path=db_path, artifact_root=artifact_root)
    pending = snapshot.stable_memory.pending_work
    assert len(pending) <= snapshot_module.PENDING_WORK_MAX_CHARS < len(todo)
    assert pending.endswith("[truncated; find the rest with memory_search]")

    agent = SuggestionAgent(
        config={"llm_client": MockLLMClient()},
        repository=MockSuggestionAgentRepository(),
        research_tools=build_mock_suggestion_research_tools(),
        stable_memory=snapshot.stable_memory,
    )
    # The rest of the prompt near the old 64k budget, which no real run reached.
    prompt = agent._build_prompt(  # noqa: SLF001
        {
            "short_term_insight": "insight",
            "reconsideration_reason": "reason",
            "stable_memory_context": snapshot.stable_memory.prompt,
            "action_agent_capabilities": "capabilities",
            "recent_suggestions": "suggestions",
            "recent_activity_descriptions": "activities",
            "recent_activity_summary_1h": "hourly",
            "recent_activity_summaries_24h_1w_1m": "summaries",
            "context_density_signal": "context_density: high",
            "workspace_context_prompt": "w" * 50_000,
        }
    )
    assert len(prompt) <= SUGGESTION_INITIAL_PROMPT_MAX_CHARS
    assert "[truncated; find the rest with memory_search]" in prompt


_NO_SUGGESTION = {
    "has_suggestion": False,
    "interaction_contract": None,
    "key_point": "",
    "suggestion_summary": None,
    "target_context": None,
    "candidates": [],
}


@pytest.mark.asyncio
async def test_a_suggestion_run_reads_back_a_spilled_folder_listing(
    tmp_path: Path,
) -> None:
    from pantaray_agents.agents.artifact_react import ReactLoopStep
    from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
    from pantaray_agents.agents.suggestion_agent.output import (
        parse_suggestion_output,
    )
    from pantaray_agents.agents.suggestion_agent.react import (
        SUBMIT_SUGGESTION_TOOL_NAME,
        run_suggestion_react,
    )
    from pantaray_llm.contracts.tool_use import LlmToolCall

    db_path = _bootstrap_db(tmp_path)
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    for index in range(400):
        (root / f"meeting-notes-with-a-long-name-{index:04}.md").write_text("x")
    _register_workspace(db_path=db_path, root=root)
    tool_outputs: list[Any] = []
    turns = iter(
        (
            ("list", lambda: {"path": str(root), "limit": 500}),
            ("read", lambda: {"path": tool_outputs[0]["path"], "offset": 2}),
            (SUBMIT_SUGGESTION_TOOL_NAME, lambda: _NO_SUGGESTION),
        )
    )

    async def generate_tool_call(**_kwargs) -> LlmToolCallTurn:  # noqa: ANN003
        name, arguments = next(turns)
        call = LlmToolCall(call_id=name, name=name, arguments=arguments())
        return LlmToolCallTurn(calls=(call,), continuation=None)

    async def record_step(step: ReactLoopStep) -> None:
        if step.step_kind == "tool" and step.status == "success":
            tool_outputs.append(step.tool_output)

    result = await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-1",
        initial_prompt="context",
        system_instruction="system",
        research_tools=LocalSuggestionResearchTools(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            snapshot=_snapshot(db_path=db_path),
            activity_start=None,
        ),
        generate_tool_call=generate_tool_call,
        parse_output=parse_suggestion_output,
        record_step=record_step,
        discard_llm_thoughts=lambda: None,
    )

    assert result["has_suggestion"] is False

    listing, page = tool_outputs[:2]
    assert listing["storage"] == "action_file"
    assert "meeting-notes-with-a-long-name-0000.md" in page["content"]
