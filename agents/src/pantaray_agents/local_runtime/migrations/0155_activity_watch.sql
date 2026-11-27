-- The activity watcher's own state: its read position in the recorder's
-- store, the last form it saw of each window (keyed by Zanei's window id, or
-- by the app's bundle id until the app shows one), and, for each activity hook
-- it watches, the windows in a match that has not ended.
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
-- replaced definition is watched afresh, and so is one switched on again.
-- Records before watched_from only update its matches, so a window already
-- matching, or one that started while the hook was off, never fires it.
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
-- not matching since, NULL while its last form matches. A match goes with its
-- window's form, and follows it when the form moves to another key.
CREATE TABLE activity_hook_windows (
    user_id TEXT NOT NULL,
    hook_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    unmatched_since TEXT,
    PRIMARY KEY (user_id, hook_id, window_key),
    FOREIGN KEY (user_id, hook_id)
        REFERENCES activity_hook_watches(user_id, hook_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, window_key)
        REFERENCES activity_windows(user_id, window_key)
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- Whatever writes the switch (the owner, the Action's yes, a stop after
-- failures), a hook that starts or stops listening loses its watch here.
CREATE TRIGGER user_hooks_listening_resets_activity_watch
AFTER UPDATE OF enabled, stopped_reason ON user_hooks
WHEN (OLD.enabled = 1 AND OLD.stopped_reason IS NULL)
    IS NOT (NEW.enabled = 1 AND NEW.stopped_reason IS NULL)
BEGIN
    DELETE FROM activity_hook_watches
    WHERE user_id = NEW.user_id AND hook_id = NEW.hook_id;
END;
