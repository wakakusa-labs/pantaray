-- The files a Suggestion made, as an ordered JSON array of paths relative to
-- its artifact folder. Every Suggestion without files, including every row
-- stored before this column, holds the empty array.
ALTER TABLE agent_suggestions ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '[]'
CHECK (json_valid(artifacts_json) AND json_type(artifacts_json) = 'array');
