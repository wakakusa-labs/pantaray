PRAGMA foreign_keys = OFF;

-- A TODO's start date may be unset, so TODOs added in bulk do not all start at
-- once: the AI picks one up only after the user gives it a date. SQLite cannot
-- drop NOT NULL in place, so the table is rebuilt with its indexes and the
-- triggers that bump the change counter. Foreign keys stay off so dropping the
-- old table does not cascade to its progress records.
CREATE TABLE todo_items_v0139 (
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
    -- No start date: the AI does not pick it up until the user sets one.
    starts_on TEXT,
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

INSERT INTO todo_items_v0139(
    todo_id, user_id, project_id, client_todo_id, text, author, origin_kind,
    suggestion_id, previous_todo_id, starts_on, due_date, due_repeat, checked_by,
    checked_at, report_kind, report_note, report_run_id, report_at,
    deletion_request_id, deletion_request_reason, deletion_requested_at,
    deletion_request_state, deleted_by, deleted_at, created_at
)
SELECT
    todo_id, user_id, project_id, client_todo_id, text, author, origin_kind,
    suggestion_id, previous_todo_id, starts_on, due_date, due_repeat, checked_by,
    checked_at, report_kind, report_note, report_run_id, report_at,
    deletion_request_id, deletion_request_reason, deletion_requested_at,
    deletion_request_state, deleted_by, deleted_at, created_at
FROM todo_items;

DROP TABLE todo_items;
ALTER TABLE todo_items_v0139 RENAME TO todo_items;

CREATE INDEX idx_todo_items_user_starts_on ON todo_items(user_id, starts_on);
CREATE INDEX idx_todo_items_user_deleted ON todo_items(user_id, deleted_at)
WHERE deleted_at IS NOT NULL;
CREATE INDEX idx_todo_items_project ON todo_items(project_id);

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

PRAGMA foreign_keys = ON;
