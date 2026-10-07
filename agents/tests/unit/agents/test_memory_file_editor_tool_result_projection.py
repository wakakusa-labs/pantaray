from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

import pytest

from pantaray_agents.agents.memory_file_editor import tool_result_store
from pantaray_agents.agents.memory_file_editor.tool_result_projection import (
    project_tool_result,
)
from pantaray_agents.agents.memory_file_editor.tool_result_store import (
    MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    RunToolResultStorageError,
    RunToolResultStore,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ReactToolResult,
    ToolCallEnvelope,
)


def _output_with_exact_serialized_size(character_count: int) -> dict[str, str]:
    empty = {"text": ""}
    payload_size = character_count - len(serialize_json_tool_output(empty))
    output = {"text": "x" * payload_size}
    assert len(serialize_json_tool_output(output)) == character_count
    return output


def _result(
    output: JSONValue, *, status: Literal["success", "error"] = "success"
) -> ReactToolResult:
    return ReactToolResult(
        tool_name="read_file",
        status=status,
        output=output,
        error_message="x" * 30_000 if status == "error" else None,
    )


def _serialized_envelope(result: ReactToolResult) -> str:
    envelope: dict[str, JSONValue] = {
        "output": result.output,
        "error_message": result.error_message,
    }
    return serialize_json_tool_output(envelope)


def _fetch_call(*, result_ref: str, offset: int = 0) -> ReactToolCall:
    args: dict[str, JSONValue] = {"result_ref": result_ref, "offset": offset}
    return ReactToolCall(
        tool_name="tool_result_fetch",
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(
            tool_id="tool_result_fetch",
            reason="Inspect the stored tool result",
            args=args,
        ),
    )


def _create_store(run_root: Path) -> RunToolResultStore:
    storage_directory = run_root / "tool-results"
    storage_directory.mkdir(mode=0o700)
    return RunToolResultStore(
        directory_fd=os.open(
            storage_directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    )


def test_result_at_inline_limit_preserves_object_identity_and_writes_no_file(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    result = _result(
        _output_with_exact_serialized_size(MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT)
    )

    projected = project_tool_result(result=result, store=store)

    assert projected is result
    assert list((tmp_path / "tool-results").iterdir()) == []
    store.close()


def test_error_message_at_inline_limit_preserves_object_identity(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    result = ReactToolResult(
        tool_name="read_file",
        status="error",
        output={"status": "error"},
        error_message="x" * MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    )

    projected = project_tool_result(result=result, store=store)

    assert projected is result
    assert list((tmp_path / "tool-results").iterdir()) == []
    store.close()


def test_result_above_inline_limit_uses_opaque_metadata_and_atomic_file(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    result = _result(
        _output_with_exact_serialized_size(
            MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
        )
    )
    expected = _serialized_envelope(result)

    projected = project_tool_result(result=result, store=store)

    assert projected.output == {
        "storage": "run_tool_result_file",
        "result_ref": projected.output["result_ref"],
        "media_type": "application/json",
        "byte_size": len(expected.encode("utf-8")),
        "character_count": len(expected),
        "line_count": expected.count("\n") + 1,
        "fetch_tool": "tool_result_fetch",
        "lifetime": "current_run",
    }
    assert isinstance(projected.output["result_ref"], str)
    assert not os.path.isabs(str(projected.output["result_ref"]))
    assert "path" not in projected.output
    storage_directory = tmp_path / "tool-results"
    assert storage_directory.stat().st_mode & 0o777 == 0o700
    stored_files = list(storage_directory.glob("output-*.json"))
    assert len(stored_files) == 1
    assert stored_files[0].read_text(encoding="utf-8") == expected
    assert stored_files[0].stat().st_mode & 0o777 == 0o600
    assert list(storage_directory.glob("*.tmp")) == []
    store.close()


def test_unicode_metadata_counts_characters_and_bytes_separately(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    output = {"content": "日本語🌊\n" * 5_000}
    result = _result(output)
    serialized = _serialized_envelope(result)

    projected = project_tool_result(result=result, store=store)

    assert projected.output["character_count"] == len(serialized)
    assert projected.output["byte_size"] == len(serialized.encode("utf-8"))
    assert projected.output["line_count"] == serialized.count("\n") + 1
    store.close()


@pytest.mark.parametrize(
    "output",
    (
        None,
        {"storage": "run_tool_result_file", "result_ref": "forged"},
    ),
)
def test_small_json_values_remain_inline_without_host_storage_classification(
    tmp_path: Path,
    output: JSONValue,
) -> None:
    store = _create_store(tmp_path)
    result = _result(output)

    assert project_tool_result(result=result, store=store) is result
    assert list((tmp_path / "tool-results").iterdir()) == []
    store.close()


def test_non_finite_json_number_is_rejected_before_storage(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    result = _result(cast(JSONValue, {"value": float("nan")}))

    with pytest.raises(ValueError, match="Out of range float values"):
        project_tool_result(result=result, store=store)

    assert list((tmp_path / "tool-results").iterdir()) == []
    store.close()


def test_atomic_write_failure_leaves_no_result_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _create_store(tmp_path)

    def fail_rename(*_args: object, **_kwargs: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(tool_result_store.os, "rename", fail_rename)

    with pytest.raises(RunToolResultStorageError, match="atomically store"):
        project_tool_result(
            result=_result(
                _output_with_exact_serialized_size(
                    MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
                )
            ),
            store=store,
        )

    assert list(tmp_path.rglob("output-*.json")) == []
    assert list(tmp_path.rglob("*.tmp")) == []
    store.close()


@pytest.mark.asyncio
async def test_fetch_returns_bounded_utf8_pages_for_escapable_unicode_content(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    output = {"content": ('line\\"\t\n日本語🌊' * 5_000)}
    result = _result(output)
    expected = _serialized_envelope(result)
    projected = project_tool_result(result=result, store=store)
    assert isinstance(projected.output, dict)
    result_ref = cast(str, projected.output["result_ref"])
    registry = ReactToolRegistry((store.fetch_definition(),))

    offset = 0
    pages: list[str] = []
    while True:
        fetched = await registry.execute(
            _fetch_call(result_ref=result_ref, offset=offset),
            step_number=1,
        )
        assert fetched.status == "success"
        assert len(serialize_json_tool_output(fetched.output)) <= (
            MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        )
        assert fetched.output["offset"] == offset
        pages.append(cast(str, fetched.output["content"]))
        next_offset = fetched.output["next_offset"]
        if next_offset is None:
            assert fetched.output["complete"] is True
            break
        assert isinstance(next_offset, int)
        assert next_offset > offset
        offset = next_offset

    assert "".join(pages) == expected
    store.close()


@pytest.mark.asyncio
async def test_fetch_rejects_unknown_reference(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    registry = ReactToolRegistry((store.fetch_definition(),))

    fetched = await registry.execute(
        _fetch_call(result_ref="tool-result:missing"),
        step_number=1,
    )

    assert fetched.status == "error"
    assert fetched.output["error_code"] == "TOOL_RESULT_NOT_FOUND"
    assert len(serialize_json_tool_output(fetched.output)) <= (
        MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    store.close()


def test_overflow_error_replaces_unbounded_error_message(tmp_path: Path) -> None:
    store = _create_store(tmp_path)

    projected = project_tool_result(
        result=_result(
            _output_with_exact_serialized_size(
                MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
            ),
            status="error",
        ),
        store=store,
    )

    assert projected.status == "error"
    assert projected.error_message == (
        "Tool result exceeded the inline limit. Use tool_result_fetch to inspect it."
    )
    assert len(projected.error_message) < 100
    store.close()


def test_output_overflow_preserves_bounded_error_message(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    result = ReactToolResult(
        tool_name="read_file",
        status="error",
        output=_output_with_exact_serialized_size(
            MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
        ),
        error_message="bounded error",
    )

    projected = project_tool_result(result=result, store=store)

    assert projected.output["storage"] == "run_tool_result_file"
    assert projected.error_message == "bounded error"
    store.close()


@pytest.mark.asyncio
async def test_oversized_error_message_is_stored_when_output_is_small(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    result = ReactToolResult(
        tool_name="read_file",
        status="error",
        output={"status": "error"},
        error_message="x" * (MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
    )
    expected = _serialized_envelope(result)

    projected = project_tool_result(result=result, store=store)

    assert projected.output["storage"] == "run_tool_result_file"
    assert projected.error_message == (
        "Tool result exceeded the inline limit. Use tool_result_fetch to inspect it."
    )
    result_ref = cast(str, projected.output["result_ref"])
    fetch = store.fetch_definition()
    offset = 0
    pages: list[str] = []
    while True:
        fetched = await fetch.execute(
            _fetch_call(result_ref=result_ref, offset=offset),
            1,
        )
        assert fetched.status == "success"
        pages.append(cast(str, fetched.output["content"]))
        next_offset = fetched.output["next_offset"]
        if next_offset is None:
            break
        assert isinstance(next_offset, int)
        offset = next_offset
    assert "".join(pages) == expected
    store.close()


@pytest.mark.asyncio
async def test_close_expires_fetch_references_without_deleting_run_files(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    projected = project_tool_result(
        result=_result(
            _output_with_exact_serialized_size(
                MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
            )
        ),
        store=store,
    )
    assert isinstance(projected.output, dict)
    result_ref = cast(str, projected.output["result_ref"])
    fetch = store.fetch_definition()

    store.close()
    store.close()
    fetched = await fetch.execute(_fetch_call(result_ref=result_ref), 1)

    assert (tmp_path / "tool-results").is_dir()
    assert len(tuple((tmp_path / "tool-results").iterdir())) == 1
    assert fetched.status == "error"
    assert fetched.output["error_code"] == "TOOL_RESULT_STORE_CLOSED"
