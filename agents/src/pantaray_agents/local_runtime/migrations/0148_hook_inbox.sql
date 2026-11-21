-- Events waiting for the hooks that listen to them. An event is written once
-- per listening hook, in the transaction of the write it records, and deleted
-- when the hook hands it to a run (in the transaction that enqueues the run)
-- or drops it.
CREATE TABLE hook_inbox (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    occurred_at TEXT NOT NULL
);

CREATE INDEX idx_hook_inbox_hook ON hook_inbox(user_id, hook_id, delivery_id);
