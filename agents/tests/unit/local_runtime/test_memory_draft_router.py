from __future__ import annotations

import pytest

from pantaray_agents.agents.memory_file_editor.tools import (
    APPLY_PATCH_TOOL_NAME,
    DELETE_MEMORY_FILE_TOOL_NAME,
    LINK_MEMORY_TOOL_NAME,
    MOVE_MEMORY_FILE_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    UNLINK_MEMORY_TOOL_NAME,
    WRITE_FILE_TOOL_NAME,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AgentExperienceContent,
    AgentExperienceScope,
    render_agent_experience_markdown,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.models import (
    DraftLink,
    MemoryDocument,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorTools,
    MemoryDraftRoute,
    MemoryDraftRouter,
    MemoryDraftToolSession,
)
from pantaray_agents.local_runtime.tooling.memory_retrieval import MemoryContextSession
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

USER_ID = "user-1"
RUN_ID = "run-1"


def _session(*, root: str, path: str, content: str) -> MemoryDraftToolSession:
    return MemoryDraftToolSession(
        editable_policy=EditablePathPolicy((f"{root}/**",)),
        draft=create_memory_draft(
            user_id=USER_ID,
            owner_node_id=f"node-{root}",
            base_revision_id=None,
            documents=(MemoryDocument(path, content),),
        ),
        memory_context=MemoryContextSession(user_id=USER_ID, run_id=RUN_ID),
    )


def _router() -> MemoryDraftRouter:
    return MemoryDraftRouter(
        routes=(
            MemoryDraftRoute(
                source="fact",
                root="facts",
                session=_session(
                    root="facts", path="facts/index.md", content="# Facts\n"
                ),
            ),
            MemoryDraftRoute(
                source="long_term_insight",
                root="insights",
                session=_session(
                    root="insights", path="insights/index.md", content="# Insights\n"
                ),
            ),
        )
    )


def _call(tool_name: str, args: dict[str, JSONValue]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=tool_name, reason=None, args=args),
    )


async def _execute(
    router: MemoryDraftRouter, tool_name: str, args: dict[str, JSONValue]
) -> dict[str, JSONValue]:
    definition = next(item for item in router.definitions() if item.name == tool_name)
    result = await definition.execute(_call(tool_name, args), 1)
    assert isinstance(result.output, dict)
    return result.output


def test_router_exposes_one_tool_per_name_over_every_root() -> None:
    router = _router()
    names = tuple(definition.name for definition in router.definitions())
    assert len(set(names)) == len(names)
    assert router.editable_policy.allowed_globs == ("facts/**", "insights/**")
    assert tuple(document.source_path for document in router.draft.documents) == (
        "facts/index.md",
        "insights/index.md",
    )


@pytest.mark.asyncio
async def test_patch_to_one_root_leaves_the_other_draft_untouched() -> None:
    router = _router()
    insights_before = router.routes[1].session.draft
    tools = LocalMemoryFileEditorTools(
        editable_policy=router.editable_policy,
        memory_session=router,
    )
    read = await tools.read_file(
        _call(READ_FILE_TOOL_NAME, {"root": "memory_draft", "path": "facts/index.md"}),
        1,
    )
    assert read.status == "success"
    patched = await tools.apply_patch(
        _call(
            APPLY_PATCH_TOOL_NAME,
            {
                "path": "facts/index.md",
                "chunks": [
                    {
                        "lines": [
                            {"op": "context", "text": "# Facts"},
                            {"op": "add", "text": "- lives in Tokyo"},
                        ]
                    }
                ],
            },
        ),
        2,
    )
    assert patched.status == "success"
    assert isinstance(patched.output, dict)
    assert patched.output["changed"] is True
    assert "lives in Tokyo" in router.routes[0].session.draft.documents[0].content
    assert router.routes[1].session.draft is insights_before
    assert patched.output["draft_revision"] == router.draft.draft_revision


@pytest.mark.asyncio
async def test_move_between_memory_categories_is_refused() -> None:
    router = _router()
    output = await _execute(
        router,
        MOVE_MEMORY_FILE_TOOL_NAME,
        {
            "source_path": "facts/index.md",
            "destination_path": "insights/index.md",
            "expected_draft_revision": router.draft.draft_revision,
        },
    )
    assert output["status"] == "error"
    assert output["error_code"] == "MEMORY_DRAFT_ROUTE_INVALID"


@pytest.mark.asyncio
async def test_stale_combined_revision_is_refused() -> None:
    router = _router()
    output = await _execute(
        router,
        DELETE_MEMORY_FILE_TOOL_NAME,
        {
            "path": "facts/index.md",
            "expected_draft_revision": "sha256:" + "0" * 64,
        },
    )
    assert output["status"] == "error"
    assert output["error_code"] == "MEMORY_DRAFT_REVISION_STALE"


_EXPERIENCE_ENTRY_PATH = "agent_experience/entries/entry-001.md"
_EXPERIENCE_EVIDENCE_REF = "ref_action_1"


def _router_with_agent_experience() -> MemoryDraftRouter:
    entry = (
        render_agent_experience_markdown(
            AgentExperienceContent(
                experience_id="entry-001",
                scope=AgentExperienceScope(kind="user", key=None),
                applies_when="Running tests",
                observed_approach="Used the project interpreter",
                outcome="effective",
                next_time_rule="Use the project interpreter",
                observed_result="Tests passed",
                supersedes_experience_id=None,
            )
        )
        + f"- Evidence: Action action-1 [[ref:{_EXPERIENCE_EVIDENCE_REF} "
        'note:"source action"]]\n'
    )
    session = MemoryDraftToolSession(
        editable_policy=EditablePathPolicy(("agent_experience/entries/*.md",)),
        draft=create_memory_draft(
            user_id=USER_ID,
            owner_node_id="node-agent_experience",
            base_revision_id=None,
            documents=(MemoryDocument(_EXPERIENCE_ENTRY_PATH, entry),),
            carried_links=(
                DraftLink(
                    local_ref_id=_EXPERIENCE_EVIDENCE_REF,
                    target_fragment_id="fragment-1",
                    source_path=_EXPERIENCE_ENTRY_PATH,
                    source_anchor_text="- Evidence: Action action-1",
                    source_anchor_occurrence=1,
                    reference_note="source action",
                    created_at="2026-01-05T00:00:00Z",
                    state="active",
                ),
            ),
        ),
        memory_context=MemoryContextSession(user_id=USER_ID, run_id=RUN_ID),
    )
    return MemoryDraftRouter(
        routes=(
            *_router().routes,
            MemoryDraftRoute(
                source="agent_experience",
                root="agent_experience",
                session=session,
            ),
        )
    )


@pytest.mark.asyncio
async def test_linking_inside_agent_experience_is_refused() -> None:
    router = _router_with_agent_experience()

    output = await _execute(
        router,
        LINK_MEMORY_TOOL_NAME,
        {
            "source_path": _EXPERIENCE_ENTRY_PATH,
            "target_handle": "ctx_1",
            "exact_text": "# Agent Experience",
            "occurrence": 1,
            "note": "source action",
            "expected_draft_revision": router.draft.draft_revision,
        },
    )

    assert output["status"] == "error"
    assert output["error_code"] == "MEMORY_DRAFT_ROUTE_INVALID"


@pytest.mark.asyncio
async def test_unlinking_a_host_written_evidence_ref_is_refused() -> None:
    router = _router_with_agent_experience()

    output = await _execute(
        router,
        UNLINK_MEMORY_TOOL_NAME,
        {
            "local_ref_id": _EXPERIENCE_EVIDENCE_REF,
            "expected_draft_revision": router.draft.draft_revision,
        },
    )

    assert output["status"] == "error"
    assert output["error_code"] == "MEMORY_DRAFT_ROUTE_INVALID"
    assert router.routes[2].session.draft.links[0].state == "active"


def test_route_policy_must_stay_under_its_own_root() -> None:
    session = MemoryDraftToolSession(
        editable_policy=EditablePathPolicy(("insights/**",)),
        draft=create_memory_draft(
            user_id=USER_ID,
            owner_node_id="node-facts",
            base_revision_id=None,
            documents=(MemoryDocument("insights/index.md", "# Insights\n"),),
        ),
        memory_context=MemoryContextSession(user_id=USER_ID, run_id=RUN_ID),
    )
    with pytest.raises(ValueError, match="own root"):
        MemoryDraftRouter(
            routes=(MemoryDraftRoute(source="fact", root="facts", session=session),)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit", ["remove", "change_source", "move_tag", "add_spacing", "edit_lesson"]
)
async def test_patch_preserves_host_evidence_while_allowing_lesson_edits(
    edit: str,
) -> None:
    router = _router_with_agent_experience()
    before = router.routes[2].session.draft
    lines = before.documents[0].content.splitlines()
    old = (
        lines[-1]
        if edit != "edit_lesson"
        else '- Next-time rule: "Use the project interpreter"'
    )
    new = "" if edit == "remove" else old.replace("action-1", "action-2")
    if edit == "move_tag":
        new = old.replace("Action action-1 ", "Action ") + " action-1"
    elif edit == "add_spacing":
        new = old.replace("Action action-1 ", "Action action-1  ")
    if edit == "edit_lesson":
        new = '- Next-time rule: "Check the interpreter before running tests"'
    tools = LocalMemoryFileEditorTools(
        editable_policy=router.editable_policy, memory_session=router
    )
    await tools.read_file(
        _call(
            READ_FILE_TOOL_NAME,
            {"root": "memory_draft", "path": _EXPERIENCE_ENTRY_PATH},
        ),
        1,
    )
    result = await tools.apply_patch(
        _call(
            APPLY_PATCH_TOOL_NAME,
            {
                "path": _EXPERIENCE_ENTRY_PATH,
                "chunks": [
                    {
                        "lines": [
                            {"op": "remove", "text": old},
                            {"op": "add", "text": new},
                        ]
                    }
                ],
            },
        ),
        2,
    )
    if edit == "edit_lesson":
        assert result.output["status"] == "success"
        assert new in router.routes[2].session.draft.documents[0].content
        assert router.routes[2].session.draft.links == before.links
    else:
        assert result.output["error_code"] == "MEMORY_LINK_INVALID"
        assert "host-owned" in result.output["message"]
        assert router.routes[2].session.draft == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "problem"),
    [
        (APPLY_PATCH_TOOL_NAME, "not valid JSON: scope"),
        (WRITE_FILE_TOOL_NAME, "content is invalid: outcome"),
    ],
)
async def test_malformed_agent_experience_entry_is_a_tool_error(
    tool_name: str, problem: str
) -> None:
    router = _router_with_agent_experience()
    before = router.routes[2].session.draft
    tools = LocalMemoryFileEditorTools(
        editable_policy=router.editable_policy, memory_session=router
    )
    if tool_name == APPLY_PATCH_TOOL_NAME:
        await tools.read_file(
            _call(
                READ_FILE_TOOL_NAME,
                {"root": "memory_draft", "path": _EXPERIENCE_ENTRY_PATH},
            ),
            1,
        )
        result = await tools.apply_patch(
            _call(
                APPLY_PATCH_TOOL_NAME,
                {
                    "path": _EXPERIENCE_ENTRY_PATH,
                    "chunks": [
                        {
                            "lines": [
                                {
                                    "op": "remove",
                                    "text": '- Scope: {"kind":"user","key":null}',
                                },
                                {"op": "add", "text": "- Scope: {kind: user}"},
                            ]
                        }
                    ],
                },
            ),
            2,
        )
    else:
        entry = before.documents[0].content.splitlines()[:9]
        malformed = "\n".join(entry).replace('"effective"', '"verified"')
        result = await tools.write_file(
            _call(
                WRITE_FILE_TOOL_NAME,
                {
                    "path": "agent_experience/entries/entry-002.md",
                    "text": malformed.replace("entry-001", "entry-002") + "\n",
                },
            ),
            1,
        )

    assert result.status == "error"
    assert isinstance(result.output, dict)
    assert result.output["error_code"] == "MEMORY_CATALOG_INTEGRITY"
    assert problem in str(result.output["message"])
    assert router.routes[2].session.draft == before


@pytest.mark.asyncio
async def test_renaming_an_agent_experience_entry_is_refused() -> None:
    router = _router_with_agent_experience()
    before = router.routes[2].session.draft

    output = await _execute(
        router,
        MOVE_MEMORY_FILE_TOOL_NAME,
        {
            "source_path": _EXPERIENCE_ENTRY_PATH,
            "destination_path": "agent_experience/entries/entry-002.md",
            "expected_draft_revision": router.draft.draft_revision,
        },
    )

    assert output["error_code"] == "MEMORY_DRAFT_ROUTE_INVALID"
    assert router.routes[2].session.draft == before


@pytest.mark.asyncio
async def test_unlink_rejects_a_revision_local_id_owned_by_two_categories() -> None:
    router = _router()
    for route in router.routes:
        path = f"{route.root}/index.md"
        route.session.draft = create_memory_draft(
            user_id=USER_ID,
            owner_node_id=f"node-{route.root}",
            base_revision_id=None,
            documents=(
                MemoryDocument(path, '- Claim [[ref:ref_shared note:"evidence"]]\n'),
            ),
            carried_links=(
                DraftLink(
                    local_ref_id="ref_shared",
                    target_fragment_id="target",
                    source_path=path,
                    source_anchor_text="- Claim",
                    source_anchor_occurrence=1,
                    reference_note="evidence",
                    created_at="2026-01-01",
                    state="carried",
                ),
            ),
        )
    before = router.draft
    result = await _execute(
        router,
        UNLINK_MEMORY_TOOL_NAME,
        {
            "local_ref_id": "ref_shared",
            "expected_draft_revision": before.draft_revision,
        },
    )
    assert result["error_code"] == "MEMORY_DRAFT_ROUTE_INVALID"
    assert "ambiguous" in result["message"]
    assert router.draft == before
