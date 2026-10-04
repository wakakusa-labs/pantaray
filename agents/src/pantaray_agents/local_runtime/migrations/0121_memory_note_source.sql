PRAGMA foreign_keys = OFF;

CREATE TABLE memory_nodes_v0121 (
    user_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'source_records', 'activity_log', 'activity_summary', 'suggestion', 'action',
        'action_file_read', 'memory_note', 'agent_experience', 'short_term_insight',
        'long_term_insight', 'fact'
    )),
    source_record_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN ('preparing', 'active', 'tombstoned')
    ),
    integrity TEXT NOT NULL CHECK (integrity IN ('healthy', 'corrupt')),
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, node_id),
    UNIQUE (user_id, source_type, source_record_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, node_id, current_revision_id)
        REFERENCES memory_revisions(user_id, node_id, revision_id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (lifecycle = 'preparing' AND current_revision_id IS NULL)
        OR
        (lifecycle IN ('active', 'tombstoned') AND current_revision_id IS NOT NULL)
    ),
    CHECK (lifecycle != 'preparing' OR integrity = 'healthy')
);

INSERT INTO memory_nodes_v0121(
    user_id, node_id, source_type, source_record_id, lifecycle, integrity,
    current_revision_id, created_at, updated_at
)
SELECT
    user_id, node_id, source_type, source_record_id, lifecycle, integrity,
    current_revision_id, created_at, updated_at
FROM memory_nodes;

DROP TABLE memory_nodes;
ALTER TABLE memory_nodes_v0121 RENAME TO memory_nodes;

CREATE INDEX idx_memory_nodes_current_revision
    ON memory_nodes(user_id, current_revision_id);
CREATE INDEX idx_memory_nodes_lifecycle
    ON memory_nodes(user_id, lifecycle, integrity, updated_at);
CREATE INDEX idx_memory_nodes_record_exact
    ON memory_nodes(user_id, source_record_id);

PRAGMA foreign_keys = ON;
