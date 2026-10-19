PRAGMA foreign_keys = OFF;

-- Connected integration accounts keep their permissions here, one row per scope:
-- 'integration_read:<toolkit>:<account>', 'integration_write:<toolkit>:<account>'
-- and 'integration_tool:<account>:<slug>'. 'never_allow' turns a scope off.
-- An integration write asks the user every time, so no row may save it as
-- always_allow; the repository raises its own error before this CHECK fires.
CREATE TABLE approval_preferences_v0130 (
    preference_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type = 'global'),
    scope_ref TEXT CHECK (scope_ref IS NULL),
    approval_mode TEXT NOT NULL CHECK (
        approval_mode IN ('prompt_each_time', 'always_allow', 'never_allow')
    ),
    applies_to_json TEXT NOT NULL CHECK (json_valid(applies_to_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL CHECK (updated_by = 'user'),
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    CHECK (scope_type = 'global' AND scope_ref IS NULL),
    CHECK (
        approval_mode <> 'always_allow'
        OR instr(applies_to_json, '"integration_write:') = 0
    )
);

INSERT INTO approval_preferences_v0130(
    preference_id, user_id, scope_type, scope_ref, approval_mode,
    applies_to_json, created_at, updated_at, updated_by, revoked_at
)
SELECT
    preference_id, user_id, scope_type, scope_ref, approval_mode,
    applies_to_json, created_at, updated_at, updated_by, revoked_at
FROM approval_preferences;

DROP TABLE approval_preferences;
ALTER TABLE approval_preferences_v0130 RENAME TO approval_preferences;

CREATE INDEX idx_approval_preferences_user_scope
ON approval_preferences(user_id, scope_type, scope_ref, updated_at DESC);

PRAGMA foreign_keys = ON;
