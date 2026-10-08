from __future__ import annotations

import codecs
import os
import shutil
from pathlib import Path

import pytest

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files import read as read_module
from pantaray_agents.tools.files import ripgrep, text_encoding
from pantaray_agents.tools.files.read_output import ReadToolOutput

from .read_tool_broker_support import (
    bootstrap_read_runtime_db,
    execute_read_tool,
)

_REAL_RIPGREP = pytest.mark.skipif(
    shutil.which("rg", path=ripgrep.RIPGREP_TRUSTED_PATH) is None,
    reason="ripgrep is not installed in a trusted location",
)
_ALLOW_ALL_PROFILE = "(version 1)\n(allow default)"
# ﾂｩ is C2 A9 in CP932, which UTF-8 also accepts, as ©.
_BOTH_ENCODINGS_LINE = "ﾂｩ needle\r\n"
# Past the size a page counts lines to the end of, so the first page stops
# before the CP932 line.
_ASCII_PADDING = "Dim count As Integer\r\n" * 30_000
_CP932_LINE = "' 日本語 needle\r\n"


def _grep(cwd: Path, *, max_matches: int = 10) -> ripgrep.RipgrepGrepResult:
    return ripgrep.run_ripgrep_grep(
        cwd=cwd,
        open_matched_file=lambda path: os.open(cwd / path, os.O_RDONLY),
        sandbox_profile=_ALLOW_ALL_PROFILE,
        pattern="needle",
        include_glob=None,
        max_matches=max_matches,
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
@_REAL_RIPGREP
async def test_grep_shows_a_cp932_line_valid_as_utf8_as_read_shows_it(
    tmp_path: Path,
) -> None:
    # Half-width katakana ﾂｩ is C2 A9 in CP932, which UTF-8 reads as ©, so only
    # the rest of the file says which one the line is.
    short = "ﾂｩ needle"
    long = "ﾂｩ" * 400 + "needle" + "ﾂｩ" * 400
    payload = f"{short}\r\n{long}\r\n' 日本語\r\n".encode("cp932")
    [read] = await _read(tmp_path, payload)
    search_root = tmp_path / "search-root"
    search_root.mkdir()
    (search_root / "source.txt").write_bytes(payload)

    grep = _grep(search_root)

    assert read["encoding"] == "cp932"
    assert read["content"] == f"{short}\n{long}\n' 日本語\n"
    # The long line's excerpt is centred on the match counted in that text.
    assert [(match.line_number, match.line) for match in grep.matches] == [
        (1, short),
        (2, "…" + "ﾂｩ" * 125 + "needle" + "ﾂｩ" * 122 + "…"),
    ]


@pytest.mark.asyncio
async def test_read_decides_from_a_bounded_prefix_and_refuses_a_later_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(text_encoding, "ENCODING_DECISION_BYTES", 64)
    payload = (_BOTH_ENCODINGS_LINE + _ASCII_PADDING + _CP932_LINE).encode("cp932")

    [first] = await _read(tmp_path, payload, {"limit": 1})
    (tmp_path / "later").mkdir()
    with pytest.raises(BrokerPolicyError) as exc_info:
        await _read(tmp_path / "later", payload, {"offset": 30_002})

    # Only the prefix was decoded, so the CP932 line past it did not decide.
    assert (first["encoding"], first["content"]) == ("utf-8", "© needle\n")
    assert exc_info.value.code == "READ_TEXT_ENCODING_UNSUPPORTED"
    assert "not valid utf-8 text" in str(exc_info.value)


@_REAL_RIPGREP
def test_grep_decides_from_a_bounded_prefix_after_the_match_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(text_encoding, "ENCODING_DECISION_BYTES", 64)
    (tmp_path / "large.bas").write_bytes(
        (_BOTH_ENCODINGS_LINE + _ASCII_PADDING + _CP932_LINE).encode("cp932")
    )

    result = _grep(tmp_path, max_matches=1)

    assert [(match.line_number, match.line) for match in result.matches] == [
        (1, "© needle")
    ]


@_REAL_RIPGREP
def test_grep_stops_deciding_encodings_when_its_time_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = "日本語 needle\n".encode("cp932")
    (tmp_path / "module.bas").write_bytes(line)
    clock = iter((0.0, ripgrep.RIPGREP_TIMEOUT_SECONDS))
    monkeypatch.setattr(ripgrep, "monotonic", lambda: next(clock))

    result = _grep(tmp_path)

    assert [match.line for match in result.matches] == [
        line.decode("utf-8", errors="replace").removesuffix("\n")
    ]
