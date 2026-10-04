from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from tests.unit.local_runtime.broker_test_support import BROKER_ACTOR_PROCESS_ID

from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    create_private_temp_dir,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    SANDBOX_TEMP_DIR_PREFIX,
)
from pantaray_agents.schema.read_access import ReadAccessScope

from .support import (
    INTEGRATION_APPROVAL_TIMESTAMP,
    ONE_SECOND_MS,
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    compile_workspace_binary,
    execute_bash,
    load_latest_audit,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


PWD_SOURCE = r"""
#include <limits.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {
    char cwd[PATH_MAX];
    if (getcwd(cwd, sizeof(cwd)) == NULL) {
        perror("getcwd");
        return 2;
    }
    puts(cwd);
    return 0;
}
"""
SHEBANG_INTERPRETER_SOURCE = r"""
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc < 2) {
        puts("missing-script");
        return 2;
    }
    printf("interpreter-ok:%s\n", argv[1]);
    return 0;
}
"""
ACTION_PLAN_SANDBOX_PROBE_SOURCE = r"""
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

static int expect_denied_open(const char *path, int flags) {
    int fd;
    errno = 0;
    fd = open(path, flags);
    if (fd >= 0) {
        close(fd);
        return 1;
    }
    return errno == EACCES || errno == EPERM ? 0 : 2;
}

int main(void) {
    int neighbor_fd;
    if (expect_denied_open(__PLAN_PATH__, O_RDONLY) != 0 ||
        expect_denied_open(__PLAN_PATH__, O_WRONLY | O_TRUNC) != 0 ||
        expect_denied_open(__SYMLINK_PATH__, O_RDONLY) != 0 ||
        expect_denied_open(__SYMLINK_PATH__, O_WRONLY | O_TRUNC) != 0) {
        return 10;
    }
    errno = 0;
    if (link(__PLAN_PATH__, __HARDLINK_PATH__) == 0 ||
        (errno != EACCES && errno != EPERM)) {
        return 11;
    }
    neighbor_fd = open(__NEIGHBOR_PATH__, O_RDWR | O_APPEND);
    if (neighbor_fd < 0 || write(neighbor_fd, "ok\n", 3) != 3) {
        return 12;
    }
    close(neighbor_fd);
    puts("plan-private-neighbor-writable");
    return 0;
}
"""
TEMP_ISOLATION_PROBE_SOURCE = r"""
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    char own_path[PATH_MAX];
    const char *tmpdir = getenv("TMPDIR");
    if (tmpdir == NULL || snprintf(own_path, sizeof(own_path), "%s/owned", tmpdir) < 0) {
        return 10;
    }
    int own_fd = open(own_path, O_CREAT | O_WRONLY, 0600);
    if (own_fd < 0 || write(own_fd, "ok", 2) != 2) {
        return 11;
    }
    close(own_fd);
    errno = 0;
    int sibling_fd = open(__SIBLING_PATH__, O_WRONLY | O_TRUNC);
    if (sibling_fd >= 0 || (errno != EACCES && errno != EPERM)) {
        if (sibling_fd >= 0) close(sibling_fd);
        return 12;
    }
    puts("private-temp-isolated");
    return 0;
}
"""


@pytest.mark.asyncio
async def test_workspace_executable_runs_via_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    compile_workspace_binary(
        workspace_path=testbed.context.workspace_path,
        executable_name="cwdprobe",
        source_code=PWD_SOURCE,
    )

    outcome = await execute_bash(testbed=testbed, command="cwdprobe")

    assert outcome.status == "success", outcome.output
    assert outcome.output["status"] == "success"
    assert outcome.output["stdout"] == f"{testbed.context.workspace_path}\n"
    audit = load_latest_audit(db_path=testbed.db_path)
    assert audit["terminal_outcome"] == "exited"
    assert audit["resolved_executable_path"] == "/bin/bash"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_access_scope", ["workspace", "full_access"])
async def test_action_plan_remains_private_inside_broad_workspace_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_access_scope: ReadAccessScope,
) -> None:
    # Under full access the profile also allows reading "/"; the plan deny must
    # still win.
    testbed = bootstrap_runtime_testbed(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        read_access_scope=read_access_scope,
    )
    plan_path = testbed.context.workspace_path / "plan.md"
    symlink_path = testbed.context.workspace_path / "plan-alias.md"
    hardlink_path = testbed.context.workspace_path / "plan-hardlink.md"
    neighbor_path = testbed.context.workspace_path / "neighbor.txt"
    plan_path.write_text("private\n", encoding="utf-8")
    symlink_path.symlink_to(plan_path)
    neighbor_path.write_text("public\n", encoding="utf-8")
    source = (
        ACTION_PLAN_SANDBOX_PROBE_SOURCE.replace(
            "__PLAN_PATH__", json.dumps(str(plan_path))
        )
        .replace("__SYMLINK_PATH__", json.dumps(str(symlink_path)))
        .replace("__HARDLINK_PATH__", json.dumps(str(hardlink_path)))
        .replace("__NEIGHBOR_PATH__", json.dumps(str(neighbor_path)))
    )
    compile_workspace_binary(
        workspace_path=testbed.context.workspace_path,
        executable_name="planprobe",
        source_code=source,
    )

    outcome = await execute_bash(testbed=testbed, command="planprobe")

    assert outcome.status == "success", outcome.output
    assert outcome.output["stdout"] == "plan-private-neighbor-writable\n"
    assert plan_path.read_text(encoding="utf-8") == "private\n"
    assert neighbor_path.read_text(encoding="utf-8") == "public\nok\n"
    assert not hardlink_path.exists()


@pytest.mark.asyncio
async def test_invocation_temp_is_outside_workspace_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    # Another invocation's temp dir, where Pantaray creates them.
    leaf = create_private_temp_dir(
        db_path=testbed.db_path, prefix=SANDBOX_TEMP_DIR_PREFIX
    )
    sibling_path = leaf / "owned"
    sibling_path.write_text("private", encoding="utf-8")
    compile_workspace_binary(
        workspace_path=testbed.context.workspace_path,
        executable_name="tempprobe",
        source_code=TEMP_ISOLATION_PROBE_SOURCE.replace(
            "__SIBLING_PATH__", json.dumps(str(sibling_path))
        ),
    )
    with sqlite3.connect(testbed.db_path) as connection, connection:
        action_id = str(
            connection.execute(
                "SELECT action_id FROM execution_sessions WHERE execution_session_id = ?",
                (testbed.context.execution_session_id,),
            ).fetchone()[0]
        )
        for child_id in ("child-unclaimed", "child-workspace"):
            connection.execute(
                """INSERT INTO processes(
                       process_id, user_id, kind, status, action_id, started_at,
                       updated_at, heartbeat_at, next_event_seq, parent_process_id
                   ) VALUES (?, 'user-1', 'action_subagent', 'running', ?, ?, ?, ?, 1, ?)""",
                (
                    child_id,
                    action_id,
                    *([INTEGRATION_APPROVAL_TIMESTAMP] * 3),
                    BROKER_ACTOR_PROCESS_ID,
                ),
            )
        connection.execute(
            """INSERT INTO action_subagent_resource_claims(
                   claim_id, user_id, action_id, parent_process_id, child_process_id,
                   resource_kind, root_identity, normalized_key, acquired_at
               ) VALUES ('claim-workspace', 'user-1', ?, ?, 'child-workspace',
                         'workspace_path', ?, ?, ?)""",
            (
                action_id,
                BROKER_ACTOR_PROCESS_ID,
                testbed.context.manifest_id,
                str(testbed.context.workspace_path),
                INTEGRATION_APPROVAL_TIMESTAMP,
            ),
        )
    for child_id in ("child-unclaimed", "child-workspace"):
        outcome = await execute_broker_tool(
            db_path=testbed.db_path,
            busy_timeout_ms=ONE_SECOND_MS,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=child_id,
            manifest_id=testbed.context.manifest_id,
            execution_session_id=testbed.context.execution_session_id,
            tool_request_id=f"request-{child_id}",
            args={"command": "tempprobe"},
        )
        assert outcome.status == "success", outcome.output
        assert outcome.output["stdout"] == "private-temp-isolated\n"
    assert sibling_path.read_text(encoding="utf-8") == "private"

    with sqlite3.connect(testbed.db_path) as connection:
        paths = connection.execute(
            """SELECT resource_path FROM tool_runtime_resources
               WHERE resource_kind = 'temp_dir' AND tool_invocation_id IS NOT NULL"""
        ).fetchall()
    assert len(paths) == 2
    assert all(
        not Path(str(row[0])).is_relative_to(testbed.context.workspace_path)
        for row in paths
    )


@pytest.mark.asyncio
async def test_workspace_shebang_script_runs_with_os_interpreter_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    interpreter_path = compile_workspace_binary(
        workspace_path=testbed.context.workspace_path,
        executable_name="shebang-interpreter",
        source_code=SHEBANG_INTERPRETER_SOURCE,
    )
    script_path = testbed.context.workspace_path / ".venv" / "bin" / "shebangprobe"
    script_path.write_text(f"#!{interpreter_path}\nignored\n", encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | 0o111)

    outcome = await execute_bash(testbed=testbed, command="shebangprobe")

    assert outcome.status == "success", outcome.output
    assert outcome.output["stdout"] == f"interpreter-ok:{script_path}\n"
    audit = load_latest_audit(db_path=testbed.db_path)
    assert audit["terminal_outcome"] == "exited"
    assert audit["resolved_executable_path"] == "/bin/bash"


@pytest.mark.asyncio
async def test_workspace_sh_shebang_script_runs_with_macos_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if (
        not Path("/bin/sh").is_file()
        or not Path("/private/var/select/sh").is_file()
        or not Path("/bin/bash").is_file()
    ):
        pytest.skip("requires macOS-compatible /bin/sh and /bin/bash paths")
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    script_path = testbed.context.workspace_path / ".venv" / "bin" / "shprobe"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("#!/bin/sh\necho sh-ok\n", encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | 0o111)

    outcome = await execute_bash(testbed=testbed, command="shprobe")

    assert outcome.status == "success", outcome.output
    assert outcome.output["stdout"] == "sh-ok\n"


@pytest.mark.asyncio
async def test_git_uses_installed_runtime_inside_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    git_path = shutil.which("git")
    if git_path is None:
        pytest.skip("requires git")
    subprocess.run(
        [git_path, "init"],
        cwd=testbed.context.workspace_path,
        check=True,
        capture_output=True,
        text=True,
    )

    outcome = await execute_bash(testbed=testbed, command="git status --short")

    assert outcome.status == "success", outcome.output
    audit = load_latest_audit(db_path=testbed.db_path)
    assert audit["resolved_executable_path"] == "/bin/bash"


@pytest.mark.asyncio
async def test_sleep_runs_in_sandbox_with_default_command_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    outcome = await execute_bash(testbed=testbed, command="sleep 0.01")

    assert outcome.status == "success", outcome.output
    assert outcome.output["exit_code"] == 0
    audit = load_latest_audit(db_path=testbed.db_path)
    assert audit["terminal_outcome"] == "exited"
    assert audit["resolved_executable_path"] == "/bin/bash"


@pytest.mark.asyncio
async def test_shell_does_not_run_parent_startup_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    marker = testbed.context.workspace_path / "unexpected-startup.txt"
    script = testbed.context.workspace_path / "parent-startup.sh"
    script.write_text(f"printf unexpected > {shlex.quote(str(marker))}\n")
    monkeypatch.setenv("BASH_ENV", str(script))
    outcome = await execute_bash(testbed=testbed, command="pwd")
    assert outcome.status == "success", outcome.output
    assert outcome.output["stdout"].strip() == str(testbed.context.workspace_path)
    assert not marker.exists()
