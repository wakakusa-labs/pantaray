PRAGMA foreign_keys = OFF;

-- 'fact' and 'long_term_insight' are those memory trees' current revision folders,
-- read-only like 'agent_experience'; source_id is the revision.
CREATE TABLE workspace_manifest_roots_v0129 (
    root_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (
        source_type IN (
            'folder', 'scratch', 'agent_experience', 'approved_folder', 'fact',
            'long_term_insight'
        )
    ),
    source_id TEXT,
    display_name TEXT NOT NULL,
    canonical_real_path TEXT NOT NULL,
    real_path TEXT NOT NULL,
    can_read INTEGER NOT NULL CHECK (can_read IN (0, 1)),
    can_apply_patch INTEGER NOT NULL CHECK (can_apply_patch IN (0, 1)),
    can_process_read INTEGER NOT NULL CHECK (can_process_read IN (0, 1)),
    can_process_write INTEGER NOT NULL CHECK (can_process_write IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (manifest_id)
        REFERENCES workspace_manifests(manifest_id) ON DELETE CASCADE,
    CHECK (
        (
            source_type IN (
                'folder', 'agent_experience', 'approved_folder', 'fact',
                'long_term_insight'
            )
            AND source_id IS NOT NULL
            AND trim(source_id) <> ''
        )
        OR (source_type = 'scratch' AND source_id = manifest_id)
    ),
    UNIQUE (manifest_id, canonical_real_path)
);

INSERT INTO workspace_manifest_roots_v0129(
    root_id, manifest_id, source_type, source_id, display_name,
    canonical_real_path, real_path, can_read, can_apply_patch,
    can_process_read, can_process_write, created_at
)
SELECT
    root_id, manifest_id, source_type, source_id, display_name,
    canonical_real_path, real_path, can_read, can_apply_patch,
    can_process_read, can_process_write, created_at
FROM workspace_manifest_roots;

DROP TABLE workspace_manifest_roots;
ALTER TABLE workspace_manifest_roots_v0129 RENAME TO workspace_manifest_roots;

CREATE INDEX idx_workspace_manifest_roots_source
    ON workspace_manifest_roots(source_type, source_id);

PRAGMA foreign_keys = ON;
