-- What a delivered suggestion changed, judged afterward from the user's and the
-- environment's records at a checkpoint (days after delivery). Verdicts are
-- append-only: one per suggestion and checkpoint, never rewritten, and removed
-- only with the suggestion they judge.
CREATE TABLE suggestion_outcome_verdicts (
    verdict_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    checkpoint_days INTEGER NOT NULL CHECK (checkpoint_days > 0),
    verdict TEXT NOT NULL CHECK (
        verdict IN ('improved', 'no_effect', 'worse', 'unknown')
    ),
    evidence_json TEXT NOT NULL CHECK (
        json_valid(evidence_json) AND json_type(evidence_json) = 'array'
    ),
    reason TEXT NOT NULL CHECK (LENGTH(reason) > 0),
    judged_at TEXT NOT NULL,
    -- The Memory update run whose conversation judged it.
    job_id TEXT NOT NULL,
    prompt_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    UNIQUE (user_id, suggestion_id, checkpoint_days),
    FOREIGN KEY (user_id, suggestion_id)
        REFERENCES agent_suggestions(user_id, suggestion_id) ON DELETE CASCADE
);

CREATE TRIGGER reject_suggestion_outcome_verdict_update
BEFORE UPDATE ON suggestion_outcome_verdicts
BEGIN
    SELECT RAISE(ABORT, 'suggestion outcome verdicts are append-only');
END;
