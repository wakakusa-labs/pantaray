-- Indexes for the memory search lanes; the migration runner also indexes each
-- embedding generation's entry table by revision
-- (storage/migrations/memory_search_indexes.py).
-- The exact lane looks keywords up as fragment source paths, which no index
-- covered, so each search read every fragment of the user.
CREATE INDEX idx_memory_fragments_source_path
    ON memory_fragments(user_id, source_path);
