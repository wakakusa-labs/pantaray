import { LocalBackendRequestError, type createLocalBackendClient } from '../localBackend/client';

export type WorkspaceOrganization = {
  organization_id: string;
  display_name: string;
};

export type WorkspaceProject = {
  project_id: string;
  display_name: string;
  sort_order: number;
  organization_ids: string[];
};

export type WorkspaceFolder = {
  folder_id: string;
  display_name: string;
  real_path: string;
  canonical_real_path: string;
  organization_ids: string[];
  project_ids: string[];
};

/**
 * A name another project has is refused, not answered with that project. The refusal crosses
 * IPC as data because an Error thrown across `invoke` loses its status.
 */
export type WorkspaceProjectCreateResult = WorkspaceProject | { errorCode: 'PROJECT_NAME_TAKEN' };

const HTTP_CONFLICT = 409;

export type ReadAccessScope = 'workspace' | 'full_access';

export type WorkspaceSettings = {
  read_access_scope: ReadAccessScope;
  organizations: WorkspaceOrganization[];
  projects: WorkspaceProject[];
  folders: WorkspaceFolder[];
};

export type CommandNetworkSettings = {
  command_network_enabled: boolean;
};

const COMMAND_NETWORK_REQUEST_TIMEOUT_MS = 10_000;

function parseCommandNetworkSettings(payload: unknown): CommandNetworkSettings {
  if (
    !payload ||
    typeof payload !== 'object' ||
    !('command_network_enabled' in payload) ||
    typeof payload.command_network_enabled !== 'boolean'
  ) {
    throw new Error('Command network settings response is invalid.');
  }
  return { command_network_enabled: payload.command_network_enabled };
}

export type ReadAccessScopeSettings = {
  read_access_scope: ReadAccessScope;
};

export type WorkspaceProjectOrder = {
  project_ids: string[];
};

type WorkspaceCreateInput = {
  displayName?: unknown;
  realPath?: unknown;
  organizationIds?: unknown;
  projectIds?: unknown;
};

type WorkspaceLinksUpdateInput = {
  organizationIds?: unknown;
  projectIds?: unknown;
};

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isReadAccessScope(value: unknown): value is ReadAccessScope {
  return value === 'workspace' || value === 'full_access';
}

function normalizeCreateInput(input: unknown): Required<WorkspaceCreateInput> {
  const candidate = input && typeof input === 'object' ? (input as WorkspaceCreateInput) : {};
  const displayName = String(candidate.displayName || '').trim();
  const realPath = String(candidate.realPath || '').trim();
  const organizationIds = isStringArray(candidate.organizationIds) ? candidate.organizationIds : [];
  const projectIds = isStringArray(candidate.projectIds) ? candidate.projectIds : [];
  return { displayName, realPath, organizationIds, projectIds };
}

function normalizeLinksInput(input: unknown): Required<WorkspaceLinksUpdateInput> {
  const candidate = input && typeof input === 'object' ? (input as WorkspaceLinksUpdateInput) : {};
  const organizationIds = isStringArray(candidate.organizationIds) ? candidate.organizationIds : [];
  const projectIds = isStringArray(candidate.projectIds) ? candidate.projectIds : [];
  return { organizationIds, projectIds };
}

function isWorkspaceSettings(value: unknown): value is WorkspaceSettings {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<WorkspaceSettings>;
  return (
    Array.isArray(candidate.organizations) &&
    candidate.organizations.every(isWorkspaceOrganization) &&
    Array.isArray(candidate.projects) &&
    candidate.projects.every(isWorkspaceProject) &&
    Array.isArray(candidate.folders) &&
    candidate.folders.every(isWorkspaceFolder) &&
    isReadAccessScope(candidate.read_access_scope)
  );
}

function isWorkspaceOrganization(value: unknown): value is WorkspaceOrganization {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<WorkspaceOrganization>;
  return (
    typeof candidate.organization_id === 'string' && typeof candidate.display_name === 'string'
  );
}

function isWorkspaceProject(value: unknown): value is WorkspaceProject {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<WorkspaceProject>;
  return (
    typeof candidate.project_id === 'string' &&
    typeof candidate.display_name === 'string' &&
    typeof candidate.sort_order === 'number' &&
    Number.isInteger(candidate.sort_order) &&
    candidate.sort_order >= 0 &&
    isStringArray(candidate.organization_ids)
  );
}

function isWorkspaceFolder(value: unknown): value is WorkspaceFolder {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<WorkspaceFolder>;
  return (
    typeof candidate.folder_id === 'string' &&
    typeof candidate.display_name === 'string' &&
    typeof candidate.real_path === 'string' &&
    typeof candidate.canonical_real_path === 'string' &&
    isStringArray(candidate.organization_ids) &&
    isStringArray(candidate.project_ids)
  );
}

function parseWorkspaceSettings(payload: unknown): WorkspaceSettings {
  if (!isWorkspaceSettings(payload)) {
    throw new Error('Workspace settings response is invalid.');
  }
  return payload;
}

function isReadAccessScopeSettings(value: unknown): value is ReadAccessScopeSettings {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<ReadAccessScopeSettings>;
  return isReadAccessScope(candidate.read_access_scope);
}

function parseReadAccessScopeSettings(payload: unknown): ReadAccessScopeSettings {
  if (!isReadAccessScopeSettings(payload)) {
    throw new Error('Read access scope response is invalid.');
  }
  return payload;
}

function parseWorkspaceOrganization(payload: unknown): WorkspaceOrganization {
  if (!isWorkspaceOrganization(payload)) {
    throw new Error('Workspace organization response is invalid.');
  }
  return payload;
}

function parseWorkspaceProject(payload: unknown): WorkspaceProject {
  if (!isWorkspaceProject(payload)) {
    throw new Error('Workspace project response is invalid.');
  }
  return payload;
}

function parseWorkspaceFolder(payload: unknown): WorkspaceFolder {
  if (!isWorkspaceFolder(payload)) {
    throw new Error('Workspace folder response is invalid.');
  }
  return payload;
}

function parseWorkspaceProjectOrder(payload: unknown): WorkspaceProjectOrder {
  if (!payload || typeof payload !== 'object') {
    throw new Error('Workspace project order response is invalid.');
  }
  const candidate = payload as Partial<WorkspaceProjectOrder>;
  if (!isStringArray(candidate.project_ids)) {
    throw new Error('Workspace project order response is invalid.');
  }
  return { project_ids: candidate.project_ids };
}

function buildWorkspaceSettingsUrl(userId: string): string {
  return `/v1/agents/users/${encodeURIComponent(userId)}/workspace-settings`;
}

export function createWorkspaceSettingsFetcher(params: {
  requestJson: ReturnType<typeof createLocalBackendClient>['requestJson'];
  getUserId: () => string | null;
}): {
  get: () => Promise<WorkspaceSettings>;
  getReadAccessScope: () => Promise<ReadAccessScopeSettings>;
  getCommandNetwork: () => Promise<CommandNetworkSettings>;
  updateCommandNetwork: (enabled: boolean) => Promise<CommandNetworkSettings>;
  createOrganization: (input: unknown) => Promise<WorkspaceOrganization>;
  createProject: (input: unknown) => Promise<WorkspaceProjectCreateResult>;
  renameProject: (projectId: string, input: { displayName: string }) => Promise<WorkspaceProject>;
  createFolder: (input: unknown) => Promise<WorkspaceFolder>;
  reorderProjects: (input: { projectIds: string[] }) => Promise<WorkspaceProjectOrder>;
  deleteOrganization: (organizationId: string) => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  deleteFolder: (folderId: string) => Promise<void>;
  updateProjectLinks: (projectId: string, input: unknown) => Promise<WorkspaceProject>;
  updateFolderLinks: (folderId: string, input: unknown) => Promise<WorkspaceFolder>;
  updateReadAccessScope: (
    readAccessScope: unknown
  ) => Promise<{ read_access_scope: ReadAccessScope }>;
} {
  const buildUrl = (): string => {
    const userId = params.getUserId();
    if (!userId) {
      throw new Error('Missing authenticated user id.');
    }
    return buildWorkspaceSettingsUrl(userId);
  };

  return {
    get: async (): Promise<WorkspaceSettings> => {
      return parseWorkspaceSettings(
        await params.requestJson<unknown>({ path: buildUrl(), method: 'GET' })
      );
    },
    getReadAccessScope: async (): Promise<ReadAccessScopeSettings> => {
      return parseReadAccessScopeSettings(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/read-access-scope`,
          method: 'GET',
        })
      );
    },
    getCommandNetwork: async () =>
      parseCommandNetworkSettings(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/command-network`,
          method: 'GET',
          timeoutMs: COMMAND_NETWORK_REQUEST_TIMEOUT_MS,
        })
      ),
    updateCommandNetwork: async (enabled) =>
      parseCommandNetworkSettings(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/command-network`,
          method: 'PUT',
          body: { command_network_enabled: enabled },
          timeoutMs: COMMAND_NETWORK_REQUEST_TIMEOUT_MS,
        })
      ),
    createOrganization: async (input: unknown): Promise<WorkspaceOrganization> => {
      const normalized = normalizeCreateInput(input);
      return parseWorkspaceOrganization(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/organizations`,
          method: 'POST',
          body: { display_name: normalized.displayName },
        })
      );
    },
    createProject: async (input: unknown): Promise<WorkspaceProjectCreateResult> => {
      const normalized = normalizeCreateInput(input);
      try {
        return parseWorkspaceProject(
          await params.requestJson<unknown>({
            path: `${buildUrl()}/projects`,
            method: 'POST',
            body: {
              display_name: normalized.displayName,
              organization_ids: normalized.organizationIds,
            },
          })
        );
      } catch (error) {
        if (error instanceof LocalBackendRequestError && error.status === HTTP_CONFLICT)
          return { errorCode: 'PROJECT_NAME_TAKEN' };
        throw error;
      }
    },
    renameProject: async (projectId, input): Promise<WorkspaceProject> =>
      parseWorkspaceProject(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/projects/${encodeURIComponent(projectId)}/name`,
          method: 'PUT',
          body: { display_name: input.displayName },
        })
      ),
    createFolder: async (input: unknown): Promise<WorkspaceFolder> => {
      const normalized = normalizeCreateInput(input);
      return parseWorkspaceFolder(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/folders`,
          method: 'POST',
          body: {
            display_name: normalized.displayName,
            real_path: normalized.realPath,
            organization_ids: normalized.organizationIds,
            project_ids: normalized.projectIds,
          },
        })
      );
    },
    reorderProjects: async (input): Promise<WorkspaceProjectOrder> => {
      return parseWorkspaceProjectOrder(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/projects/order`,
          method: 'PUT',
          body: { project_ids: input.projectIds },
        })
      );
    },
    deleteOrganization: async (organizationId: string): Promise<void> => {
      await params.requestJson<unknown>({
        path: `${buildUrl()}/organizations/${encodeURIComponent(organizationId)}`,
        method: 'DELETE',
      });
    },
    deleteProject: async (projectId: string): Promise<void> => {
      await params.requestJson<unknown>({
        path: `${buildUrl()}/projects/${encodeURIComponent(projectId)}`,
        method: 'DELETE',
      });
    },
    deleteFolder: async (folderId: string): Promise<void> => {
      await params.requestJson<unknown>({
        path: `${buildUrl()}/folders/${encodeURIComponent(folderId)}`,
        method: 'DELETE',
      });
    },
    updateProjectLinks: async (projectId: string, input: unknown): Promise<WorkspaceProject> => {
      const normalized = normalizeLinksInput(input);
      return parseWorkspaceProject(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/projects/${encodeURIComponent(projectId)}/links`,
          method: 'PUT',
          body: { organization_ids: normalized.organizationIds },
        })
      );
    },
    updateFolderLinks: async (folderId: string, input: unknown): Promise<WorkspaceFolder> => {
      const normalized = normalizeLinksInput(input);
      return parseWorkspaceFolder(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/folders/${encodeURIComponent(folderId)}/links`,
          method: 'PUT',
          body: {
            organization_ids: normalized.organizationIds,
            project_ids: normalized.projectIds,
          },
        })
      );
    },
    updateReadAccessScope: async (
      readAccessScope: unknown
    ): Promise<{ read_access_scope: ReadAccessScope }> => {
      if (!isReadAccessScope(readAccessScope)) {
        throw new Error('Invalid read access scope.');
      }
      return parseReadAccessScopeSettings(
        await params.requestJson<unknown>({
          path: `${buildUrl()}/read-access-scope`,
          method: 'PUT',
          body: { read_access_scope: readAccessScope },
        })
      );
    },
  };
}
