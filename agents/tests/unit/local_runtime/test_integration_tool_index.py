from __future__ import annotations

from collections.abc import Iterator

import pytest

from pantaray_agents.local_runtime.integrations.contracts import (
    IntegrationTool,
    ToolClass,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_gateway,
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.local_runtime.integrations.tool_index import ToolIndex


def _tool(
    slug: str, description: str, *, tool_class: ToolClass = "read"
) -> IntegrationTool:
    toolkit = slug.split("_", 1)[0].lower()
    return IntegrationTool(
        slug=slug,
        toolkit=toolkit,
        name=slug.split("_", 1)[1].replace("_", " ").capitalize(),
        description=description,
        input_schema={"type": "object", "properties": {}},
        tool_class=tool_class,
    )


GMAIL_TOOLS = [
    _tool("GMAIL_FETCH_EMAILS", "Fetch a list of emails from the Gmail inbox."),
    _tool(
        "GMAIL_SEND_EMAIL",
        "Send an email to one or more recipients.",
        tool_class="write",
    ),
    _tool("GMAIL_LIST_LABELS", "List all labels in the user's mailbox."),
    _tool("GMAIL_GET_CONTACTS", "Get contacts with their email address."),
]
GITHUB_TOOLS = [
    _tool(
        "GITHUB_CREATE_AN_ISSUE",
        "Create a new issue in a GitHub repository.",
        tool_class="write",
    ),
    _tool(
        "GITHUB_CREATE_A_PULL_REQUEST",
        "Create a pull request in a GitHub repository.",
        tool_class="write",
    ),
    _tool("GITHUB_LIST_REPOSITORY_ISSUES", "List issues in a GitHub repository."),
]


def _all(_: IntegrationTool) -> bool:
    return True


@pytest.fixture
def index() -> ToolIndex:
    index = ToolIndex()
    index.replace("gmail", GMAIL_TOOLS)
    index.replace("github", GITHUB_TOOLS)
    return index


def _slugs(tools: list[IntegrationTool]) -> list[str]:
    return [tool.slug for tool in tools]


def test_the_tool_named_by_the_query_ranks_first(index: ToolIndex) -> None:
    assert _slugs(index.search("send email", usable=_all))[:2] == [
        "GMAIL_SEND_EMAIL",
        "GMAIL_FETCH_EMAILS",
    ]
    assert _slugs(index.search("fetch emails", usable=_all))[0] == "GMAIL_FETCH_EMAILS"
    # "github" is in every GitHub tool, so "issue" decides the order.
    assert _slugs(index.search("create github issue", usable=_all))[0] == (
        "GITHUB_CREATE_AN_ISSUE"
    )


def test_a_word_only_in_the_description_still_finds_the_tool(index: ToolIndex) -> None:
    assert _slugs(index.search("mailbox", usable=_all)) == ["GMAIL_LIST_LABELS"]


def test_no_shared_word_returns_nothing(index: ToolIndex) -> None:
    assert index.search("calendar event", usable=_all) == []


def test_toolkit_filter_and_limit(index: ToolIndex) -> None:
    assert _slugs(index.search("list", toolkit="github", usable=_all)) == [
        "GITHUB_LIST_REPOSITORY_ISSUES"
    ]
    assert len(index.search("create list issue email", usable=_all, limit=2)) == 2


def test_unusable_tools_never_take_a_place(index: ToolIndex) -> None:
    def read_only(tool: IntegrationTool) -> bool:
        return tool.tool_class == "read"

    assert _slugs(index.search("send email", usable=read_only, limit=1)) == [
        "GMAIL_FETCH_EMAILS"
    ]


def test_refresh_replaces_the_toolkit_wholesale(index: ToolIndex) -> None:
    index.replace("gmail", GMAIL_TOOLS[:1])

    assert index.get("GMAIL_SEND_EMAIL") is None
    assert index.get("GMAIL_FETCH_EMAILS") == GMAIL_TOOLS[0]
    assert index.get("GITHUB_CREATE_AN_ISSUE") == GITHUB_TOOLS[0]

    index.replace("gmail", [])
    assert index.search("email", usable=_all) == []


@pytest.fixture
def no_gateway() -> Iterator[None]:
    set_integration_gateway(None)
    yield
    set_integration_gateway(None)


@pytest.mark.usefixtures("no_gateway")
def test_registering_a_gateway_drops_the_previous_tools() -> None:
    assert get_integration_gateway() is None
    get_integration_tool_index().replace("gmail", GMAIL_TOOLS)

    gateway = object()
    set_integration_gateway(gateway)  # type: ignore[arg-type]

    assert get_integration_gateway() is gateway
    assert get_integration_tool_index().get("GMAIL_SEND_EMAIL") is None
