-- The Memory update now writes what delivered suggestions and Actions changed
-- into memory itself (an endeavor's "What Pantaray's help changed", and Agent
-- Experience), so nothing reads or writes the verdicts 0133 stored.
DROP TRIGGER reject_suggestion_outcome_verdict_update;
DROP TABLE suggestion_outcome_verdicts;
