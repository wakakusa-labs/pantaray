export type WorkspaceSettings = Awaited<
  ReturnType<NonNullable<NonNullable<Window['electron']>['workspaceSettings']>['get']>
>;

export type WorkspaceOrganization = WorkspaceSettings['organizations'][number];
export type WorkspaceProject = WorkspaceSettings['projects'][number];
export type WorkspaceFolder = WorkspaceSettings['folders'][number];

export type WorkspaceFolderCreateInput = {
  displayName: string;
  realPath: string;
  organizationIds: string[];
  projectIds: string[];
};

export type WorkspaceMutationEvent =
  | { type: 'organizationCreated'; organization: WorkspaceOrganization }
  | { type: 'organizationDeleted'; organizationId: string }
  | { type: 'projectCreated'; project: WorkspaceProject }
  | { type: 'projectDeleted'; projectId: string }
  | { type: 'folderCreated'; folder: WorkspaceFolder }
  | { type: 'folderDeleted'; folderId: string }
  | { type: 'projectLinksUpdated'; project: WorkspaceProject }
  | { type: 'folderLinksUpdated'; folder: WorkspaceFolder }
  | { type: 'projectsReordered'; projectIds: string[] }
  | { type: 'projectsOrderPreview'; projectIds: string[] };

export type FocusKey = string;

export type FocusRequest = {
  onSuccess: FocusKey;
  onFailure: FocusKey;
};

export const workspaceFocusId = {
  organizationDelete: (organizationId: string): FocusKey =>
    `ws-organization-delete-${organizationId}`,
  organizationInput: 'ws-organization-input' as FocusKey,
  organizationManagerOpen: 'ws-organization-manager-open' as FocusKey,
  organizationCreateOption: 'ws-organization-create-option' as FocusKey,
  organizationOptionDelete: (organizationId: string): FocusKey =>
    `ws-organization-option-delete-${organizationId}`,
  projectAdd: 'ws-project-add' as FocusKey,
  projectDelete: (projectId: string): FocusKey => `ws-project-delete-${projectId}`,
  projectHeading: (projectId: string): FocusKey => `ws-project-heading-${projectId}`,
  projectFolderAdd: (projectId: string): FocusKey => `ws-project-folder-add-${projectId}`,
  projectOrganizationAdd: (projectId: string): FocusKey =>
    `ws-project-organization-add-${projectId}`,
  projectOrganizationRemove: (projectId: string, organizationId: string): FocusKey =>
    `ws-project-organization-remove-${projectId}-${organizationId}`,
  projectFolderDelete: (projectId: string, folderId: string): FocusKey =>
    `ws-project-folder-delete-${projectId}-${folderId}`,
  unassignedFolderDelete: (folderId: string): FocusKey => `ws-unassigned-folder-delete-${folderId}`,
  unassignedAssign: (folderId: string): FocusKey => `ws-unassigned-assign-${folderId}`,
};

// This reducer mirrors the canonical response rules in workspace_settings.py,
// workspace_project_order.py, migrations 0036 (link FK cascades), and 0068 (project order).
// Promote mutations to a versioned server snapshot when responses stop being canonical or an
// external writer is introduced; until then, a second GET would create a competing data path.
export function applyWorkspaceMutation(
  settings: WorkspaceSettings,
  event: WorkspaceMutationEvent
): WorkspaceSettings {
  switch (event.type) {
    case 'organizationCreated':
      return {
        ...settings,
        organizations: upsertSorted(
          settings.organizations,
          event.organization,
          (organization) => organization.organization_id,
          compareOrganizations
        ),
      };
    case 'organizationDeleted':
      return {
        ...settings,
        organizations: settings.organizations.filter(
          (organization) => organization.organization_id !== event.organizationId
        ),
        projects: settings.projects.map((project) => ({
          ...project,
          organization_ids: project.organization_ids.filter(
            (organizationId) => organizationId !== event.organizationId
          ),
        })),
        folders: settings.folders.map((folder) => ({
          ...folder,
          organization_ids: folder.organization_ids.filter(
            (organizationId) => organizationId !== event.organizationId
          ),
        })),
      };
    case 'projectCreated':
    case 'projectLinksUpdated':
      return {
        ...settings,
        projects: upsertSorted(
          settings.projects,
          event.project,
          (project) => project.project_id,
          compareProjects
        ),
      };
    case 'projectDeleted':
      // The backend unregisters the folders only this project held (workspace_settings_deletion).
      return {
        ...settings,
        projects: settings.projects.filter((project) => project.project_id !== event.projectId),
        folders: settings.folders
          .filter(
            (folder) =>
              !folder.project_ids.includes(event.projectId) ||
              folder.project_ids.some((projectId) => projectId !== event.projectId)
          )
          .map((folder) => ({
            ...folder,
            project_ids: folder.project_ids.filter((projectId) => projectId !== event.projectId),
          })),
      };
    case 'folderCreated':
    case 'folderLinksUpdated':
      return {
        ...settings,
        folders: upsertSorted(
          settings.folders,
          event.folder,
          (folder) => folder.folder_id,
          compareFolders
        ),
      };
    case 'folderDeleted':
      return {
        ...settings,
        folders: settings.folders.filter((folder) => folder.folder_id !== event.folderId),
      };
    case 'projectsReordered':
      return {
        ...settings,
        projects: orderProjectsByIds(settings.projects, event.projectIds),
      };
    case 'projectsOrderPreview':
      return {
        ...settings,
        projects: orderProjectsByIds(settings.projects, event.projectIds),
      };
  }
}

export function successorFocusKey(
  ids: string[],
  removedId: string,
  toKey: (id: string) => FocusKey,
  anchorKey: FocusKey
): FocusKey {
  const removedIndex = ids.indexOf(removedId);
  if (removedIndex < 0) return anchorKey;
  const remainingIds = ids.filter((id) => id !== removedId);
  const successorId = remainingIds[Math.min(removedIndex, remainingIds.length - 1)];
  return successorId === undefined ? anchorKey : toKey(successorId);
}

export function upsertSorted<T>(
  items: T[],
  item: T,
  idOf: (value: T) => string,
  compare: (left: T, right: T) => number
): T[] {
  const itemId = idOf(item);
  return [...items.filter((candidate) => idOf(candidate) !== itemId), item].sort(compare);
}

export function resolveProjectOrganizations(
  project: WorkspaceProject,
  organizations: WorkspaceOrganization[]
): WorkspaceOrganization[] {
  const organizationsById = new Map(
    organizations.map((organization) => [organization.organization_id, organization])
  );
  return project.organization_ids
    .map((organizationId) => organizationsById.get(organizationId))
    .filter((organization): organization is WorkspaceOrganization => organization !== undefined)
    .sort(compareOrganizations);
}

export function orderProjectsByIds(
  projects: WorkspaceProject[],
  projectIds: string[]
): WorkspaceProject[] {
  const projectsById = new Map(projects.map((project) => [project.project_id, project]));
  if (projectIds.length !== projects.length || projectIds.some((id) => !projectsById.has(id))) {
    throw new Error('Project order does not match the current project set.');
  }
  return projectIds.map((projectId, sortOrder) => ({
    ...projectsById.get(projectId)!,
    sort_order: sortOrder,
  }));
}

export function getUnassignedFolders(
  folders: WorkspaceFolder[],
  projects: WorkspaceProject[]
): WorkspaceFolder[] {
  const activeProjectIds = new Set(projects.map((project) => project.project_id));
  return folders.filter(
    (folder) => !folder.project_ids.some((projectId) => activeProjectIds.has(projectId))
  );
}

export function resolveFolderOrganizations(
  folder: WorkspaceFolder,
  organizations: WorkspaceOrganization[]
): WorkspaceOrganization[] {
  const organizationIds = new Set(folder.organization_ids);
  return organizations
    .filter((organization) => organizationIds.has(organization.organization_id))
    .sort(compareOrganizations);
}

export function countOrganizationUsage(
  organizationId: string,
  projects: WorkspaceProject[],
  folders: WorkspaceFolder[]
): number {
  const projectCount = projects.filter((project) =>
    project.organization_ids.includes(organizationId)
  ).length;
  const activeProjectIds = new Set(projects.map((project) => project.project_id));
  const directFolderCount = folders.filter(
    (folder) =>
      !folder.project_ids.some((projectId) => activeProjectIds.has(projectId)) &&
      folder.organization_ids.includes(organizationId)
  ).length;
  return projectCount + directFolderCount;
}

export function displayNameFromPath(realPath: string): string {
  const normalized = realPath.replace(/[\\/]+$/u, '');
  const parts = normalized.split(/[\\/]/u);
  return parts[parts.length - 1] || normalized;
}

export function splitPathForMiddleEllipsis(realPath: string): [string, string] {
  const midpoint = Math.ceil(realPath.length / 2);
  return [realPath.slice(0, midpoint), realPath.slice(midpoint)];
}

export function compareOrganizations(
  left: WorkspaceOrganization,
  right: WorkspaceOrganization
): number {
  return (
    compareCodeUnits(left.display_name, right.display_name) ||
    compareCodeUnits(left.organization_id, right.organization_id)
  );
}

export function compareProjects(left: WorkspaceProject, right: WorkspaceProject): number {
  return (
    left.sort_order - right.sort_order ||
    compareCodeUnits(left.display_name, right.display_name) ||
    compareCodeUnits(left.project_id, right.project_id)
  );
}

export function compareFolders(left: WorkspaceFolder, right: WorkspaceFolder): number {
  return (
    compareCodeUnits(left.display_name, right.display_name) ||
    compareCodeUnits(left.folder_id, right.folder_id)
  );
}

// JavaScript code-unit order matches the SQLite BINARY collation used by workspace queries.
function compareCodeUnits(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}
