from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    require_reference_source,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryContextExpiredError,
    MemoryReferenceDepthError,
    MemoryReferenceInputError,
    MemoryReferenceNotFoundError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemorySearchResult,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    follow_memory_reference,
)
from pantaray_agents.local_runtime.memory_catalog.search_policy import (
    MemorySearchFocus,
    parse_memory_search_focus,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MemorySearchRequest,
    execute_memory_search,
    memory_search_notes,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.memory_embeddings import (
    MEMORY_SEARCH_SEMANTIC_STATUS_VALUES,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
    tool_error_response,
)
from pantaray_agents.utils.local_time import describe_utc_timestamp

MEMORY_SEARCH_TOOL_NAME = "memory_search"
GET_MEMORY_REFERENCE_TOOL_NAME = "get_memory_reference"
# A search sets the run's context epoch, which a reference reads.
_CONTEXT_EPOCH = ToolConcurrency("parallel", shared_state="memory_context_epoch")


@dataclass(slots=True)
class MemoryContextSession:
    user_id: str
    run_id: str
    epoch: MemoryContextEpoch | None = None

    def __post_init__(self) -> None:
        if not self.user_id.strip() or not self.run_id.strip():
            raise ValueError("memory context user_id and run_id must not be empty")
        if self.epoch is not None and (
            self.epoch.user_id != self.user_id or self.epoch.run_id != self.run_id
        ):
            raise ValueError("memory context epoch belongs to another user or run")

    def require_epoch(self) -> MemoryContextEpoch:
        if self.epoch is None:
            raise MemoryContextExpiredError(
                "Run memory_search before using a memory context handle."
            )
        return self.epoch


@dataclass(frozen=True, slots=True)
class MemoryRetrievalPolicy:
    allowed_focuses: tuple[MemorySearchFocus, ...]
    default_focus: MemorySearchFocus | None
    max_results: int
    default_limit: int | None
    call_limit: int | None
    pinned_revisions: Mapping[MemorySource, str | None] | None
    search_content_max_chars: int | None
    reference_content_max_chars: int | None
    enqueue_repair_on_reference_failure: bool

    def __post_init__(self) -> None:
        if not self.allowed_focuses or len(set(self.allowed_focuses)) != len(
            self.allowed_focuses
        ):
            raise ValueError(
                "allowed memory search focuses must be unique and non-empty"
            )
        if (
            self.default_focus is not None
            and self.default_focus not in self.allowed_focuses
        ):
            raise ValueError("default memory search focus must be allowed")
        if self.max_results < 1:
            raise ValueError("memory search max_results must be positive")
        if (
            self.default_limit is not None
            and not 1 <= self.default_limit <= self.max_results
        ):
            raise ValueError("default memory search limit must be within max_results")
        if self.call_limit is not None and self.call_limit < 1:
            raise ValueError("memory retrieval call_limit must be positive")
        for value in (
            self.search_content_max_chars,
            self.reference_content_max_chars,
        ):
            if value is not None and value < 1:
                raise ValueError("memory retrieval content limits must be positive")


MEMORY_EDITOR_RETRIEVAL_POLICY = MemoryRetrievalPolicy(
    allowed_focuses=("all",),
    default_focus="all",
    max_results=8,
    default_limit=8,
    call_limit=6,
    pinned_revisions=None,
    search_content_max_chars=None,
    reference_content_max_chars=None,
    enqueue_repair_on_reference_failure=True,
)


@dataclass(slots=True)
class MemoryRetrievalSession:
    db_path: Path
    busy_timeout_ms: int
    context: MemoryContextSession
    policy: MemoryRetrievalPolicy
    calls_used: int = 0

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        search_description = (
            "Search persisted memory and return matching content with stable "
            "context handles. Words are matched from three characters; below "
            "that, single characters are not matched, and two-letter ASCII words "
            "only whole and when written in capitals (PR, UI) or with a digit "
            "(#7, v2). notes in the result name any query term that was not "
            "matched and say when the result list was full."
        )
        if self.policy.search_content_max_chars is not None:
            search_description += (
                " A truncated preview cannot be expanded in this run; it is "
                "all of the fragment that is available."
            )
        return (
            ReactToolDefinition(
                name=MEMORY_SEARCH_TOOL_NAME,
                description=search_description,
                request_schema=self._search_request_schema(),
                response_schema=react_tool_response_schema(
                    success_schema=_search_success_schema()
                ),
                execute=self.search,
                concurrency=_CONTEXT_EPOCH,
            ),
            ReactToolDefinition(
                name=GET_MEMORY_REFERENCE_TOOL_NAME,
                description=(
                    "Follow one explicit [[ref:...]] from visible memory context. "
                    "Reference traversal is limited to one hop."
                ),
                request_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_handle", "local_ref_id"],
                    "properties": {
                        "source_handle": {"type": "string", "minLength": 1},
                        "local_ref_id": {"type": "string", "minLength": 1},
                    },
                },
                response_schema=react_tool_response_schema(
                    success_schema=_reference_success_schema()
                ),
                execute=self.follow_reference,
                concurrency=_CONTEXT_EPOCH,
            ),
        )

    async def search(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        if not self._consume_call():
            return self._budget_error(call.tool_name)
        args = _arguments(call)
        try:
            focus = self._focus(args)
            limit = self._limit(args)
            query = _required_string(args, "query")
        except ValueError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_SEARCH_INVALID",
                message=str(exc),
            )
        response = await execute_memory_search(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            request=MemorySearchRequest(
                user_id=self.context.user_id,
                run_id=self.context.run_id,
                query=query,
                focus=focus,
                limit=limit,
                pinned_revisions=self.policy.pinned_revisions,
                current_epoch=self.context.epoch,
            ),
        )
        results: list[JSONValue] = [
            self._present_search_result(row) for row in response.results
        ]
        self.context.epoch = response.epoch
        return _success(
            call.tool_name,
            {
                "status": "success",
                "results": results,
                "semantic_status": response.semantic_status,
                "semantic_error_code": response.semantic_error_code,
                "notes": list[JSONValue](
                    memory_search_notes(
                        response, limit=limit, max_limit=self.policy.max_results
                    )
                ),
                "calls_remaining": self._calls_remaining,
            },
        )

    async def follow_reference(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        if not self._consume_call():
            return self._budget_error(call.tool_name)
        args = _arguments(call)
        try:
            epoch = self.context.require_epoch()
            source_handle = _required_string(args, "source_handle")
            require_reference_source(epoch=epoch, context_handle=source_handle)
            with open_memory_catalog_connection(
                db_path=self.db_path,
                busy_timeout_ms=self.busy_timeout_ms,
            ) as connection:
                epoch, resolved = follow_memory_reference(
                    connection=connection,
                    epoch=epoch,
                    source_handle=source_handle,
                    local_ref_id=_required_string(args, "local_ref_id"),
                    enqueue_repair_on_failure=(
                        self.policy.enqueue_repair_on_reference_failure
                    ),
                )
            reference = self._present_reference(cast(dict[str, JSONValue], resolved))
        except MemoryReferenceDepthError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_REFERENCE_DEPTH_EXCEEDED",
                message=str(exc),
            )
        except (
            MemoryContextExpiredError,
            MemoryReferenceInputError,
            MemoryReferenceNotFoundError,
        ) as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=type(exc).__name__,
                message=str(exc),
            )
        self.context.epoch = epoch
        return _success(
            call.tool_name,
            {
                "status": "success",
                "reference": reference,
                "calls_remaining": self._calls_remaining,
            },
        )

    def _search_request_schema(self) -> dict[str, JSONValue]:
        required: list[JSONValue] = ["query"]
        if self.policy.default_focus is None:
            required.append("focus")
        if self.policy.default_limit is None:
            required.append("limit")
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {
                "query": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "focus": {
                    "type": "string",
                    "enum": list(self.policy.allowed_focuses),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self.policy.max_results,
                },
            },
        }

    def _focus(self, args: dict[str, JSONValue]) -> MemorySearchFocus:
        raw_focus = args.get("focus")
        if raw_focus is None:
            if self.policy.default_focus is None:
                raise ValueError("focus is required")
            return self.policy.default_focus
        if not isinstance(raw_focus, str):
            raise ValueError("focus must be a string")
        focus = parse_memory_search_focus(raw_focus)
        if focus not in self.policy.allowed_focuses:
            raise ValueError("memory search focus is not allowed")
        return focus

    def _limit(self, args: dict[str, JSONValue]) -> int:
        raw_limit = args.get("limit")
        if raw_limit is None:
            if self.policy.default_limit is None:
                raise ValueError("limit is required")
            return self.policy.default_limit
        if (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or not 1 <= raw_limit <= self.policy.max_results
        ):
            raise ValueError(
                f"limit must be an integer between 1 and {self.policy.max_results}"
            )
        limit: int = raw_limit
        return limit

    def _present_search_result(self, row: MemorySearchResult) -> dict[str, JSONValue]:
        context_handle = row.get("context_handle")
        if not isinstance(context_handle, str) or not context_handle.strip():
            raise ValueError("memory search result has no context handle")
        source = row["source"]
        raw_content = str(row.get("content") or "")
        content = _bounded(raw_content, self.policy.search_content_max_chars)
        return {
            "source": source,
            "root": _memory_root(source),
            "record_id": str(row.get("record_id") or ""),
            "content": content,
            "content_truncated": len(content) < len(raw_content),
            "source_path": str(row.get("source_path") or ""),
            "heading_path": _optional_string(row.get("heading_path")),
            "observed_at": describe_utc_timestamp(row["observed_at"]),
            "match_kind": row["match_kind"],
            "context_handle": context_handle,
        }

    def _present_reference(
        self, resolved: dict[str, JSONValue]
    ) -> dict[str, JSONValue]:
        raw_content = str(resolved.get("target_content") or "")
        content = _bounded(raw_content, self.policy.reference_content_max_chars)
        reference = dict(resolved)
        reference["target_content"] = content
        reference["target_content_truncated"] = len(content) < len(raw_content)
        reference["target_root"] = _memory_root(
            _required_string(resolved, "target_source")
        )
        return reference

    @property
    def _calls_remaining(self) -> int | None:
        if self.policy.call_limit is None:
            return None
        return self.policy.call_limit - self.calls_used

    def _consume_call(self) -> bool:
        if (
            self.policy.call_limit is not None
            and self.calls_used >= self.policy.call_limit
        ):
            return False
        self.calls_used += 1
        return True

    def _budget_error(self, tool_name: str) -> ReactToolResult:
        return tool_error_response(
            tool_name=tool_name,
            error_code="MEMORY_RETRIEVAL_LIMIT_REACHED",
            message=(
                f"the combined memory retrieval limit of {self.policy.call_limit} "
                "calls was reached"
            ),
        )


def _arguments(call: ReactToolCall) -> dict[str, JSONValue]:
    if not isinstance(call.tool_args, dict):
        raise ValueError("tool arguments must be an object")
    return call.tool_args


def _required_string(args: dict[str, JSONValue], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: JSONValue) -> str | None:
    return str(value) if value is not None else None


def _bounded(content: str, max_chars: int | None) -> str:
    return content if max_chars is None else content[:max_chars]


def _memory_root(source: str) -> str | None:
    if source in {"fact", "facts"}:
        return "facts"
    if source == "long_term_insight":
        return "insights"
    if source == "agent_experience":
        return "agent_experience"
    return None


def _success(tool_name: str, output: dict[str, JSONValue]) -> ReactToolResult:
    return ReactToolResult(tool_name=tool_name, status="success", output=output)


def _search_success_schema() -> dict[str, JSONValue]:
    nullable_string: dict[str, JSONValue] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "results",
            "semantic_status",
            "semantic_error_code",
            "notes",
            "calls_remaining",
        ],
        "properties": {
            "status": {"const": "success"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "source",
                        "root",
                        "record_id",
                        "content",
                        "content_truncated",
                        "source_path",
                        "heading_path",
                        "observed_at",
                        "match_kind",
                        "context_handle",
                    ],
                    "properties": {
                        "source": {"type": "string"},
                        "root": nullable_string,
                        "record_id": {"type": "string"},
                        "content": {"type": "string"},
                        "content_truncated": {"type": "boolean"},
                        "source_path": {"type": "string"},
                        "heading_path": nullable_string,
                        "observed_at": {"type": "string"},
                        "match_kind": {
                            "type": "string",
                            "enum": [
                                "exact",
                                "corroborated",
                                "lexical",
                                "semantic",
                            ],
                        },
                        "context_handle": {"type": "string", "minLength": 1},
                    },
                },
            },
            "semantic_status": {
                "type": "string",
                "enum": list(MEMORY_SEARCH_SEMANTIC_STATUS_VALUES),
            },
            "semantic_error_code": nullable_string,
            "notes": {"type": "array", "items": {"type": "string"}},
            "calls_remaining": {"type": ["integer", "null"], "minimum": 0},
        },
    }


def _reference_success_schema() -> dict[str, JSONValue]:
    nullable_string: dict[str, JSONValue] = {"type": ["string", "null"]}
    reference_properties: dict[str, JSONValue] = {
        "local_ref_id": {"type": "string"},
        "reference_note": {"type": "string"},
        "source_path": {"type": "string"},
        "source_heading_path": nullable_string,
        "target_fragment_id": {"type": "string"},
        "target_source": {"type": "string"},
        "target_content": {"type": "string"},
        "target_content_truncated": {"type": "boolean"},
        "target_path": {"type": "string"},
        "target_heading_path": nullable_string,
        "target_revision_id": {"type": "string"},
        "target_is_current": {"type": "boolean"},
        "target_lifecycle": {"type": "string"},
        "target_integrity": {"type": "string"},
        "current_target_revision_id": {"type": "string"},
        "target_context_handle": {"type": "string", "minLength": 1},
        "target_root": nullable_string,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "reference", "calls_remaining"],
        "properties": {
            "status": {"const": "success"},
            "reference": {
                "type": "object",
                "additionalProperties": False,
                "required": list(reference_properties),
                "properties": reference_properties,
            },
            "calls_remaining": {"type": ["integer", "null"], "minimum": 0},
        },
    }


__all__ = [
    "GET_MEMORY_REFERENCE_TOOL_NAME",
    "MEMORY_EDITOR_RETRIEVAL_POLICY",
    "MEMORY_SEARCH_TOOL_NAME",
    "MemoryContextSession",
    "MemoryRetrievalPolicy",
    "MemoryRetrievalSession",
]
