-- How far the unified Memory runs have taken each user's dismissed suggestions:
-- the dismissal time and suggestion id of the last dismissal a run was handed.
-- A run's dismissals are pinned in its job payload, and the cursor moves when
-- that job is enqueued. Dismissals recorded before this migration are not
-- handed, so each user's cursor starts at their latest one.
CREATE TABLE memory_dismissal_cursors (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    through_rejected_at TEXT NOT NULL,
    through_suggestion_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO memory_dismissal_cursors(
    user_id, through_rejected_at, through_suggestion_id, updated_at
)
SELECT user_id, rejected_at, suggestion_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM (
    SELECT user_id, rejected_at, suggestion_id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id ORDER BY rejected_at DESC, suggestion_id DESC
           ) AS recency
    FROM agent_suggestions
    WHERE user_reaction = 'rejected' AND rejected_at IS NOT NULL
      AND user_id IN (SELECT user_id FROM users)
)
WHERE recency = 1;

CREATE INDEX idx_agent_suggestions_user_dismissals
    ON agent_suggestions(user_id, rejected_at, suggestion_id)
    WHERE user_reaction = 'rejected';
