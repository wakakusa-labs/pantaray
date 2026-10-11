-- What the user wrote when they dismissed a suggestion: their reason, recorded
-- with the dismissal and nowhere else. It starts no Action. A reason exists only
-- on a dismissed suggestion, and is never an empty string.
ALTER TABLE agent_suggestions ADD COLUMN dismiss_reason TEXT CHECK (
    dismiss_reason IS NULL
    OR (user_reaction = 'rejected' AND LENGTH(dismiss_reason) > 0)
);
