-- What the owner decided about each hook. A hook without a row is enabled, and
-- a task without an approved hash never fires: a task fires only while its
-- file's content still hashes to the one the owner approved.
CREATE TABLE hook_settings (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    hook_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    approved_hash TEXT,
    PRIMARY KEY (user_id, hook_id)
);

-- The conversation a task sends its prompt to, and how its turns went. The
-- latest turn's result is NULL until that turn ends. Three failures in a row
-- stop the task with the reason of the last one, until the owner enables it.
CREATE TABLE hook_conversations (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    hook_id TEXT NOT NULL,
    action_id TEXT REFERENCES agent_actions(action_id) ON DELETE SET NULL,
    last_message_id TEXT,
    last_result TEXT CHECK (
        last_result IN ('completed', 'failed', 'canceled', 'not_started')
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    stopped_reason TEXT CHECK (stopped_reason IN ('start_failed', 'turn_failed')),
    PRIMARY KEY (user_id, hook_id)
);
