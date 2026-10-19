from __future__ import annotations

import pytest

from pantaray_agents.local_runtime.integrations.classification import (
    ToolDisposition,
    classify_tool,
)
from pantaray_agents.local_runtime.integrations.contracts import ClassOverride

SLUG = "GMAIL_FETCH_EMAILS"


@pytest.mark.parametrize(
    ("metadata_read_only", "override", "expected"),
    [
        # Without an override, only positive read-only metadata makes a read tool.
        (True, None, "read"),
        (False, None, "write"),
        # Overrides lower a read tool.
        (True, "write", "write"),
        (True, "blocked", "blocked"),
        # Overrides never raise a write tool, and can still block it.
        (False, "write", "write"),
        (False, "blocked", "blocked"),
    ],
)
def test_classification_leans_to_the_safe_side(
    metadata_read_only: bool,
    override: ClassOverride | None,
    expected: ToolDisposition,
) -> None:
    overrides: dict[str, ClassOverride] = {} if override is None else {SLUG: override}

    assert (
        classify_tool(
            SLUG, metadata_read_only=metadata_read_only, class_overrides=overrides
        )
        == expected
    )


def test_an_override_for_another_tool_does_not_apply() -> None:
    assert (
        classify_tool(
            SLUG,
            metadata_read_only=True,
            class_overrides={"GMAIL_SEND_EMAIL": "blocked"},
        )
        == "read"
    )
