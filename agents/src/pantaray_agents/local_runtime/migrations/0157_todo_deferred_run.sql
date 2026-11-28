-- The run of a TODO's conversation the user put the work back from by moving
-- its start past today. While that run is still the conversation's latest and
-- has ended, the TODO waits for its start, and on that day the pickup sends its
-- conversation the next turn. NULL: the start was never moved so.
ALTER TABLE todo_items ADD COLUMN deferred_run_id TEXT;
