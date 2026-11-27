-- The hooks an Action set up with the user's yes, one row each: the definition
-- the user approved and how its runs go. No file holds a hook any more, so the
-- tables and columns that bound a task file to its approved content go.
--
-- origin is the conversation that asked; target the one each fire continues,
-- or NULL to start a new conversation on every fire, placed in the
-- organization or project named (neither: no place). Deleting either
-- conversation deletes the hook, its state and its inbox. version grows when
-- the definition changes, so a fire computed from an older one never writes
-- its next time over the newer one's. The latest turn's result is NULL until
-- that turn ends; three failures in a row stop the hook with the reason of the
-- last one, until the owner switches it on again.
CREATE TABLE user_hooks (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    hook_id TEXT NOT NULL,
    origin_action_id TEXT NOT NULL
        REFERENCES agent_actions(action_id) ON DELETE CASCADE,
    target_action_id TEXT REFERENCES agent_actions(action_id) ON DELETE CASCADE,
    organization_id TEXT
        REFERENCES workspace_organizations(organization_id) ON DELETE SET NULL,
    project_id TEXT REFERENCES workspace_projects(project_id) ON DELETE SET NULL,
    trigger_json TEXT NOT NULL CHECK (json_valid(trigger_json)),
    until TEXT,
    message TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    next_fire_at TEXT,
    last_message_id TEXT,
    last_result TEXT CHECK (
        last_result IN ('completed', 'failed', 'canceled', 'not_started')
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    stopped_reason TEXT CHECK (stopped_reason IN ('start_failed', 'turn_failed')),
    PRIMARY KEY (user_id, hook_id),
    -- A project names its organization, as a TODO's does.
    CHECK (organization_id IS NULL OR project_id IS NULL),
    CHECK (target_action_id IS NULL OR (organization_id IS NULL AND project_id IS NULL))
);

CREATE INDEX user_hooks_by_origin ON user_hooks(origin_action_id);
CREATE INDEX user_hooks_by_target ON user_hooks(target_action_id);

-- The inbox is shared with the built-in hooks, so it cannot reference this
-- table; a deleted hook, cascaded or not, takes its events with it here.
CREATE TRIGGER user_hooks_delete_inbox AFTER DELETE ON user_hooks
BEGIN
    DELETE FROM hook_inbox WHERE user_id = OLD.user_id AND hook_id = OLD.hook_id;
END;

-- The tasks kept as files are not carried over.
DELETE FROM hook_inbox WHERE hook_id LIKE 'task.%';
DELETE FROM hook_states WHERE hook_id LIKE 'task.%';
ALTER TABLE hook_states DROP COLUMN content_hash;
DROP TABLE hook_task_origins;
DROP TABLE hook_conversations;
DROP TABLE hook_settings;
