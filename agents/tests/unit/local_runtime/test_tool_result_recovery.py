from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import pantaray_agents.local_runtime.tooling.tool_result_recovery as tool_result_recovery
from pantaray_agents.local_runtime.tooling.tool_result_recovery import (
    ToolResultRecoveryError,
    reconcile_tool_results_for_startup,
)

_JSON_FILE_NAME = f"output-{'1' * 32}.json"
_BINARY_FILE_NAME = f"output-{'2' * 32}.bin"
_ORPHAN_FILE_NAME = f"output-{'3' * 32}.json"
_TEMP_FILE_NAME = f".output-{'4' * 32}.tmp"


def _create_reference_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE tool_invocations (
                invocation_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                step_id TEXT
            );
            CREATE TABLE tool_outputs (
                output_id TEXT PRIMARY KEY,
                invocation_id TEXT NOT NULL,
                output_json TEXT,
                output_storage_kind TEXT
            );
            CREATE TABLE agent_action_steps (
                step_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                tool_output TEXT,
                runtime_state_checkpoint TEXT
            );
            """
        )


def _tool_results_root(db_path: Path, *, action_id: str = "action-1") -> Path:
    return (
        db_path.parent
        / "local_runtime_workspaces"
        / "scratch"
        / "user-1"
        / action_id
        / "tool-results"
    )


def _write_result(
    root: Path, *, owner: str, file_name: str, content: bytes = b"result"
) -> Path:
    path = root / owner / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _json_metadata(path: Path) -> dict[str, object]:
    return {
        "storage": "action_file",
        "path": str(path.absolute()),
        "media_type": "application/json",
        "byte_size": path.stat().st_size,
        "character_count": 6,
        "line_count": 1,
    }


def _binary_metadata(path: Path) -> dict[str, object]:
    return {
        "storage": "action_file",
        "path": str(path.absolute()),
        "media_type": "application/octet-stream",
        "byte_size": path.stat().st_size,
    }


def _preflight_tool_output(
    metadata: dict[str, object], *, tool_request_id: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "processing",
        "output_storage_kind": "inline_json",
        "output_owner_kind": "action_step",
        "output": {
            "kind": "approval_required",
            "tool_request_id": tool_request_id,
            "command_summary": metadata,
            "command_summary_storage_kind": "action_file",
        },
    }


def test_recovery_preserves_all_terminal_and_preflight_reference_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    root = _tool_results_root(db_path)
    terminal_json = _write_result(root, owner="invocation-1", file_name=_JSON_FILE_NAME)
    terminal_binary = _write_result(
        root, owner="invocation-2", file_name=_BINARY_FILE_NAME
    )
    preflight = _write_result(
        root, owner="request-preflight", file_name=_ORPHAN_FILE_NAME
    )
    completed_step = _write_result(
        root,
        owner="step-completed",
        file_name=f"output-{'5' * 32}.json",
    )
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO tool_invocations(invocation_id, user_id, action_id) "
            "VALUES (?, 'user-1', 'action-1')",
            (("invocation-1",), ("invocation-2",)),
        )
        connection.executemany(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'action_file')",
            (
                ("output-1", "invocation-1", json.dumps(_json_metadata(terminal_json))),
                (
                    "output-2",
                    "invocation-2",
                    json.dumps(_binary_metadata(terminal_binary)),
                ),
            ),
        )
        connection.execute(
            "UPDATE tool_invocations SET step_id = ? WHERE invocation_id = ?",
            ("step-invocation-alias", "invocation-1"),
        )
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, ?)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    _preflight_tool_output(
                        _json_metadata(preflight),
                        tool_request_id="request-preflight",
                    )
                ),
                json.dumps(
                    {"history_by_scope": {"S": [{"output": _json_metadata(preflight)}]}}
                ),
            ),
        )
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-completed",
                "user-1",
                "action-1",
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": _json_metadata(completed_step),
                        "output_storage_kind": "action_file",
                        "output_owner_kind": "action_step",
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-invocation-alias",
                "user-1",
                "action-1",
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": _json_metadata(terminal_json),
                        "output_storage_kind": "action_file",
                        "output_owner_kind": "tool_invocation",
                    }
                ),
            ),
        )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert terminal_json.is_file()
    assert terminal_binary.is_file()
    assert preflight.is_file()
    assert completed_step.is_file()
    assert result.preserved_file_count == 4
    assert result.removed_file_count == 0


def test_recovery_removes_every_managed_orphan_and_empty_owner_directory(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    root = _tool_results_root(db_path)
    json_path = _write_result(root, owner="json-owner", file_name=_JSON_FILE_NAME)
    binary_path = _write_result(root, owner="binary-owner", file_name=_BINARY_FILE_NAME)
    temp_path = _write_result(root, owner="temp-owner", file_name=_TEMP_FILE_NAME)
    empty_owner = root / "empty-owner"
    empty_owner.mkdir(parents=True)

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert not json_path.exists()
    assert not binary_path.exists()
    assert not temp_path.exists()
    assert not empty_owner.exists()
    assert result.removed_file_count == 3
    assert result.removed_directory_count == 4


def test_recovery_keeps_referenced_file_while_removing_sibling_orphan(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    root = _tool_results_root(db_path)
    referenced = _write_result(root, owner="shared-owner", file_name=_JSON_FILE_NAME)
    orphan = _write_result(root, owner="shared-owner", file_name=_ORPHAN_FILE_NAME)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    _preflight_tool_output(
                        _json_metadata(referenced), tool_request_id="shared-owner"
                    )
                ),
            ),
        )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert referenced.is_file()
    assert not orphan.exists()
    assert referenced.parent.is_dir()
    assert result.preserved_file_count == 1
    assert result.removed_file_count == 1
    assert result.removed_directory_count == 0


def test_recovery_scans_filesystem_when_action_database_rows_are_gone(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path), owner="deleted-action", file_name=_JSON_FILE_NAME
    )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert not orphan.exists()
    assert result.removed_file_count == 1


def test_recovery_does_not_treat_checkpoint_history_as_a_second_reference_ssot(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path), owner="stale-checkpoint", file_name=_JSON_FILE_NAME
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, NULL, ?)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps({"history": [{"output": _json_metadata(orphan)}]}),
            ),
        )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert not orphan.exists()
    assert result.removed_file_count == 1


def test_recovery_does_not_treat_nested_external_json_as_internal_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path),
        owner="invocation-external",
        file_name=_JSON_FILE_NAME,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations(invocation_id, user_id, action_id) "
            "VALUES (?, 'user-1', 'action-1')",
            ("invocation-external",),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'inline_json')",
            (
                "output-external",
                "invocation-external",
                json.dumps({"external_record": _json_metadata(orphan)}),
            ),
        )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert not orphan.exists()
    assert result.removed_file_count == 1


def test_recovery_ignores_action_file_shaped_inline_json(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path),
        owner="invocation-inline",
        file_name=_JSON_FILE_NAME,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations(invocation_id, user_id, action_id) "
            "VALUES (?, 'user-1', 'action-1')",
            ("invocation-inline",),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'inline_json')",
            (
                "output-inline",
                "invocation-inline",
                json.dumps(_json_metadata(orphan)),
            ),
        )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert not orphan.exists()
    assert result.removed_file_count == 1


def test_recovery_rejects_invocation_owned_formal_output_without_linked_output(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    result_path = _write_result(
        _tool_results_root(db_path),
        owner="invocation-missing",
        file_name=_JSON_FILE_NAME,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": _json_metadata(result_path),
                        "output_storage_kind": "action_file",
                        "output_owner_kind": "tool_invocation",
                    }
                ),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="linked tool_outputs"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert result_path.is_file()


def test_recovery_accepts_exact_linked_invocation_owned_formal_output(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    result_path = _write_result(
        _tool_results_root(db_path),
        owner="invocation-linked",
        file_name=_JSON_FILE_NAME,
    )
    metadata = _json_metadata(result_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations VALUES (?, ?, ?, ?)",
            ("invocation-linked", "user-1", "action-1", "step-1"),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'action_file')",
            ("output-linked", "invocation-linked", json.dumps(metadata)),
        )
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": metadata,
                        "output_storage_kind": "action_file",
                        "output_owner_kind": "tool_invocation",
                    }
                ),
            ),
        )

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert result_path.is_file()
    assert result.preserved_file_count == 1


def test_recovery_fails_closed_for_action_file_kind_with_inline_payload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path), owner="orphan", file_name=_ORPHAN_FILE_NAME
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations(invocation_id, user_id, action_id) "
            "VALUES (?, 'user-1', 'action-1')",
            ("invocation-invalid",),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'action_file')",
            (
                "output-invalid",
                "invocation-invalid",
                json.dumps({"status": "success"}),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="storage marker"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert orphan.is_file()


def test_recovery_fails_before_deletion_for_malformed_durable_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path), owner="orphan", file_name=_ORPHAN_FILE_NAME
    )
    malformed = _json_metadata(orphan)
    del malformed["line_count"]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(_preflight_tool_output(malformed, tool_request_id="orphan")),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="metadata shape"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert orphan.is_file()


def test_recovery_rejects_cross_action_references_before_deletion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    action_1_orphan = _write_result(
        _tool_results_root(db_path, action_id="action-1"),
        owner="same-owner",
        file_name=_JSON_FILE_NAME,
    )
    action_2_result = _write_result(
        _tool_results_root(db_path, action_id="action-2"),
        owner="same-owner",
        file_name=_JSON_FILE_NAME,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    _preflight_tool_output(
                        _json_metadata(action_2_result), tool_request_id="same-owner"
                    )
                ),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="outside its Action"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert action_1_orphan.is_file()
    assert action_2_result.is_file()


def test_recovery_rejects_cross_action_terminal_metadata_before_deletion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    action_1_orphan = _write_result(
        _tool_results_root(db_path, action_id="action-1"),
        owner="invocation-1",
        file_name=_ORPHAN_FILE_NAME,
    )
    action_2_result = _write_result(
        _tool_results_root(db_path, action_id="action-2"),
        owner="invocation-1",
        file_name=_JSON_FILE_NAME,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations(invocation_id, user_id, action_id) "
            "VALUES (?, 'user-1', 'action-1')",
            ("invocation-1",),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'action_file')",
            (
                "output-1",
                "invocation-1",
                json.dumps(_json_metadata(action_2_result)),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="outside its Action"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert action_1_orphan.is_file()
    assert action_2_result.is_file()


def test_recovery_rejects_storage_tag_without_metadata_before_deletion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path), owner="orphan", file_name=_ORPHAN_FILE_NAME
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    _preflight_tool_output(
                        {"storage": "action_file"}, tool_request_id="orphan"
                    )
                ),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="media_type"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert orphan.is_file()


def test_recovery_rejects_incomplete_formal_envelope_before_deletion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    orphan = _write_result(
        _tool_results_root(db_path), owner="orphan", file_name=_ORPHAN_FILE_NAME
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    {
                        "status": "success",
                        "output": {"message": "legacy"},
                        "output_storage_kind": "inline_json",
                        "output_owner_kind": "action_step",
                    }
                ),
            ),
        )

    with pytest.raises(ToolResultRecoveryError, match="formal tool-step contract"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert orphan.is_file()


def test_success_output_shaped_like_approval_is_not_a_storage_reference(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    result_path = _write_result(
        _tool_results_root(db_path),
        owner="ordinary-output",
        file_name=_JSON_FILE_NAME,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": {
                            "kind": "approval_required",
                            "tool_request_id": "ordinary-output",
                            "command_summary": _json_metadata(result_path),
                            "command_summary_storage_kind": "action_file",
                        },
                        "output_storage_kind": "inline_json",
                        "output_owner_kind": "action_step",
                    }
                ),
            ),
        )

    recovery = reconcile_tool_results_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert not result_path.exists()
    assert recovery.removed_file_count == 1


def test_recovery_never_follows_symlinks_or_deletes_unknown_entries(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    root = _tool_results_root(db_path)
    orphan = _write_result(root, owner="mixed-owner", file_name=_ORPHAN_FILE_NAME)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    symlink_path = orphan.parent / "unknown-link"
    os.symlink(outside, symlink_path)
    outside_owner = tmp_path / "outside-owner"
    outside_owner.mkdir()
    outside_result = outside_owner / _JSON_FILE_NAME
    outside_result.write_bytes(b"keep")
    owner_symlink = root / "linked-owner"
    os.symlink(outside_owner, owner_symlink)

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert not orphan.exists()
    assert symlink_path.is_symlink()
    assert owner_symlink.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"
    assert outside_result.is_file()
    assert result.unknown_entry_count == 2
    assert result.removed_directory_count == 0


def test_recovery_fails_closed_if_referenced_file_becomes_a_symlink_after_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    referenced = _write_result(
        _tool_results_root(db_path), owner="referenced", file_name=_JSON_FILE_NAME
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_action_steps VALUES (?, ?, ?, ?, NULL)",
            (
                "step-1",
                "user-1",
                "action-1",
                json.dumps(
                    _preflight_tool_output(
                        _json_metadata(referenced), tool_request_id="referenced"
                    )
                ),
            ),
        )
    original_scan = tool_result_recovery._scan_managed_storage

    def _scan_then_replace(
        *, workspace_root: Path
    ) -> tuple[tuple[tool_result_recovery._InvocationDirectory, ...], int]:
        scanned = original_scan(workspace_root=workspace_root)
        referenced.unlink()
        os.symlink(outside, referenced)
        return scanned

    monkeypatch.setattr(
        tool_result_recovery,
        "_scan_managed_storage",
        _scan_then_replace,
    )

    with pytest.raises(ToolResultRecoveryError, match="changed during"):
        reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert referenced.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_recovery_continues_cleanup_when_referenced_files_are_missing(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    root = _tool_results_root(db_path)
    orphan = _write_result(root, owner="orphan", file_name=_ORPHAN_FILE_NAME)
    kept = _write_result(root, owner="request-kept", file_name=_JSON_FILE_NAME)
    missing_shared = root / "invocation-shared" / _JSON_FILE_NAME
    missing_preflight = root / "request-missing" / _BINARY_FILE_NAME
    missing_preflight.parent.mkdir(parents=True)
    shared_metadata = {
        "storage": "action_file",
        "path": str(missing_shared.absolute()),
        "media_type": "application/json",
        "byte_size": 1,
        "character_count": 1,
        "line_count": 1,
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations VALUES (?, ?, ?, ?)",
            ("invocation-shared", "user-1", "action-1", "step-shared"),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, 'action_file')",
            ("output-shared", "invocation-shared", json.dumps(shared_metadata)),
        )
        connection.executemany(
            "INSERT INTO agent_action_steps VALUES (?, 'user-1', 'action-1', ?, NULL)",
            (
                (
                    "step-shared",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "success",
                            "output": shared_metadata,
                            "output_storage_kind": "action_file",
                            "output_owner_kind": "tool_invocation",
                        }
                    ),
                ),
                (
                    "step-missing",
                    json.dumps(
                        _preflight_tool_output(
                            {
                                "storage": "action_file",
                                "path": str(missing_preflight.absolute()),
                                "media_type": "application/octet-stream",
                                "byte_size": 1,
                            },
                            tool_request_id="request-missing",
                        )
                    ),
                ),
                (
                    "step-kept",
                    json.dumps(
                        _preflight_tool_output(
                            _json_metadata(kept), tool_request_id="request-kept"
                        )
                    ),
                ),
            ),
        )

    with caplog.at_level("WARNING", logger=tool_result_recovery.__name__):
        result = reconcile_tool_results_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )

    assert result.missing_reference_count == 3
    assert not orphan.exists()
    assert kept.is_file()
    assert result.removed_file_count == 1
    assert result.preserved_file_count == 1
    [record] = caplog.records
    assert json.loads(record.getMessage()) == {
        "evt": "TOOL_RESULT_FILES_MISSING_AT_STARTUP",
        "component": "local_runtime.tooling.tool_result_recovery",
        "missing_reference_count": 3,
        "missing_tool_invocation_reference_count": 1,
        "missing_action_step_reference_count": 2,
    }


def test_recovery_uses_canonical_db_parent_for_referenced_results(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    db_path = real_parent / "runtime.db"
    _create_reference_db(db_path)
    referenced = _write_result(
        _tool_results_root(db_path), owner="referenced", file_name=_JSON_FILE_NAME
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tool_invocations(invocation_id, user_id, action_id) "
            "VALUES (?, ?, ?)",
            ("referenced", "user-1", "action-1"),
        )
        connection.execute(
            "INSERT INTO tool_outputs VALUES (?, ?, ?, ?)",
            (
                "output-1",
                "referenced",
                json.dumps(_json_metadata(referenced)),
                "action_file",
            ),
        )
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    result = reconcile_tool_results_for_startup(
        db_path=alias_parent / db_path.name,
        busy_timeout_ms=1_000,
    )

    assert referenced.is_file()
    assert result.preserved_file_count == 1


def test_recovery_upgrades_existing_managed_directories_to_owner_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_reference_db(db_path)
    owner_path = _tool_results_root(db_path) / "owner"
    owner_path.mkdir(parents=True)
    (owner_path / "unknown.txt").write_text("keep", encoding="utf-8")
    managed_paths = (
        owner_path.parents[3],
        owner_path.parents[2],
        owner_path.parents[1],
        owner_path.parent,
        owner_path,
    )
    for path in managed_paths:
        path.chmod(0o755)

    result = reconcile_tool_results_for_startup(db_path=db_path, busy_timeout_ms=1_000)

    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in managed_paths)
    assert result.unknown_entry_count == 1
