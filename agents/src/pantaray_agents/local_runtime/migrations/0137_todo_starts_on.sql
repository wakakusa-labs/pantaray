-- A TODO starts on a calendar date instead of belonging to a week: the AI picks
-- it up from that date, and the user may move it. A row written for a week
-- starts on that week's Monday, which is the value the column already holds.
ALTER TABLE todo_items RENAME COLUMN week_start TO starts_on;

DROP INDEX idx_todo_items_user_week;
CREATE INDEX idx_todo_items_user_starts_on ON todo_items(user_id, starts_on);
