from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.tools.contract import BrokerPolicyError

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_bash_invocation,
)

BUSY_TIMEOUT_MS = 1_000
COMPLETED_AT = "2026-03-23T00:00:03Z"
type InvocationIdentityColumn = Literal["user_id", "action_id", "manifest_id"]


def test_invocation_completion_rejects_missing_tool_results_root(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    shutil.rmtree(context.tool_results_path)

    with pytest.raises(BrokerPolicyError, match="root does not exist"):
        _finalize_spilling_bash_result(
            db_path=db_path,
            invocation_id=invocation_id,
        )

    assert not context.tool_results_path.exists()
    _assert_invocation_has_no_persisted_result(db_path, invocation_id)


def test_invocation_completion_rejects_tool_results_root_retargeting(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    context.tool_results_path.rmdir()
    context.tool_results_path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BrokerPolicyError, match="approved target"):
        _finalize_spilling_bash_result(
            db_path=db_path,
            invocation_id=invocation_id,
        )

    assert not any(outside.iterdir())
    _assert_invocation_has_no_persisted_result(db_path, invocation_id)


@pytest.mark.parametrize(
    "identity_column",
    ("user_id", "action_id", "manifest_id"),
)
def test_invocation_completion_rejects_mismatched_manifest_identity(
    tmp_path: Path,
    identity_column: InvocationIdentityColumn,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    _set_invocation_identity(
        db_path=db_path,
        invocation_id=invocation_id,
        identity_column=identity_column,
        identity_value=f"mismatched-{identity_column}",
    )

    with pytest.raises(BrokerPolicyError, match="root is not available"):
        _finalize_spilling_bash_result(
            db_path=db_path,
            invocation_id=invocation_id,
        )

    assert not any(context.tool_results_path.iterdir())
    _assert_invocation_has_no_persisted_result(db_path, invocation_id)


def test_invocation_completion_rejects_mismatched_tool_results_root_id(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE workspace_manifest_roots
            SET root_id = 'root:mismatched:tool-results'
            WHERE root_id = 'root:action-1:tool-results'
            """
        )

    with pytest.raises(BrokerPolicyError, match="root is not available"):
        _finalize_spilling_bash_result(
            db_path=db_path,
            invocation_id=invocation_id,
        )

    assert not any(context.tool_results_path.iterdir())
    _assert_invocation_has_no_persisted_result(db_path, invocation_id)


def _finalize_spilling_bash_result(*, db_path: Path, invocation_id: str) -> None:
    finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        request=ToolResultFinalizationRequest(
            owner=InvocationToolResultOwner(
                invocation_id=invocation_id,
                completed_at=COMPLETED_AT,
                status="completed",
                completion_scope="invocation",
            ),
            output={
                "status": "success",
                "stdout": "x" * (ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
                "stderr": "",
                "exit_code": 0,
            },
        ),
    )


def _assert_invocation_has_no_persisted_result(
    db_path: Path,
    invocation_id: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status, completed_at
            FROM tool_invocations
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
        output_count = connection.execute(
            "SELECT COUNT(*) FROM tool_outputs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        reference_count = connection.execute(
            "SELECT COUNT(*) FROM file_references WHERE tool_invocation_id = ?",
            (invocation_id,),
        ).fetchone()
    assert invocation_row == ("running", None)
    assert output_count == (0,)
    assert reference_count == (0,)


def _set_invocation_identity(
    *,
    db_path: Path,
    invocation_id: str,
    identity_column: InvocationIdentityColumn,
    identity_value: str,
) -> None:
    update_statements: dict[InvocationIdentityColumn, str] = {
        "user_id": "UPDATE tool_invocations SET user_id = ? WHERE invocation_id = ?",
        "action_id": (
            "UPDATE tool_invocations SET action_id = ? WHERE invocation_id = ?"
        ),
        "manifest_id": (
            "UPDATE tool_invocations SET manifest_id = ? WHERE invocation_id = ?"
        ),
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            update_statements[identity_column],
            (identity_value, invocation_id),
        )
