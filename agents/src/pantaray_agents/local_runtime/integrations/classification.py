"""Classify an external tool as read, write or blocked, leaning to the safe side.

A read tool wrongly classified as write only adds a confirmation; a write tool
wrongly classified as read acts without one. So a tool is read only when the
connection's metadata positively says so, and the toolkit table's overrides
can only lower a tool, never raise it to read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from .contracts import ClassOverride, ToolClass

# 'blocked' is deliberately not a ToolClass: IntegrationTool cannot hold it, so a
# gateway must drop a blocked tool before it can reach the tool index.
type ToolDisposition = ToolClass | Literal["blocked"]


def classify_tool(
    slug: str,
    *,
    metadata_read_only: bool,
    class_overrides: Mapping[str, ClassOverride],
) -> ToolDisposition:
    override = class_overrides.get(slug)
    if override is not None:
        return override
    return "read" if metadata_read_only else "write"
