PRAGMA foreign_keys = OFF;

-- The review's reason is kept whenever the review answered: on the row it
-- approved, and on a row it left for the user to decide, where the approval
-- panel shows it. A request that never needed the user keeps no reason. SQLite
-- cannot change a CHECK in place, so the table is rebuilt with its indexes, as
-- in 0143. Existing rows are copied unchanged.
CREATE TABLE approval_sessions_v0144 (
    approval_session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    manifest_id TEXT,
    tool_request_id TEXT NOT NULL,
    tool_invocation_id TEXT,
    tool_id TEXT NOT NULL,
    intent_class TEXT NOT NULL,
    approval_source TEXT NOT NULL CHECK (
        approval_source IN ('prompt', 'settings', 'review')
    ),
    review_reason TEXT CHECK (review_reason IS NULL OR length(review_reason) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved_once', 'denied', 'interrupted')
    ),
    approved_capabilities_json TEXT NOT NULL CHECK (json_valid(approved_capabilities_json)),
    command_summary_json TEXT NOT NULL CHECK (json_valid(command_summary_json)),
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (action_id) REFERENCES agent_actions(action_id) ON DELETE CASCADE,
    FOREIGN KEY (manifest_id) REFERENCES workspace_manifests(manifest_id) ON DELETE SET NULL,
    FOREIGN KEY (tool_invocation_id) REFERENCES tool_invocations(invocation_id) ON DELETE SET NULL,
    CHECK (
        (status = 'pending' AND decided_at IS NULL)
        OR (status != 'pending' AND decided_at IS NOT NULL)
    ),
    CHECK (claimed_at IS NULL OR tool_invocation_id IS NOT NULL),
    CHECK (
        status NOT IN ('pending', 'denied')
        OR (claimed_at IS NULL AND tool_invocation_id IS NULL)
    ),
    -- A review only ever approves, and always says why.
    CHECK (
        approval_source <> 'review'
        OR (status = 'approved_once' AND review_reason IS NOT NULL)
    ),
    CHECK (approval_source <> 'settings' OR review_reason IS NULL),
    UNIQUE (user_id, tool_request_id)
);

INSERT INTO approval_sessions_v0144(
    approval_session_id, user_id, action_id, manifest_id, tool_request_id,
    tool_invocation_id, tool_id, intent_class, approval_source, status,
    approved_capabilities_json, command_summary_json, requested_at, decided_at,
    created_at, claimed_at, review_reason
)
SELECT
    approval_session_id, user_id, action_id, manifest_id, tool_request_id,
    tool_invocation_id, tool_id, intent_class, approval_source, status,
    approved_capabilities_json, command_summary_json, requested_at, decided_at,
    created_at, claimed_at, review_reason
FROM approval_sessions;

DROP TABLE approval_sessions;
ALTER TABLE approval_sessions_v0144 RENAME TO approval_sessions;

CREATE INDEX idx_approval_sessions_action_status
ON approval_sessions(action_id, status, requested_at DESC);

CREATE INDEX idx_approval_sessions_tool_invocation_status
ON approval_sessions(tool_invocation_id, status, requested_at DESC);

CREATE INDEX idx_approval_sessions_user_tool_request_status
ON approval_sessions(user_id, tool_request_id, status, requested_at DESC);

PRAGMA foreign_keys = ON;
