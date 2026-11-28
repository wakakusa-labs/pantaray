-- The activity watcher's microphone state: each app's current use of the
-- audio input as Zanei recorded it, and the microphone hooks each use fired.
--
-- A use runs from an audio_input.start until a stop followed by a quiet
-- stretch with no start, or the app's quit; a start within that stretch goes
-- on with the same use as a new stretch. stopped_at is NULL while the input is
-- in use. The app and window are those of the start of the current stretch.
CREATE TABLE microphone_uses (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    bundle_id TEXT NOT NULL,
    app_name TEXT,
    window_title TEXT,
    use_started_at TEXT NOT NULL,
    stretch_started_at TEXT NOT NULL,
    stopped_at TEXT,
    PRIMARY KEY (user_id, bundle_id)
);

-- A hook fired in the app's current use; it goes when the use ends, and when
-- the hook's watch does (switched, replaced), whose new start then keeps the
-- hook from firing in a use begun before it.
CREATE TABLE microphone_hook_uses (
    user_id TEXT NOT NULL,
    hook_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    PRIMARY KEY (user_id, hook_id, bundle_id),
    FOREIGN KEY (user_id, hook_id)
        REFERENCES activity_hook_watches(user_id, hook_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, bundle_id)
        REFERENCES microphone_uses(user_id, bundle_id) ON DELETE CASCADE
);
