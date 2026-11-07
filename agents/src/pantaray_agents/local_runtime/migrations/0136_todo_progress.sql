-- Progress toward a project's goal, counted only from progress records. The
-- user writes a goal; a record says how much one TODO moved, with evidence.

-- One current goal per project. A deleted project's goal goes with it: its
-- TODOs move to "no project", so nothing could be counted toward it, and no
-- screen could show or clear it.
CREATE TABLE todo_goals (
    project_id TEXT PRIMARY KEY REFERENCES workspace_projects(project_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    text TEXT NOT NULL CHECK (length(text) > 0),
    target_quantity REAL NOT NULL CHECK (target_quantity > 0),
    unit TEXT NOT NULL CHECK (length(unit) > 0),
    deadline TEXT NOT NULL,
    -- When the goal was first set; records count from here. Editing the goal
    -- keeps it, clearing and setting again starts over.
    set_at TEXT NOT NULL
);

-- A record goes with its TODO when the TODO is purged. Records never change:
-- the user deletes a wrong one. The AI's records name the successful tool
-- steps of its own run that show the figure; the user's need none.
CREATE TABLE todo_progress_records (
    record_id TEXT PRIMARY KEY CHECK (length(record_id) > 0),
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    todo_id TEXT NOT NULL REFERENCES todo_items(todo_id) ON DELETE CASCADE,
    quantity REAL NOT NULL CHECK (quantity > 0),
    unit TEXT NOT NULL CHECK (length(unit) > 0),
    note TEXT NOT NULL CHECK (length(note) > 0),
    author TEXT NOT NULL CHECK (author IN ('user', 'ai')),
    -- Provenance only: the steps go only with the TODO's conversation, which
    -- goes only with the TODO, and the record goes with the TODO first.
    evidence_action_id TEXT,
    evidence_step_ids TEXT CHECK (
        evidence_step_ids IS NULL
        OR (json_valid(evidence_step_ids) AND json_type(evidence_step_ids) = 'array'
            AND json_array_length(evidence_step_ids) > 0)
    ),
    recorded_at TEXT NOT NULL,
    CHECK (
        (author = 'user' AND evidence_action_id IS NULL AND evidence_step_ids IS NULL)
        OR (author = 'ai' AND evidence_action_id IS NOT NULL
            AND evidence_step_ids IS NOT NULL)
    )
);

CREATE INDEX idx_todo_progress_records_todo ON todo_progress_records(todo_id);
CREATE INDEX idx_todo_goals_user ON todo_goals(user_id);

-- The progress view reads goals and records, so their changes bump the same
-- counter the TODO list follows; a project deletion removing its goal counts
-- too. The guards keep a user's deletion from writing a counter for them.
CREATE TRIGGER todo_goals_count_insert
AFTER INSERT ON todo_goals
BEGIN
    INSERT INTO todo_revisions(user_id, revision) VALUES (NEW.user_id, 1)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER todo_goals_count_update
AFTER UPDATE ON todo_goals
BEGIN
    INSERT INTO todo_revisions(user_id, revision)
    SELECT NEW.user_id, 1 WHERE EXISTS (SELECT 1 FROM users WHERE user_id = NEW.user_id)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER todo_goals_count_delete
AFTER DELETE ON todo_goals
BEGIN
    INSERT INTO todo_revisions(user_id, revision)
    SELECT OLD.user_id, 1 WHERE EXISTS (SELECT 1 FROM users WHERE user_id = OLD.user_id)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER todo_progress_records_count_insert
AFTER INSERT ON todo_progress_records
BEGIN
    INSERT INTO todo_revisions(user_id, revision) VALUES (NEW.user_id, 1)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER todo_progress_records_count_delete
AFTER DELETE ON todo_progress_records
BEGIN
    INSERT INTO todo_revisions(user_id, revision)
    SELECT OLD.user_id, 1 WHERE EXISTS (SELECT 1 FROM users WHERE user_id = OLD.user_id)
    ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;
