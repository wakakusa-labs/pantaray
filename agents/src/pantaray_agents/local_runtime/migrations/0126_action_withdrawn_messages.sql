-- A USER message the user withdrew before any run took it. Its step row is
-- deleted; this row keeps its message_id, so a retry of that same send is
-- answered as withdrawn instead of registering the message again.
CREATE TABLE action_withdrawn_messages (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL CHECK (length(message_id) > 0),
    action_id TEXT NOT NULL REFERENCES agent_actions(action_id) ON DELETE CASCADE,
    withdrawn_at TEXT NOT NULL,
    PRIMARY KEY (user_id, message_id)
);
