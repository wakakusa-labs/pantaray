from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    apply_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ToolInvocationStartInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)
from pantaray_agents.schema.agent.base import JSONValue

from .support import _migrations_before, load_default_migrations

MIGRATION_NAME = "0074_tool_output_storage_kind.sql"
TOOL_STEP_REMOVAL_MIGRATION_NAME = "0076_remove_legacy_tool_action_steps.sql"
ACTION_CREATION_CUTOVER_MIGRATION_NAME = "0082_action_creation_cutover.sql"
RETIRED_FORMAL_ENVELOPE_MIGRATION_NAME = "0075_formal_tool_step_output_envelope.sql"
RETIRED_FORMAL_ENVELOPE_CHECKSUM = (
    "b35770ae305a15e717670f2083387d301576a7d76c64a8415b6c890d4e2d74b6"
)
RETIRED_CUTOVER_MIGRATION_NAME = "0075_formal_tool_step_cutover.sql"
RETIRED_CUTOVER_CHECKSUM = (
    "d99a61325c2278fd4290af2130af90d66de85dc1283e34602b36127393e361db"
)


def _insert_pre_action_creation_cutover_action(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-1', 'ja', '2026-08-11T00:00:00Z',
                        '2026-08-11T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id, user_id, status, created_at, updated_at
                ) VALUES (
                    'suggestion-1', 'user-1', 'processing',
                    '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id, user_id, suggestion_id, status, final_output,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES (
                    'action-1', 'user-1', 'suggestion-1', 'processing', '',
                    'test/tooling', 'v1',
                    '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z'
                )
                """
            )


def test_migration_backfills_terminal_and_preflight_storage_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    migration_index = next(
        index
        for index, migration in enumerate(migrations)
        if migration.name == MIGRATION_NAME
    )
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations[:migration_index],
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_pre_action_creation_cutover_action(db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-08-11T00:00:00Z",
        allowed_tool_ids=("bash",),
    )
    terminal_metadata = _json_metadata(
        context.tool_results_path / "invocation-file" / f"output-{'1' * 32}.json"
    )
    preflight_metadata = _json_metadata(
        context.tool_results_path / "request-1" / f"output-{'2' * 32}.json"
    )
    foreign_metadata = _json_metadata(
        context.tool_results_path / "another-owner" / f"output-{'3' * 32}.json"
    )
    external_metadata = _json_metadata(
        tmp_path / "outside" / "invocation-external" / f"output-{'5' * 32}.json"
    )
    extra_key_metadata = _json_metadata(
        context.tool_results_path / "invocation-extra" / f"output-{'6' * 32}.json"
    )
    extra_key_metadata["unexpected"] = True
    binary_metadata = _binary_metadata(
        context.tool_results_path / "invocation-binary" / f"output-{'4' * 32}.bin"
    )
    for invocation_id in (
        "invocation-file",
        "invocation-inline",
        "invocation-foreign",
        "invocation-external",
        "invocation-extra",
        "invocation-binary",
        "invocation-null",
    ):
        _insert_invocation(
            db_path=db_path,
            context=context,
            invocation_id=invocation_id,
        )
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO tool_outputs(
                output_id,
                invocation_id,
                output_json,
                redaction_applied,
                created_at
            ) VALUES (?, ?, ?, 0, '2026-08-11T00:00:01Z')
            """,
            (
                ("output-file", "invocation-file", json.dumps(terminal_metadata)),
                (
                    "output-inline",
                    "invocation-inline",
                    json.dumps({"storage": "action_file"}),
                ),
                (
                    "output-foreign",
                    "invocation-foreign",
                    json.dumps(foreign_metadata),
                ),
                (
                    "output-external",
                    "invocation-external",
                    json.dumps(external_metadata),
                ),
                (
                    "output-extra",
                    "invocation-extra",
                    json.dumps(extra_key_metadata),
                ),
                (
                    "output-binary",
                    "invocation-binary",
                    json.dumps(binary_metadata),
                ),
                ("output-null", "invocation-null", None),
            ),
        )
        connection.executemany(
            """
            INSERT INTO agent_action_steps(
                step_id,
                action_id,
                user_id,
                step_number,
                step_type,
                step_name,
                status,
                tool_output,
                created_at
            ) VALUES (?, 'action-1', 'user-1', ?, 'tool_execution', 'bash',
                      'processing', ?, '2026-08-11T00:00:01Z')
            """,
            (
                (
                    "step-file",
                    1,
                    json.dumps(_approval_output(preflight_metadata)),
                ),
                (
                    "step-inline",
                    2,
                    json.dumps(_approval_output({"command": "pwd"})),
                ),
                (
                    "step-foreign",
                    3,
                    json.dumps(_approval_output(foreign_metadata)),
                ),
                (
                    "step-binary",
                    4,
                    json.dumps(_approval_output(binary_metadata)),
                ),
            ),
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations[: migration_index + 1],
    )

    with sqlite3.connect(db_path) as connection:
        output_rows = connection.execute(
            """
            SELECT output_id, output_storage_kind
            FROM tool_outputs
            ORDER BY output_id
            """
        ).fetchall()
        step_rows = connection.execute(
            """
            SELECT step_id, json_extract(
                tool_output,
                '$.output.command_summary_storage_kind'
            )
            FROM agent_action_steps
            ORDER BY step_id
            """
        ).fetchall()

    assert output_rows == [
        ("output-binary", "inline_json"),
        ("output-external", "inline_json"),
        ("output-extra", "inline_json"),
        ("output-file", "action_file"),
        ("output-foreign", "inline_json"),
        ("output-inline", "inline_json"),
        ("output-null", None),
    ]
    assert step_rows == [
        ("step-binary", "inline_json"),
        ("step-file", "action_file"),
        ("step-foreign", "inline_json"),
        ("step-inline", "inline_json"),
    ]


def test_v74_upgrade_removes_only_tool_action_steps(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    migration_index = next(
        index
        for index, migration in enumerate(migrations)
        if migration.name == TOOL_STEP_REMOVAL_MIGRATION_NAME
    )
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations[:migration_index],
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_pre_action_creation_cutover_action(db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-08-11T00:00:00Z",
        allowed_tool_ids=("bash",),
    )
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO agent_action_steps(
                step_id,
                action_id,
                user_id,
                step_number,
                step_type,
                step_name,
                status,
                tool_output,
                created_at
            ) VALUES (?, 'action-1', 'user-1', ?, ?, ?, 'success', ?, ?)
            """,
            (
                (
                    "legacy-tool-step",
                    1,
                    "tool_execution",
                    "bash",
                    json.dumps({"status": "success", "output": {"value": "legacy"}}),
                    "2026-08-11T00:00:01Z",
                ),
                (
                    "retained-llm-step",
                    2,
                    "llm_output",
                    "assistant",
                    json.dumps({"text": "retained"}),
                    "2026-08-11T00:00:02Z",
                ),
            ),
        )
    _insert_invocation(
        db_path=db_path,
        context=context,
        invocation_id="retained-invocation",
        step_id="legacy-tool-step",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO tool_outputs(
                output_id,
                invocation_id,
                output_json,
                output_storage_kind,
                redaction_applied,
                created_at
            ) VALUES (
                'retained-output',
                'retained-invocation',
                '{"value":"retained"}',
                'inline_json',
                0,
                '2026-08-11T00:00:03Z'
            )
            """
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations,
            ACTION_CREATION_CUTOVER_MIGRATION_NAME,
        ),
    )

    with sqlite3.connect(db_path) as connection:
        steps = connection.execute(
            """
            SELECT step_id, step_type
            FROM agent_action_steps
            ORDER BY step_number
            """
        ).fetchall()
        invocation = connection.execute(
            "SELECT invocation_id, step_id FROM tool_invocations"
        ).fetchone()
        output = connection.execute(
            "SELECT output_id, invocation_id, output_json FROM tool_outputs"
        ).fetchone()
        action = connection.execute(
            "SELECT action_id FROM agent_actions WHERE action_id = 'action-1'"
        ).fetchone()
        schema_state = connection.execute(
            """
            SELECT current_version, migration_name
            FROM schema_versions
            WHERE component = 'local_runtime'
            """
        ).fetchone()
    assert steps == [("retained-llm-step", "llm_output")]
    assert invocation == ("retained-invocation", None)
    assert output == (
        "retained-output",
        "retained-invocation",
        '{"value":"retained"}',
    )
    assert action == ("action-1",)
    latest_migration = _migrations_before(
        migrations,
        ACTION_CREATION_CUTOVER_MIGRATION_NAME,
    )[-1]
    assert schema_state == (latest_migration.version, latest_migration.name)


def test_fresh_database_skips_retired_v75(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        schema_state = connection.execute(
            """
            SELECT current_version, migration_name
            FROM schema_versions
            WHERE component = 'local_runtime'
            """
        ).fetchone()
        retired_run_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM migration_journal
            WHERE migration_name IN (?, ?)
            """,
            (
                RETIRED_FORMAL_ENVELOPE_MIGRATION_NAME,
                RETIRED_CUTOVER_MIGRATION_NAME,
            ),
        ).fetchone()
    latest_migration = migrations[-1]
    assert schema_state == (latest_migration.version, latest_migration.name)
    assert retired_run_count == (0,)


@pytest.mark.parametrize(
    ("migration_name", "checksum"),
    (
        (
            RETIRED_FORMAL_ENVELOPE_MIGRATION_NAME,
            RETIRED_FORMAL_ENVELOPE_CHECKSUM,
        ),
        (RETIRED_CUTOVER_MIGRATION_NAME, RETIRED_CUTOVER_CHECKSUM),
    ),
)
def test_known_retired_v75_identity_upgrades_through_current_schema(
    tmp_path: Path,
    migration_name: str,
    checksum: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    migration_index = next(
        index
        for index, migration in enumerate(migrations)
        if migration.name == TOOL_STEP_REMOVAL_MIGRATION_NAME
    )
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations[:migration_index],
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_pre_action_creation_cutover_action(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id,
                action_id,
                user_id,
                step_number,
                step_type,
                step_name,
                status,
                tool_output,
                created_at
            ) VALUES (
                'retired-v75-tool-step',
                'action-1',
                'user-1',
                1,
                'tool_execution',
                'bash',
                'success',
                ?,
                '2026-08-11T00:00:01Z'
            )
            """,
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": {"value": "old"},
                        "output_storage_kind": "inline_json",
                        "output_owner_kind": "action_step",
                    }
                ),
            ),
        )
        connection.execute(
            "UPDATE agent_actions SET status = 'success' WHERE action_id = 'action-1'"
        )
        connection.execute(
            """
            UPDATE agent_suggestions
            SET status = 'success',
                action_execution_id = 'action-1',
                action_command_id = 'message-action-1'
            WHERE suggestion_id = 'suggestion-1'
            """
        )
        _set_schema_identity(
            connection=connection,
            migration_name=migration_name,
            checksum=checksum,
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        schema_state = connection.execute(
            """
            SELECT current_version, migration_name
            FROM schema_versions
            WHERE component = 'local_runtime'
            """
        ).fetchone()
        tool_step_count = connection.execute(
            "SELECT COUNT(*) FROM agent_action_steps WHERE step_type = 'tool_execution'"
        ).fetchone()
    latest_migration = migrations[-1]
    assert schema_state == (latest_migration.version, latest_migration.name)
    assert tool_step_count == (0,)


@pytest.mark.parametrize(
    ("migration_name", "checksum"),
    (
        (RETIRED_FORMAL_ENVELOPE_MIGRATION_NAME, "0" * 64),
        ("0075_unknown.sql", RETIRED_FORMAL_ENVELOPE_CHECKSUM),
    ),
)
def test_unknown_v75_identity_is_rejected(
    tmp_path: Path,
    migration_name: str,
    checksum: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    migration_index = next(
        index
        for index, migration in enumerate(migrations)
        if migration.name == TOOL_STEP_REMOVAL_MIGRATION_NAME
    )
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations[:migration_index],
    )
    with sqlite3.connect(db_path) as connection:
        _set_schema_identity(
            connection=connection,
            migration_name=migration_name,
            checksum=checksum,
        )

    with pytest.raises(MigrationError, match="not recognized by this build"):
        apply_migrations(
            db_path=db_path,
            busy_timeout_ms=1_000,
            migrations=migrations,
        )


def _insert_invocation(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    invocation_id: str,
    step_id: str | None = None,
) -> None:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=invocation_id,
            user_id="user-1",
            action_id="action-1",
            step_id=step_id or f"missing-{invocation_id}",
            tool_id="bash",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="process_exec_local",
            network_policy="cloud-proxy-only",
            command_summary_json={"command": "pwd"},
            capability_snapshot_json={"required_capabilities": []},
            request_json={"args": {"command": "pwd"}},
            status="running",
            started_at="2026-08-11T00:00:01Z",
        ),
    )


def _set_schema_identity(
    *,
    connection: sqlite3.Connection,
    migration_name: str,
    checksum: str,
) -> None:
    connection.execute(
        """
        UPDATE schema_versions
        SET current_version = 75,
            migration_name = ?,
            checksum = ?
        WHERE component = 'local_runtime'
        """,
        (migration_name, checksum),
    )


def _json_metadata(path: Path) -> dict[str, JSONValue]:
    return {
        "storage": "action_file",
        "path": str(path.absolute()),
        "media_type": "application/json",
        "byte_size": 21_000,
        "character_count": 21_000,
        "line_count": 1,
    }


def _binary_metadata(path: Path) -> dict[str, JSONValue]:
    return {
        "storage": "action_file",
        "path": str(path.absolute()),
        "media_type": "application/octet-stream",
        "byte_size": 21_000,
    }


def _approval_output(
    command_summary: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    return {
        "status": "processing",
        "output": {
            "kind": "approval_required",
            "tool_request_id": "request-1",
            "command_summary": command_summary,
        },
    }
