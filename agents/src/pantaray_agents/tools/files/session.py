from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pantaray_agents.local_runtime.memory_catalog.epoch import resolve_context_handle
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryContextExpiredError,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    BrokerPolicyError,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_agents.tools.files.text_lines import read_text_value_lines
from pantaray_agents.tools.memory.retrieval import (
    CONTEXT_ROOT_ID,
    MemoryContextSession,
)

from .access import ReadOnlyFileAccess
from .definitions import build_read_only_file_definitions
from .roots import ReadOnlyRoot

CONTEXT_READ_MAX_BYTES = 4_800


def _arguments(call: ReactToolCall) -> dict[str, JSONValue]:
    if not isinstance(call.tool_args, dict):
        raise AssertionError("validated tool arguments must be an object")
    return call.tool_args


def _string_arg(args: dict[str, JSONValue], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AssertionError(f"validated {name} must be a non-empty string")
    return value


def _integer_arg(args: dict[str, JSONValue], name: str) -> int:
    value = args.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError(f"validated {name} must be an integer")
    return value


def _optional_string_arg(args: dict[str, JSONValue], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AssertionError(f"validated {name} must be a non-empty string")
    return value


def _success(tool_name: str, output: dict[str, JSONValue]) -> ReactToolResult:
    return ReactToolResult(tool_name=tool_name, status="success", output=output)


def _expected_error(
    *, tool_name: str, error_code: str, error: Exception
) -> ReactToolResult:
    return tool_error_response(
        tool_name=tool_name,
        error_code=error_code,
        message=str(error) or type(error).__name__,
    )


@dataclass(frozen=True, slots=True)
class ReadOnlyFileToolSession:
    roots: tuple[ReadOnlyRoot, ...]
    user_id: str
    memory_context: MemoryContextSession | None = None
    _files: ReadOnlyFileAccess = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("read-only file session user_id must not be empty")
        if (
            self.memory_context is not None
            and self.memory_context.user_id != self.user_id
        ):
            raise ValueError("read-only file and memory contexts must share a user")
        object.__setattr__(self, "_files", ReadOnlyFileAccess(roots=self.roots))

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return build_read_only_file_definitions(
            read=self.read,
            list_paths=self.list_paths,
            glob=self.glob,
            grep=self.grep,
        )

    async def read(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = _arguments(call)
        try:
            root_id = _string_arg(args, "root")
            path = _string_arg(args, "path")
            offset = _integer_arg(args, "offset")
            column = _integer_arg(args, "column")
            limit = _integer_arg(args, "limit")
            if root_id == CONTEXT_ROOT_ID:
                if self.memory_context is None:
                    raise MemoryContextExpiredError(
                        "Run memory_search before reading a context handle."
                    )
                item = resolve_context_handle(
                    epoch=self.memory_context.require_epoch(),
                    user_id=self.user_id,
                    context_handle=path,
                )
                page = read_text_value_lines(
                    text=item.item.content,
                    offset=offset,
                    column=column,
                    limit=limit,
                    max_bytes=CONTEXT_READ_MAX_BYTES,
                )
                return _success(
                    call.tool_name,
                    {
                        "status": "success",
                        "root": root_id,
                        "path": path,
                        "content": page.content,
                        "offset": offset,
                        "column": column,
                        "end_line": page.end_line,
                        "end_column": page.end_column,
                        "total_lines": page.total_lines,
                        "next_offset": page.next_offset,
                        "next_column": page.next_column,
                        "truncated": page.truncated,
                        "truncation_reason": page.truncation_reason,
                        "retry_hint": page.retry_hint,
                    },
                )
            # A file read can stream far into a large file, so it stays off the
            # event loop like the search tools below.
            output = await asyncio.to_thread(
                self._files.read,
                root_id=root_id,
                path=path,
                offset=offset,
                column=column,
                limit=limit,
            )
            return _success(call.tool_name, output)
        except (
            ValueError,
            OSError,
            UnicodeError,
            BrokerPolicyError,
            MemoryContextExpiredError,
        ) as exc:
            return _expected_error(
                tool_name=call.tool_name,
                error_code="READ_FAILED",
                error=exc,
            )

    async def list_paths(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _arguments(call)
        try:
            output = await asyncio.to_thread(
                self._files.list,
                root_id=_string_arg(args, "root"),
                path=_string_arg(args, "path"),
                max_depth=_integer_arg(args, "max_depth"),
                offset=_integer_arg(args, "offset"),
                limit=_integer_arg(args, "limit"),
            )
            return _success(call.tool_name, output)
        except (ValueError, OSError, BrokerPolicyError) as exc:
            return _expected_error(
                tool_name=call.tool_name,
                error_code="LIST_FAILED",
                error=exc,
            )

    async def glob(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = _arguments(call)
        try:
            output = await asyncio.to_thread(
                self._files.glob,
                root_id=_string_arg(args, "root"),
                base_path=_string_arg(args, "base_path"),
                pattern=_string_arg(args, "pattern"),
                offset=_integer_arg(args, "offset"),
                limit=_integer_arg(args, "limit"),
            )
            return _success(call.tool_name, output)
        except (ValueError, OSError, BrokerPolicyError) as exc:
            return _expected_error(
                tool_name=call.tool_name,
                error_code="GLOB_FAILED",
                error=exc,
            )

    async def grep(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = _arguments(call)
        try:
            output = await asyncio.to_thread(
                self._files.grep,
                root_id=_string_arg(args, "root"),
                base_path=_string_arg(args, "base_path"),
                pattern=_string_arg(args, "pattern"),
                include_glob=_optional_string_arg(args, "include_glob"),
                offset=_integer_arg(args, "offset"),
                max_matches=_integer_arg(args, "max_matches"),
            )
            return _success(call.tool_name, output)
        except (ValueError, OSError, BrokerPolicyError) as exc:
            return _expected_error(
                tool_name=call.tool_name,
                error_code="GREP_FAILED",
                error=exc,
            )


__all__ = ["ReadOnlyFileToolSession"]
