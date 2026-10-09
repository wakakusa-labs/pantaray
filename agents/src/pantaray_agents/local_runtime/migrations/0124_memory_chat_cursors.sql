-- How far the unified Memory runs have taken each user's chat: the sequence of
-- the last chat item a run was handed. A run's range is pinned in its job
-- payload, and the cursor moves when that job is enqueued.
CREATE TABLE memory_chat_cursors (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    through_sequence INTEGER NOT NULL CHECK (through_sequence >= 0),
    updated_at TEXT NOT NULL
);
