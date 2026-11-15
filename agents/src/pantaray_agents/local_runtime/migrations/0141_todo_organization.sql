-- A TODO in no project may sit in an organization of its own. With a project,
-- its organization is the project's, so the column stays NULL: one source for
-- each TODO's organization. Deleting the organization moves its TODOs to no
-- organization, as deleting a project moves its TODOs to no project, and the
-- key is organization_id alone for the same reason as project_id's.
ALTER TABLE todo_items ADD COLUMN organization_id TEXT
    REFERENCES workspace_organizations(organization_id) ON DELETE SET NULL
    CHECK (organization_id IS NULL OR project_id IS NULL);

CREATE INDEX idx_todo_items_organization ON todo_items(organization_id);
