-- The tasks an Action wrote with its task tools, and the conversation each one
-- continues. A task file without a row is the user's own. Deleting the
-- conversation removes its tasks first; the reference has no ON DELETE action,
-- so a deletion that forgot them fails rather than leave a task whose
-- conversation is gone.
CREATE TABLE hook_task_origins (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    hook_id TEXT NOT NULL,
    action_id TEXT NOT NULL REFERENCES agent_actions(action_id),
    PRIMARY KEY (user_id, hook_id)
);

CREATE INDEX hook_task_origins_by_action ON hook_task_origins(user_id, action_id);
