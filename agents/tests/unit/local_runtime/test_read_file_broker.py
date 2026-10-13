from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    collect_prompt_file_inputs,
)
from pantaray_agents.agents.core.llm_file_inputs import build_blob_file_block
from pantaray_agents.local_runtime.llm_proxy.content_files import (
    prepare_content_file,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.read_output import (
    ReadToolOutput,
)

from .read_tool_broker_support import (
    bootstrap_read_runtime_db,
    execute_read_tool,
)


@pytest.mark.asyncio
async def test_read_workspace_file_with_offset_and_limit(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text(
        "alpha\nbeta\ngamma\n",
        encoding="utf-8",
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "notes.txt", "offset": 2, "limit": 2},
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "file"
    assert outcome.output["path"] == str(context.workspace_path / "notes.txt")
    assert outcome.output["content"] == "beta\ngamma\n"
    assert outcome.output["offset"] == 2
    assert outcome.output["end_line"] == 3
    assert outcome.output["total_lines"] == 3
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert outcome.output["next_offset"] is None
    assert outcome.file_paths == (str(context.workspace_path / "notes.txt"),)


@pytest.mark.asyncio
async def test_read_file_with_offset_and_default_limit(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text(
        "alpha\nbeta\ngamma\n",
        encoding="utf-8",
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "notes.txt", "offset": 2},
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "file"
    assert outcome.output["content"] == "beta\ngamma\n"
    assert outcome.output["offset"] == 2
    assert outcome.output["end_line"] == 3
    assert outcome.output["total_lines"] == 3
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert outcome.output["next_offset"] is None


@pytest.mark.asyncio
async def test_read_caps_output_bytes(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "large.txt").write_text(
        ("x" * 100 + "\n") * 6_000,
        encoding="utf-8",
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "large.txt"},
    )

    assert outcome.status == "success"
    output = outcome.output
    assert output["kind"] == "file"
    assert (
        len(serialize_json_tool_output(output))
        <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    assert output["total_lines"] is None
    assert output["truncated"] is True
    assert output["truncation_reason"] == "page_limit"
    assert output["retry_hint"] is not None
    # The page ends after its last whole line.
    assert output["content"].endswith("x\n")
    assert output["next_offset"] == output["end_line"] + 1
    assert output["next_column"] == 1
    ReadToolOutput.model_validate(output)


@pytest.mark.asyncio
async def test_read_reaches_large_offset_without_splitting(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    line = "x" * 100 + "\n"
    (context.workspace_path / "large.txt").write_text(line * 6_000, encoding="utf-8")
    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "large.txt", "offset": 5_900, "limit": 10},
    )
    assert outcome.status == "success"
    assert outcome.output["content"] == line * 10
    assert outcome.output["end_line"] == 5_909
    assert outcome.output["next_offset"] == 5_910


@pytest.mark.asyncio
async def test_read_reaches_lines_past_eight_mebibytes(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    line = "x" * 1_023 + "\n"
    # Past the 8 MiB that read once scanned before refusing an offset.
    last = 8 * 1024 * 1024 // len(line) + 2
    (context.workspace_path / "large.txt").write_text(line * (last - 1) + "end\n")

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "large.txt", "offset": last, "limit": 2},
    )

    assert outcome.output["content"] == "end\n"
    assert outcome.output["total_lines"] == last
    assert outcome.output["truncated"] is False


@pytest.mark.asyncio
async def test_read_returns_total_lines_when_limit_stops_before_eof(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text(
        "alpha\nbeta\ngamma\n",
        encoding="utf-8",
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "notes.txt", "offset": 1, "limit": 2},
    )

    assert outcome.status == "success"
    assert outcome.output["kind"] == "file"
    assert outcome.output["content"] == "alpha\nbeta\n"
    assert outcome.output["end_line"] == 2
    assert outcome.output["total_lines"] == 3
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "page_limit"
    assert outcome.output["retry_hint"] == (
        "Continue with offset=next_offset and column=next_column."
    )
    assert outcome.output["next_offset"] == 3
    assert outcome.output["next_column"] == 1
    ReadToolOutput.model_validate(outcome.output)


@pytest.mark.asyncio
async def test_read_pages_long_lines_without_losing_content(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "long-line.txt").write_text(
        "a" * 2_100 + "\nnext\n",
        encoding="utf-8",
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "long-line.txt"},
    )

    assert outcome.status == "success"
    assert outcome.output["content"] == (
        "a" * 2_000 + " … [line cut; read offset=1 column=2001 for the rest]\nnext\n"
    )
    assert outcome.output["next_offset"] is None
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "line_length"
    ReadToolOutput.model_validate(outcome.output)

    continuation = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "long-line.txt", "offset": 1, "column": 2_001, "limit": 1},
    )

    assert continuation.output["content"] == "a" * 100 + "\n"
    assert continuation.output["next_offset"] == 2
    assert continuation.output["next_column"] == 1


@pytest.mark.asyncio
async def test_read_keeps_whole_cut_lines_inline_with_their_markers(
    tmp_path: Path,
) -> None:
    # The shape of a saved search result: every value on one long line.
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "results.json").write_text(
        "".join(f"{index}" + "b" * 3_000 + "\n" for index in range(30)),
        encoding="utf-8",
    )

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "results.json"},
    )

    output = outcome.output
    assert (
        len(serialize_json_tool_output(output))
        <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    lines = output["content"].splitlines()
    assert len(lines) > 1
    assert lines[-1].endswith(
        f"[line cut; read offset={output['end_line']} column=2001 for the rest]"
    )
    assert output["next_offset"] == output["end_line"] + 1
    assert output["next_column"] == 1
    ReadToolOutput.model_validate(output)


@pytest.mark.asyncio
async def test_read_splits_a_first_line_too_large_to_keep_inline_in_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.tools.files.read_page.ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT",
        2_400,
    )
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "long.txt").write_text("a" * 3_000 + "\n")

    outcome = await execute_read_tool(
        db_path=db_path, context=context, args={"path": "long.txt"}
    )

    content = outcome.output["content"]
    assert content == "a" * len(content)
    assert outcome.output["next_offset"] == 1
    assert outcome.output["next_column"] == len(content) + 1
    assert outcome.output["end_column"] == len(content)


@pytest.mark.asyncio
async def test_read_rejects_binary_file(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "compiled.bin").write_bytes(b"\x00\x01\x02binary")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "compiled.bin"},
        )

    assert exc_info.value.code == "READ_BINARY_FILE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_read_refuses_a_fifo_instead_of_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    # Opening a FIFO that has no writer blocks forever, on a thread nothing can
    # stop, and the one Action worker waits with it.
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    os.mkfifo(context.workspace_path / "pipe")

    with pytest.raises(BrokerPolicyError) as exc_info:
        async with asyncio.timeout(5):
            await execute_read_tool(
                db_path=db_path,
                context=context,
                args={"path": "pipe"},
            )

    assert exc_info.value.code == "READ_NOT_A_REGULAR_FILE"


@pytest.mark.asyncio
async def test_a_read_image_is_sent_as_read_after_its_file_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    source_path = context.workspace_path / "pixel.png"
    source_path.write_bytes(payload)

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "pixel.png"},
    )
    # The Action regenerates the screenshot it just looked at.
    source_path.write_bytes(payload + b"regenerated")

    assert outcome.status == "success"
    assert outcome.output["kind"] == "attachment"
    (shown,) = outcome.output["attachments"]
    (kept,) = outcome.attachments
    assert shown["source_kind"] == kept["source_kind"] == "local_image_blob"
    assert shown["storage_path"] == kept["storage_path"]
    assert shown["ref"] == kept["ref"]
    assert "sha256" not in shown
    # The next turn's request reads the history's attachment back as it was read.
    (file_input,) = collect_prompt_file_inputs(
        prompt=kept["ref"], owners=[{"attachments": [kept]}]
    )
    prepared = prepare_content_file(
        file_data=build_blob_file_block(file_input)["file_data"],
        user_id="user-1",  # the user read_tool_broker_support runs as
    )
    assert prepared.payload == payload
    assert prepared.sha256 == hashlib.sha256(payload).hexdigest()
    assert outcome.search_text is None


@pytest.mark.asyncio
async def test_read_rejects_attachment_over_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    monkeypatch.setattr(
        "pantaray_agents.tools.files.read.MAX_ATTACHMENT_BYTES",
        16,
    )
    (context.workspace_path / "huge.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + (b"x" * 17)
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "huge.png"},
        )

    assert exc_info.value.code == "READ_ATTACHMENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_read_rejects_offset_beyond_end_of_file(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "notes.txt", "offset": 3},
        )

    assert exc_info.value.code == "READ_OFFSET_OUT_OF_RANGE"


@pytest.mark.asyncio
async def test_read_allows_offset_one_for_empty_file(tmp_path: Path) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "empty.txt").write_text("", encoding="utf-8")

    outcome = await execute_read_tool(
        db_path=db_path,
        context=context,
        args={"path": "empty.txt", "offset": 1},
    )

    assert outcome.status == "success"
    assert outcome.output["content"] == ""
    assert outcome.output["total_lines"] == 0
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert outcome.output["end_line"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("line", ["English source " * 8, '日本語の本文😀\t"\\' * 10])
async def test_read_pages_survive_projection_and_reconstruct_source(
    tmp_path: Path, line: str
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    source = "\n".join(f"{number}: {line}" for number in range(322))
    (context.workspace_path / "source.txt").write_text(source, encoding="utf-8")
    parts: list[str] = []
    offset, column = 1, 1
    while True:
        outcome = await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "source.txt", "offset": offset, "column": column},
        )
        output = outcome.output
        ReadToolOutput.model_validate(output)
        assert (
            len(serialize_json_tool_output(output))
            <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        )
        assert output["content"]
        parts.append(output["content"])
        next_offset, next_column = output["next_offset"], output["next_column"]
        if next_offset is None:
            assert output["truncated"] is False
            break
        assert (next_offset, next_column) > (offset, column)
        offset, column = next_offset, next_column
    assert len(parts) > 1
    assert "".join(parts) == source
