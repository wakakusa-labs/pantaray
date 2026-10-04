from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling import tool_result_storage
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    ToolResultLoadError,
    ToolResultReadLimitError,
    ToolResultStorageError,
    ToolResultTextOffsetError,
    load_action_file_json_result,
    read_tool_result_text_prefix,
    release_stored_tool_result,
    store_tool_result,
)


def _display_text(output: object) -> str:
    return json.dumps(output, ensure_ascii=False, indent=2)


def _output_with_exact_display_size(character_count: int) -> dict[str, str]:
    empty_output = {"text": ""}
    payload_size = character_count - len(_display_text(empty_output))
    output = {"text": "x" * payload_size}
    assert len(_display_text(output)) == character_count
    return output


def test_text_prefix_rejects_invalid_cursor_and_storage_provenance() -> None:
    with pytest.raises(ToolResultTextOffsetError, match="splits a UTF-8"):
        read_tool_result_text_prefix(
            action_tool_results_path=None,
            output_owner_id="step-1",
            output="🙂",
            storage_kind="inline_json",
            start_byte=2,
            page_bytes=4,
            max_read_bytes=6,
        )
    with pytest.raises(ToolResultLoadError, match="metadata is missing"):
        read_tool_result_text_prefix(
            action_tool_results_path=None,
            output_owner_id="step-1",
            output=None,
            storage_kind="action_file",
            start_byte=0,
            page_bytes=4,
            max_read_bytes=6,
        )


def test_small_result_stays_inline_without_creating_storage_directory(
    tmp_path: Path,
) -> None:
    output = {"status": "success", "content": "日本語\nsecond line"}

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="invocation-1",
        output=output,
        search_text="search\ntext",
        stdout_text="stdout\n",
        stderr_text="stderr\n",
    )

    assert result.output_json is output
    assert result.storage_kind == "inline_json"
    assert result.search_text == "search\ntext"
    assert result.stdout_text == "stdout\n"
    assert result.stderr_text == "stderr\n"
    assert list(tmp_path.iterdir()) == []


def test_result_at_inline_character_limit_stays_inline(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT)

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="boundary-result",
        output=output,
    )

    assert result.output_json is output
    assert result.storage_kind == "inline_json"
    assert list(tmp_path.iterdir()) == []


def test_result_above_inline_character_limit_is_stored(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="above-boundary",
        output=output,
    )

    assert result.output_json["character_count"] == (
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    assert result.storage_kind == "action_file"
    assert (tmp_path / str(result.output_json["path"])).is_file()
    release_stored_tool_result(result)


def test_large_unicode_result_is_stored_with_exact_metadata(tmp_path: Path) -> None:
    output = {
        "title": "日本語の結果",
        "content": ("一行目\n二行目🌊" * 2_500),
    }
    history_text = _display_text(output)
    assert len(history_text) > ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="large-result",
        output=output,
        search_text="duplicate search text",
        stdout_text="duplicate stdout",
        stderr_text="duplicate stderr",
    )

    relative_path = Path(str(result.output_json["path"]))
    stored_path = tmp_path / relative_path
    assert stored_path.read_text(encoding="utf-8") == history_text
    assert result.output_json["storage"] == "action_file"
    assert result.storage_kind == "action_file"
    assert result.output_json["path"] == relative_path.as_posix()
    assert result.output_json["media_type"] == "application/json"
    assert result.output_json["byte_size"] == len(history_text.encode("utf-8"))
    assert result.output_json["character_count"] == len(history_text)
    assert result.output_json["line_count"] == history_text.count("\n") + 1
    assert result.output_json["preview"] == history_text[:1_000]
    assert f"path={relative_path.as_posix()}" in result.output_json["retry_hint"]
    assert "offset=" in result.output_json["retry_hint"]
    assert "sha256" not in result.output_json
    assert result.search_text is None
    assert result.stdout_text is None
    assert result.stderr_text is None
    release_stored_tool_result(result)

    # A result spilled before previews existed still loads.
    without_preview = {
        key: value
        for key, value in result.output_json.items()
        if key not in {"preview", "retry_hint"}
    }
    for metadata in (result.output_json, without_preview):
        loaded = load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="large-result",
            metadata=metadata,
            max_bytes=result.output_json["byte_size"],
        )
        assert loaded == output


def test_action_file_json_load_is_bounded(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="bounded-result",
        output=output,
    )
    release_stored_tool_result(result)

    with pytest.raises(ToolResultReadLimitError, match="read limit"):
        load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="bounded-result",
            metadata=result.output_json,
            max_bytes=1,
        )


def test_action_file_json_load_rejects_invalid_path_before_read_limit(
    tmp_path: Path,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="invalid-path",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    metadata = dict(result.output_json)
    metadata["path"] = str((tmp_path / "outside.json").absolute())

    with pytest.raises(ToolResultLoadError, match="outside its owner") as error:
        load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="invalid-path",
            metadata=metadata,
            max_bytes=1,
        )

    assert not isinstance(error.value, ToolResultReadLimitError)


def test_action_file_json_load_rejects_symlink_replacement(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="symlinked-result",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    stored_path = Path(str(result.output_json["path"]))
    outside = tmp_path.parent / f"{tmp_path.name}-outside-result.json"
    outside.write_text("{}", encoding="utf-8")
    stored_path.unlink()
    stored_path.symlink_to(outside)

    with pytest.raises(ToolResultLoadError, match="safely open"):
        load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="symlinked-result",
            metadata=result.output_json,
            max_bytes=int(result.output_json["byte_size"]),
        )


def test_action_file_json_load_rejects_byte_size_mismatch(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="size-mismatch",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    metadata = dict(result.output_json)
    metadata["byte_size"] = int(metadata["byte_size"]) + 1

    with pytest.raises(ToolResultLoadError, match="size or type"):
        load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="size-mismatch",
            metadata=metadata,
            max_bytes=int(metadata["byte_size"]),
        )


def test_action_file_json_load_rechecks_size_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="growing-result",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    stored_path = Path(str(result.output_json["path"]))
    original_read = tool_result_storage.os.read
    grew = False

    def grow_after_read(file_descriptor: int, read_bytes: int) -> bytes:
        nonlocal grew
        chunk = original_read(file_descriptor, read_bytes)
        if chunk and not grew:
            with stored_path.open("ab") as handle:
                handle.write(b" ")
            grew = True
        return chunk

    monkeypatch.setattr(tool_result_storage.os, "read", grow_after_read)
    with pytest.raises(ToolResultLoadError, match="size changed"):
        load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="growing-result",
            metadata=result.output_json,
            max_bytes=int(result.output_json["byte_size"]),
        )


def test_action_file_json_load_rejects_infinite_exponent(tmp_path: Path) -> None:
    owner_path = tmp_path / "infinite-number"
    owner_path.mkdir()
    stored_path = owner_path / f"output-{'1' * 32}.json"
    text = '{"value": 1e9999}'
    stored_path.write_text(text, encoding="utf-8")
    metadata = {
        "storage": "action_file",
        "path": str(stored_path.absolute()),
        "media_type": "application/json",
        "byte_size": len(text.encode("utf-8")),
        "character_count": len(text),
        "line_count": 1,
    }

    with pytest.raises(ToolResultLoadError, match="valid UTF-8 JSON"):
        load_action_file_json_result(
            action_tool_results_path=tmp_path,
            invocation_id="infinite-number",
            metadata=metadata,
            max_bytes=1_000,
        )


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"\x00\x01raw\x00bytes",
        b"<script>alert('raw')</script>",
        b"small binary payload",
        b"x" * (ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
    ),
    ids=("empty", "nul", "active-content", "small", "large"),
)
def test_binary_result_is_always_stored_with_exact_metadata(
    tmp_path: Path,
    payload: bytes,
) -> None:
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="binary-result",
        output=payload,
        search_text="must not be duplicated",
        stdout_text="must not be duplicated",
        stderr_text="must not be duplicated",
    )

    assert isinstance(result.output_json, dict)
    assert result.storage_kind == "action_file"
    assert result.output_json == {
        "storage": "action_file",
        "path": result.output_json["path"],
        "media_type": "application/octet-stream",
        "byte_size": len(payload),
    }
    stored_path = Path(str(result.output_json["path"]))
    assert stored_path.suffix == ".bin"
    assert stored_path.read_bytes() == payload
    assert result.search_text is None
    assert result.stdout_text is None
    assert result.stderr_text is None
    release_stored_tool_result(result)


def test_small_storage_shaped_json_remains_host_classified_inline(
    tmp_path: Path,
) -> None:
    output = {"storage": "action_file"}

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="external-storage-value",
        output=output,
    )

    assert result.output_json is output
    assert result.storage_kind == "inline_json"
    assert list(tmp_path.iterdir()) == []


def test_json_null_stays_inline_and_preserves_audit_text(
    tmp_path: Path,
) -> None:
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="no-output",
        output=None,
        search_text="search",
        stdout_text="stdout",
        stderr_text="stderr",
    )

    assert result.output_json is None
    assert result.storage_kind == "inline_json"
    assert result.search_text == "search"
    assert result.stdout_text == "stdout"
    assert result.stderr_text == "stderr"


@pytest.mark.parametrize(
    "invocation_id",
    ("", ".", "..", "../escape", "nested/id", r"nested\id", "nul\0id"),
)
def test_invalid_invocation_id_is_rejected_before_writing(
    tmp_path: Path,
    invocation_id: str,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    with pytest.raises(ValueError, match="invocation_id"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id=invocation_id,
            output=output,
        )

    assert list(tmp_path.iterdir()) == []


def test_runtime_invocation_id_is_encoded_as_one_path_segment(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="bash:action-1:request-1:1",
        output=output,
    )

    result_path = str(result.output_json["path"])
    assert result_path.startswith(
        str(tmp_path / "bash%3Aaction-1%3Arequest-1%3A1" / "output-")
    )
    assert result_path.endswith(".json")
    release_stored_tool_result(result)


def test_storage_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_path.mkdir()
    (tmp_path / "symlink-escape").symlink_to(
        outside_path,
        target_is_directory=True,
    )
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    with pytest.raises(ToolResultStorageError, match="tool result directory"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="symlink-escape",
            output=output,
        )

    assert list(outside_path.iterdir()) == []


def test_atomic_rename_failure_preserves_previous_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_output = {
        "content": "previous" * ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    }
    replacement_output = {
        "content": "replacement" * ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    }
    previous_result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="atomic-result",
        output=previous_output,
    )
    stored_path = tmp_path / str(previous_result.output_json["path"])
    previous_text = stored_path.read_text(encoding="utf-8")

    def fail_rename(
        _source: str,
        _destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        _ = src_dir_fd, dst_dir_fd
        raise OSError("rename failed")

    monkeypatch.setattr(tool_result_storage.os, "rename", fail_rename)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="atomic-result",
            output=replacement_output,
        )

    assert stored_path.read_text(encoding="utf-8") == previous_text
    assert list(stored_path.parent.glob(".output-*.tmp")) == []
    release_stored_tool_result(previous_result)


def test_directory_fsync_failure_removes_renamed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    original_fsync = tool_result_storage.os.fsync
    call_count = 0

    def fail_result_directory_fsync(file_descriptor: int) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise OSError("directory fsync failed")
        original_fsync(file_descriptor)

    monkeypatch.setattr(tool_result_storage.os, "fsync", fail_result_directory_fsync)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="fsync-failure",
            output=output,
        )

    assert list(tmp_path.rglob("*.json")) == []


def test_parent_directory_fsync_failure_stops_before_writing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    original_fsync = tool_result_storage.os.fsync
    parent_stat = tmp_path.stat()

    def fail_parent_directory_fsync(file_descriptor: int) -> None:
        descriptor_stat = tool_result_storage.os.fstat(file_descriptor)
        if (
            descriptor_stat.st_dev == parent_stat.st_dev
            and descriptor_stat.st_ino == parent_stat.st_ino
        ):
            raise OSError("parent directory fsync failed")
        original_fsync(file_descriptor)

    monkeypatch.setattr(tool_result_storage.os, "fsync", fail_parent_directory_fsync)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="parent-fsync-failure",
            output=output,
        )

    assert list(tmp_path.rglob("output-*")) == []
