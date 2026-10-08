from __future__ import annotations

import codecs
import shutil
from pathlib import Path

import pytest

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files import read as read_module
from pantaray_agents.tools.files import ripgrep
from pantaray_agents.tools.files.read_output import ReadToolOutput

from .read_tool_broker_support import (
    bootstrap_read_runtime_db,
    execute_read_tool,
)

_JAPANESE_LINES = [f"{number}行目: 日本語の本文①髙\r\n" for number in range(1, 6)]


async def _read(
    tmp_path: Path, payload: bytes, *pages: dict[str, int]
) -> list[dict[str, object]]:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    (context.workspace_path / "source.txt").write_bytes(payload)
    outputs = []
    for page in pages or ({},):
        outcome = await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": "source.txt", **page},
        )
        ReadToolOutput.model_validate(outcome.output)
        outputs.append(outcome.output)
    return outputs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bom", "codec", "encoding"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be"),
        (codecs.BOM_UTF8, "utf-8", "utf-8-sig"),
        (b"", "utf-8", "utf-8"),
    ],
)
async def test_read_pages_marked_and_utf8_text_across_a_page_boundary(
    tmp_path: Path, bom: bytes, codec: str, encoding: str
) -> None:
    payload = bom + "".join(_JAPANESE_LINES).encode(codec)

    first, second = await _read(
        tmp_path, payload, {"offset": 1, "limit": 2}, {"offset": 3, "limit": 2}
    )

    assert first["content"] == "1行目: 日本語の本文①髙\n2行目: 日本語の本文①髙\n"
    assert (first["next_offset"], first["total_lines"]) == (3, 5)
    assert second["content"] == "3行目: 日本語の本文①髙\n4行目: 日本語の本文①髙\n"
    assert (second["end_line"], second["next_offset"]) == (4, 5)
    assert first["encoding"] == second["encoding"] == encoding


@pytest.mark.asyncio
async def test_read_reports_cp932_on_every_page_of_a_file_ascii_at_the_top(
    tmp_path: Path,
) -> None:
    # Past the size counted to the end of the file, so only a decision made on
    # the whole file sees the Japanese text from the first page.
    ascii_lines = ["Dim count As Integer\r\n"] * 30_000
    japanese = "' 合計を計算する①髙\r\n"
    payload = "".join([*ascii_lines, japanese]).encode("cp932")
    assert len(payload) > 512 * 1024

    first, last = await _read(tmp_path, payload, {"limit": 1}, {"offset": 30_001})

    assert first["content"] == "Dim count As Integer\n"
    assert last["content"] == "' 合計を計算する①髙\n"
    assert first["encoding"] == last["encoding"] == "cp932"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"ok\n\x81 neither\n", "neither UTF-8 nor CP932"),
        (codecs.BOM_UTF16_LE + b"a\x00b", "not valid utf-16-le text"),
    ],
)
async def test_read_refuses_text_it_cannot_decode_with_a_specific_code(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    with pytest.raises(BrokerPolicyError) as exc_info:
        await _read(tmp_path, payload)

    assert exc_info.value.code == "READ_TEXT_ENCODING_UNSUPPORTED"
    assert message in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_refuses_utf32_as_binary(tmp_path: Path) -> None:
    payload = b"\xff\xfe\x00\x00" + "text\n".encode("utf-32-le")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await _read(tmp_path, payload)

    assert exc_info.value.code == "READ_BINARY_FILE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_read_refuses_utf16_text_over_the_in_memory_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_module, "MAX_DOCUMENT_BYTES", 64)
    payload = codecs.BOM_UTF16_LE + ("x" * 40).encode("utf-16-le")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await _read(tmp_path, payload)

    assert exc_info.value.code == "READ_TEXT_FILE_TOO_LARGE"


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("rg", path=ripgrep.RIPGREP_TRUSTED_PATH) is None,
    reason="ripgrep is not installed in a trusted location",
)
async def test_grep_shows_a_cp932_line_valid_as_utf8_as_read_shows_it(
    tmp_path: Path,
) -> None:
    # Half-width katakana ﾂｩ is C2 A9 in CP932, which UTF-8 reads as ©, so only
    # the whole file says which one the line is.
    short = "ﾂｩ needle"
    long = "ﾂｩ" * 400 + "needle" + "ﾂｩ" * 400
    payload = f"{short}\r\n{long}\r\n' 日本語\r\n".encode("cp932")
    [read] = await _read(tmp_path, payload)
    search_root = tmp_path / "search-root"
    search_root.mkdir()
    (search_root / "source.txt").write_bytes(payload)

    grep = ripgrep.run_ripgrep_grep(
        cwd=search_root,
        sandbox_profile="(version 1)\n(allow default)",
        pattern="needle",
        include_glob=None,
        max_matches=10,
    )

    assert read["encoding"] == "cp932"
    assert read["content"] == f"{short}\n{long}\n' 日本語\n"
    # The long line's excerpt is centred on the match counted in that text.
    assert [(match.line_number, match.line) for match in grep.matches] == [
        (1, short),
        (2, "…" + "ﾂｩ" * 125 + "needle" + "ﾂｩ" * 122 + "…"),
    ]
