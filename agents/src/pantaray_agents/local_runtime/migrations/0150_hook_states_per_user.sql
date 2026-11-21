-- A time hook's state belongs to the owner the engine fires it for: keyed by
-- the hook alone, an occurrence one owner spent was spent for every owner, so
-- after an account switch the new owner waited for the next one.
CREATE TABLE hook_states_per_user (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    hook_id TEXT NOT NULL,
    next_fire_at TEXT,
    PRIMARY KEY (user_id, hook_id)
);

-- Every existing owner continues from the shared state, so the upgrade neither
-- refires nor skips an occurrence for anyone.
INSERT INTO hook_states_per_user(user_id, hook_id, next_fire_at)
SELECT users.user_id, states.hook_id, states.next_fire_at
FROM users CROSS JOIN hook_states AS states;

DROP TABLE hook_states;
ALTER TABLE hook_states_per_user RENAME TO hook_states;
