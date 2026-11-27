-- A hook that starts a new conversation on each fire starts it as the
-- conversation of a new TODO in its place; title is that TODO's text. Only
-- such a hook has one, and it always has one. A new migration rather than an
-- edit of 0153: a database that already applied 0153 never reads it again.
ALTER TABLE user_hooks ADD COLUMN title TEXT
    CHECK ((target_action_id IS NULL) = (title IS NOT NULL));
