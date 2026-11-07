-- The user's weekly TODO list. A row holds only the list's own facts; whether
-- its work runs, waits or failed stays with the Action it starts, and its
-- organization is always read through its project.
CREATE TABLE todo_items (
    todo_id TEXT PRIMARY KEY CHECK (length(todo_id) > 0),
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    -- Deleting a project moves its TODOs to "no project". workspace_projects is
    -- keyed by project_id alone, and a key that also named user_id would null
    -- user_id too, so the key is project_id; the store writes only the user's
    -- own projects.
    project_id TEXT REFERENCES workspace_projects(project_id) ON DELETE SET NULL,
    -- The screen's idempotency key for a TODO it wrote.
    client_todo_id TEXT CHECK (client_todo_id IS NULL OR length(client_todo_id) > 0),
    text TEXT NOT NULL CHECK (length(text) > 0),
    author TEXT NOT NULL CHECK (author IN ('user', 'ai')),
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('written', 'suggestion', 'recurrence')),
    -- Provenance only: neither references a row, so purging the Suggestion or
    -- the previous instance leaves this TODO as it was written.
    suggestion_id TEXT,
    previous_todo_id TEXT,
    week_start TEXT NOT NULL,
    due_date TEXT,
    due_repeat TEXT CHECK (due_repeat IN ('none', 'weekly')),
    checked_by TEXT CHECK (checked_by IN ('user', 'ai')),
    checked_at TEXT,
    report_kind TEXT CHECK (report_kind IN ('continue', 'partial', 'needs_user')),
    report_note TEXT,
    report_run_id TEXT,
    report_at TEXT,
    deletion_request_id TEXT,
    deletion_request_reason TEXT,
    deletion_requested_at TEXT,
    deletion_request_state TEXT CHECK (deletion_request_state IN ('pending', 'declined')),
    deleted_by TEXT CHECK (deleted_by IN ('user', 'ai_approved_by_user')),
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    -- One TODO per screen write and per Suggestion. A recurrence's id derives
    -- from the previous instance's id, so the primary key alone keeps it to one.
    UNIQUE (user_id, client_todo_id),
    UNIQUE (user_id, suggestion_id),
    CHECK (
        (origin_kind = 'written' AND suggestion_id IS NULL AND previous_todo_id IS NULL)
        OR (origin_kind = 'suggestion' AND suggestion_id IS NOT NULL
            AND previous_todo_id IS NULL)
        OR (origin_kind = 'recurrence' AND suggestion_id IS NULL
            AND previous_todo_id IS NOT NULL)
    ),
    CHECK ((due_date IS NULL) = (due_repeat IS NULL)),
    CHECK ((checked_by IS NULL) = (checked_at IS NULL)),
    CHECK (
        (report_kind IS NULL) = (report_note IS NULL)
        AND (report_kind IS NULL) = (report_at IS NULL)
        AND (report_run_id IS NULL OR report_kind IS NOT NULL)
    ),
    CHECK (
        (deletion_request_id IS NULL) = (deletion_request_reason IS NULL)
        AND (deletion_request_id IS NULL) = (deletion_requested_at IS NULL)
        AND (deletion_request_id IS NULL) = (deletion_request_state IS NULL)
    ),
    CHECK ((deleted_by IS NULL) = (deleted_at IS NULL))
);

CREATE INDEX idx_todo_items_user_week ON todo_items(user_id, week_start);
CREATE INDEX idx_todo_items_user_deleted ON todo_items(user_id, deleted_at)
WHERE deleted_at IS NOT NULL;
CREATE INDEX idx_todo_items_project ON todo_items(project_id);

-- A per-user change counter that the live session follows, as it follows
-- chat_items. Triggers bump it so a change made outside the store -- a project
-- deletion moving TODOs to "no project" -- is counted too. A write that changes
-- nothing must not touch the row.
CREATE TABLE todo_revisions (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0)
);

CREATE TRIGGER todo_items_count_insert
AFTER INSERT ON todo_items
BEGIN
    INSERT INTO todo_revisions(user_id, revision) VALUES (NEW.user_id, 1)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

-- Deleting the user cascades here as well, possibly after its counter went;
-- the guard keeps that cascade from writing a counter for a missing user.
CREATE TRIGGER todo_items_count_update
AFTER UPDATE ON todo_items
BEGIN
    INSERT INTO todo_revisions(user_id, revision)
    SELECT NEW.user_id, 1 WHERE EXISTS (SELECT 1 FROM users WHERE user_id = NEW.user_id)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER todo_items_count_delete
AFTER DELETE ON todo_items
BEGIN
    INSERT INTO todo_revisions(user_id, revision)
    SELECT OLD.user_id, 1 WHERE EXISTS (SELECT 1 FROM users WHERE user_id = OLD.user_id)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;
