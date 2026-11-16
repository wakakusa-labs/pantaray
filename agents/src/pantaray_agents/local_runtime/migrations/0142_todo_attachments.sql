-- The files and images the user attached when writing a TODO, which the pickup
-- hands to the first message of the Action that works on it. Only a TODO the
-- user wrote holds any; NULL is none.
ALTER TABLE todo_items ADD COLUMN attachments_json TEXT
    CHECK (attachments_json IS NULL OR origin_kind = 'written');
