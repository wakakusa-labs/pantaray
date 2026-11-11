-- A TODO import's draft: organizations, projects and TODOs named, not yet
-- written. Names are kept as written; whether each one is new or existing is
-- decided against the workspace whenever the draft is read or registered.
-- `organization_id` names no row: an organization deleted while its import is
-- open makes that import's registration refused, not the draft deleted.
CREATE TABLE todo_import_drafts (
    import_id TEXT PRIMARY KEY CHECK (length(import_id) > 0),
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('all', 'organization', 'no_organization')),
    organization_id TEXT,
    draft_json TEXT NOT NULL,
    -- Each write replaces the whole draft and moves this by one.
    revision INTEGER NOT NULL CHECK (revision > 0),
    state TEXT NOT NULL CHECK (state IN ('open', 'registered', 'canceled')),
    -- What registration created, answered again to a repeated registration.
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((scope_kind = 'organization') = (organization_id IS NOT NULL)),
    CHECK ((state = 'registered') = (result_json IS NOT NULL))
);
