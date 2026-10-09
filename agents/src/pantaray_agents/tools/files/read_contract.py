"""The arguments and result of a read/list/glob/grep call, whoever makes it.

A caller checks who may read what, builds a ``ReadScope``, and hands the call's
arguments to the read and search code; what comes back is the result below,
which the caller then stores or shows in its own form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.schema.agent.base import JSONValue

DiscoveryTruncationReason = Literal[
    "limit",
    "timeout",
    "output_bytes",
    "line_length",
]
DISCOVERY_RESULT_LIMIT_MAX = 500
LIST_MAX_DEPTH = 6


class ReadToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, pattern=r"\S")
    offset: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=1)
    start_unit: int | None = Field(default=None, ge=1)


class ListToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, pattern=r"\S")
    max_depth: int = Field(default=2, ge=1, le=LIST_MAX_DEPTH)
    limit: int = Field(default=100, ge=1, le=DISCOVERY_RESULT_LIMIT_MAX)


class GlobToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_path: str = Field(min_length=1, pattern=r"\S")
    pattern: str = Field(min_length=1, pattern=r"\S")
    limit: int = Field(default=100, ge=1, le=DISCOVERY_RESULT_LIMIT_MAX)


class GrepToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_path: str = Field(min_length=1, pattern=r"\S")
    pattern: str = Field(min_length=1, pattern=r"\S")
    include_glob: str | None = Field(default=None, min_length=1, pattern=r"\S")
    max_matches: int = Field(default=100, ge=1, le=DISCOVERY_RESULT_LIMIT_MAX)


@dataclass(frozen=True, slots=True)
class ReadToolResult:
    """What one successful call found; every refusal is a ``BrokerPolicyError``.

    ``file_reference_paths`` are the returned files that lie in a registered
    folder, which a caller may remember as files this call looked at.
    """

    output: dict[str, JSONValue]
    search_text: str | None
    file_paths: tuple[str, ...]
    file_reference_paths: tuple[str, ...]
    attachments: tuple[dict[str, JSONValue], ...] = ()


__all__ = [
    "DISCOVERY_RESULT_LIMIT_MAX",
    "LIST_MAX_DEPTH",
    "DiscoveryTruncationReason",
    "GlobToolArgs",
    "GrepToolArgs",
    "ListToolArgs",
    "ReadToolArgs",
    "ReadToolResult",
]
