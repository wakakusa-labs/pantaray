-- The activity watcher's own state: where it has read the recorder's store to,
-- the last form it saw of each window, and, for each on activity hook it
-- watches, the windows in a match that has not ended. Read position, window
-- forms, match state and the events they fire commit in one transaction.
--
-- A window is keyed by Zanei's window id, or by its app's bundle id when the
-- app has shown no window with an id.
CREATE TABLE activity_watch_positions (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    cursor TEXT NOT NULL
);

CREATE TABLE activity_windows (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    window_key TEXT NOT NULL,
    bundle_id TEXT,
    app_name TEXT,
    window_title TEXT,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (user_id, window_key)
);

CREATE INDEX activity_windows_by_app ON activity_windows(user_id, bundle_id, seen_at);

-- A hook is watched from watched_from, for the version of it read then; a
-- replaced definition is watched afresh. Records before watched_from only
-- update its matches, so a window already matching never fires it.
CREATE TABLE activity_hook_watches (
    user_id TEXT NOT NULL,
    hook_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    watched_from TEXT NOT NULL,
    PRIMARY KEY (user_id, hook_id),
    FOREIGN KEY (user_id, hook_id)
        REFERENCES user_hooks(user_id, hook_id) ON DELETE CASCADE
);

-- A window in a match of the hook; unmatched_since is when it was first seen
-- not matching since, NULL while its last form matches.
CREATE TABLE activity_hook_windows (
    user_id TEXT NOT NULL,
    hook_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    unmatched_since TEXT,
    PRIMARY KEY (user_id, hook_id, window_key),
    FOREIGN KEY (user_id, hook_id)
        REFERENCES activity_hook_watches(user_id, hook_id) ON DELETE CASCADE
);
