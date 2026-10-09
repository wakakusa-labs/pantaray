-- A thinking block is bound to everything the request sent before it: the
-- system instruction, the tools, and every earlier message. Handing one back
-- behind a prefix that has changed since -- an omitted result body, a dropped
-- retry notice, a replaced row, another system instruction -- is a 400.
--
-- So a stored turn now carries the fingerprint of the prefix it was produced
-- behind, and the projection hands it back only where today's prefix matches.
-- Rows written before this column have none, and are never handed back: such a
-- run continues without them, which the provider always accepts.
ALTER TABLE agent_action_steps ADD COLUMN provider_turn_fingerprint TEXT CHECK (
    provider_turn_fingerprint IS NULL
    OR (provider_turn IS NOT NULL AND LENGTH(provider_turn_fingerprint) > 0)
);
