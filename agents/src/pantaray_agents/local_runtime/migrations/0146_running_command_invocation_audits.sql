PRAGMA foreign_keys = OFF;

-- A command that keeps running after its bash call returns is audited when the
-- call returns, as 'running', and its end is written to the same row later.
-- A running row has no exit code or signal yet. SQLite cannot change a CHECK in
-- place, so the table is rebuilt with its indexes. Existing rows are copied
-- unchanged.
CREATE TABLE command_invocation_audits_v0146 (
    invocation_id TEXT PRIMARY KEY,
    approval_session_id TEXT,
    execution_kind TEXT NOT NULL CHECK (
        execution_kind IN ('agent_generated', 'workspace_command')
    ),
    executable_source_kind TEXT NOT NULL CHECK (
        executable_source_kind IN (
            'app_runtime_python',
            'trusted_system_executable',
            'workspace_managed_executable'
        )
    ),
    resolved_executable_path TEXT NOT NULL,
    terminal_outcome TEXT NOT NULL CHECK (
        terminal_outcome IN (
            'running',
            'exited',
            'signaled',
            'timed_out',
            'canceled',
            'sandbox_violation',
            'spawn_failed',
            'budget_exceeded',
            'broker_failed'
        )
    ),
    exit_code INTEGER,
    signal INTEGER,
    stdout_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stdout_bytes >= 0),
    stderr_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stderr_bytes >= 0),
    stdout_max_bytes INTEGER NOT NULL CHECK (stdout_max_bytes > 0),
    stderr_max_bytes INTEGER NOT NULL CHECK (stderr_max_bytes > 0),
    temp_storage_limit_bytes INTEGER NOT NULL CHECK (temp_storage_limit_bytes > 0),
    child_count_limit INTEGER NOT NULL CHECK (child_count_limit > 0),
    open_file_lease_limit INTEGER NOT NULL CHECK (open_file_lease_limit > 0),
    budget_exceeded_kind TEXT CHECK (
        budget_exceeded_kind IS NULL
        OR budget_exceeded_kind IN (
            'stdout_limit',
            'stderr_limit',
            'temp_storage_limit',
            'child_count_limit',
            'open_file_lease_limit'
        )
    ),
    sandbox_violation_kind TEXT CHECK (
        sandbox_violation_kind IS NULL
        OR sandbox_violation_kind IN (
            'path_escape',
            'write_denied',
            'exec_denied',
            'network_denied',
            'unknown'
        )
    ),
    sandbox_violation_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (invocation_id) REFERENCES tool_invocations(invocation_id) ON DELETE CASCADE,
    FOREIGN KEY (approval_session_id) REFERENCES approval_sessions(approval_session_id) ON DELETE RESTRICT,
    CHECK (
        terminal_outcome <> 'running'
        OR (exit_code IS NULL AND signal IS NULL)
    )
);

INSERT INTO command_invocation_audits_v0146(
    invocation_id, approval_session_id, execution_kind, executable_source_kind,
    resolved_executable_path, terminal_outcome, exit_code, signal, stdout_bytes,
    stderr_bytes, stdout_max_bytes, stderr_max_bytes, temp_storage_limit_bytes,
    child_count_limit, open_file_lease_limit, budget_exceeded_kind,
    sandbox_violation_kind, sandbox_violation_summary, created_at, updated_at
)
SELECT
    invocation_id, approval_session_id, execution_kind, executable_source_kind,
    resolved_executable_path, terminal_outcome, exit_code, signal, stdout_bytes,
    stderr_bytes, stdout_max_bytes, stderr_max_bytes, temp_storage_limit_bytes,
    child_count_limit, open_file_lease_limit, budget_exceeded_kind,
    sandbox_violation_kind, sandbox_violation_summary, created_at, updated_at
FROM command_invocation_audits;

DROP TABLE command_invocation_audits;
ALTER TABLE command_invocation_audits_v0146 RENAME TO command_invocation_audits;

CREATE INDEX idx_command_invocation_audits_outcome_created
ON command_invocation_audits(terminal_outcome, created_at DESC);

CREATE INDEX idx_command_invocation_audits_exec_kind_created
ON command_invocation_audits(execution_kind, created_at DESC);

PRAGMA foreign_keys = ON;
