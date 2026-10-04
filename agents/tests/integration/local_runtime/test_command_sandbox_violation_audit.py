from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.sandbox.sandbox_denial import (
    WRITE_FOLDER_REQUEST_HINT,
)

from .support import (
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    compile_workspace_binary,
    execute_bash,
    load_latest_audit,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


def _write_outside_source(target_path: Path) -> str:
    return f"""
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {{
    int fd = open("{target_path}", O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (fd < 0) {{
        perror("open");
        return 111;
    }}
    close(fd);
    return 0;
}}
"""


@pytest.mark.asyncio
async def test_denied_write_keeps_the_os_error_and_hints_at_write_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    outside_path = tmp_path / "should-not-appear.txt"
    compile_workspace_binary(
        workspace_path=testbed.context.workspace_path,
        executable_name="violationprobe",
        source_code=_write_outside_source(outside_path),
    )

    outcome = await execute_bash(testbed=testbed, command="violationprobe")

    assert outcome.status == "error"
    audit = load_latest_audit(db_path=testbed.db_path)
    assert audit["terminal_outcome"] == "exited"
    assert audit["exit_code"] == 111
    assert audit["sandbox_violation_summary"] is None
    assert outcome.output["exit_code"] == 111
    assert outcome.output["stderr"] == "open: Operation not permitted\n"
    assert outcome.output["error"]["llm_feedback"] == WRITE_FOLDER_REQUEST_HINT
    assert not outside_path.exists()
