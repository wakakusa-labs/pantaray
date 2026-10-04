-- Whether a stored Suggestion reached the user. A new one is held until the
-- release task shows it or ends it unshown; rows without a Suggestion carry none.
ALTER TABLE agent_suggestions ADD COLUMN delivery_state TEXT CHECK (
    delivery_state IS NULL
    OR (
        has_suggestion IS 1
        AND delivery_state IN ('held', 'released', 'expired', 'superseded')
    )
);

-- Every Suggestion stored before holding existed was shown when it was stored.
UPDATE agent_suggestions SET delivery_state = 'released' WHERE has_suggestion = 1;

-- A newer Suggestion replaces the held one, so an owner holds at most one.
CREATE UNIQUE INDEX uq_agent_suggestions_one_held_per_user
ON agent_suggestions(user_id)
WHERE delivery_state = 'held';
