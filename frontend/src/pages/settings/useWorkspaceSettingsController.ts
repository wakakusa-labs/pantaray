import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import { useLocalOwner } from '@/context/localOwnerContext';

import type { Translate } from './types';
import { useWorkspaceProjectDragController } from './useWorkspaceProjectDragController';
import {
  getCachedWorkspaceSettings,
  setCachedWorkspaceSettings,
} from './components/workspaceSettingsCache';
import {
  applyWorkspaceMutation,
  countOrganizationUsage,
  workspaceFocusId,
} from './components/workspaceSettingsModel';
import type {
  WorkspaceFolderCreateInput,
  FocusKey,
  FocusRequest,
  WorkspaceMutationEvent,
  WorkspaceSettings,
} from './components/workspaceSettingsModel';

type WorkspaceSettingsAction =
  | { type: 'settingsLoaded'; settings: WorkspaceSettings }
  | WorkspaceMutationEvent;

export type ResolvedFocusRequest = {
  key: FocusKey;
};

// Project creation focuses a control that only exists once the response supplies its id, so the
// success key may be derived from the mutation result. A mutation submitted from a surface that
// stays open on failure omits the failure key so that focus stays on the control it came from.
type MutationFocusRequest<Result> = {
  onSuccess: FocusKey | ((result: Result) => FocusKey);
  onFailure?: FocusKey;
};

export type WorkspacePendingKey =
  | 'organization:create'
  | 'project:create'
  | 'projects:reorder'
  | `organization:delete:${string}`
  | `project:delete:${string}`
  | `project:links:${string}`
  | `folder:create:${string}`
  | `folder:delete:${string}`
  | `folder:links:${string}`;

export const workspacePendingKey = {
  organizationCreate: 'organization:create' as WorkspacePendingKey,
  organizationDelete: (organizationId: string): WorkspacePendingKey =>
    `organization:delete:${organizationId}`,
  projectCreate: 'project:create' as WorkspacePendingKey,
  projectDelete: (projectId: string): WorkspacePendingKey => `project:delete:${projectId}`,
  projectLinks: (projectId: string): WorkspacePendingKey => `project:links:${projectId}`,
  folderCreate: (projectId: string): WorkspacePendingKey => `folder:create:${projectId}`,
  folderDelete: (folderId: string): WorkspacePendingKey => `folder:delete:${folderId}`,
  folderLinks: (folderId: string): WorkspacePendingKey => `folder:links:${folderId}`,
  projectsReorder: 'projects:reorder' as WorkspacePendingKey,
};

/**
 * Owner scope is this hook's life: the owner boundary mounts it for one confirmed owner and
 * unmounts it when the owner changes, so nothing here re-checks the owner after an await.
 * Every workspace-settings write is addressed in main from main's own current owner
 * (electron/src/settings/workspaceSettingsFetch.ts:220-226), never from an id sent by the
 * renderer, and the module cache is written under the owner captured here.
 */
export function useWorkspaceSettingsController(t: Translate) {
  const { id: ownerId } = useLocalOwner();
  const settingsRevisionRef = useRef(0);
  const pendingGuardRef = useRef<Set<WorkspacePendingKey>>(new Set());
  // `null` is "nothing has been read yet", which is not the same as a workspace whose
  // lists happen to be empty: defaults nobody confirmed must never be shown, cached or
  // mutated as if they were this owner's settings.
  const [settings, dispatchSettings] = useReducer(
    workspaceSettingsReducer,
    getCachedWorkspaceSettings(ownerId)
  );
  const [projectGeneration, setProjectGeneration] = useState(0);
  const [isLoading, setIsLoading] = useState(settings === null);
  const [pending, setPending] = useState<ReadonlySet<WorkspacePendingKey>>(new Set());
  const [focusRequest, setFocusRequest] = useState<ResolvedFocusRequest | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const showLoading = isLoading && settings === null;

  useEffect(() => {
    let cancelled = false;
    const startRevision = settingsRevisionRef.current;
    (async () => {
      try {
        const next = await requireWorkspaceSettingsApi().get();
        // A mutation that completed while this read was in flight already holds the newer
        // truth, so the read it started before must not be applied on top of it.
        if (cancelled || settingsRevisionRef.current !== startRevision) return;
        dispatchSettings({ type: 'settingsLoaded', settings: next });
        setProjectGeneration((current) => current + 1);
        setErrorMessage(null);
      } catch {
        if (!cancelled && settingsRevisionRef.current === startRevision) {
          setErrorMessage(t('settings.workspace.loadFailed'));
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  useEffect(() => {
    if (settings) setCachedWorkspaceSettings(ownerId, settings);
  }, [settings, ownerId]);

  const commitMutation = async <Result>(
    pendingKey: WorkspacePendingKey,
    operation: () => Promise<Result>,
    toEvent: (result: Result) => WorkspaceMutationEvent,
    advancesProjectGeneration = false,
    focus?: MutationFocusRequest<Result>
  ): Promise<Result | null> => {
    if (!beginPending(pendingKey, pendingGuardRef.current, setPending)) return null;
    setErrorMessage(null);
    try {
      const result = await operation();
      settingsRevisionRef.current += 1;
      dispatchSettings(toEvent(result));
      if (advancesProjectGeneration) setProjectGeneration((current) => current + 1);
      if (focus) {
        const { onSuccess } = focus;
        setFocusRequest({ key: typeof onSuccess === 'function' ? onSuccess(result) : onSuccess });
      }
      return result;
    } catch {
      setErrorMessage(t('settings.workspace.saveFailed'));
      if (focus?.onFailure) setFocusRequest({ key: focus.onFailure });
      return null;
    } finally {
      endPending(pendingKey, pendingGuardRef.current, setPending);
    }
  };

  const addOrganization = async (displayName: string): Promise<string | null> => {
    const trimmedName = displayName.trim();
    if (!trimmedName) return null;
    const organization = await commitMutation(
      workspacePendingKey.organizationCreate,
      async () =>
        await requireWorkspaceSettingsApi().createOrganization({ displayName: trimmedName }),
      (created) => ({ type: 'organizationCreated', organization: created })
    );
    return organization?.organization_id ?? null;
  };

  const createProject = async (
    displayName: string,
    organizationIds: string[]
  ): Promise<boolean> => {
    const trimmedName = displayName.trim();
    if (!trimmedName) return false;
    const project = await commitMutation(
      workspacePendingKey.projectCreate,
      async () =>
        await requireWorkspaceSettingsApi().createProject({
          displayName: trimmedName,
          organizationIds,
        }),
      (created) => ({ type: 'projectCreated', project: created }),
      true,
      { onSuccess: (created) => workspaceFocusId.projectFolderAdd(created.project_id) }
    );
    return project !== null;
  };

  const createFolder = async (input: WorkspaceFolderCreateInput): Promise<boolean> => {
    const displayName = input.displayName.trim();
    const realPath = input.realPath.trim();
    if (!displayName || !realPath) return false;
    const folder = await commitMutation(
      workspacePendingKey.folderCreate(input.projectIds[0] ?? 'unassigned'),
      async () =>
        await requireWorkspaceSettingsApi().createFolder({
          displayName,
          realPath,
          organizationIds: input.organizationIds,
          projectIds: input.projectIds,
        }),
      (created) => ({ type: 'folderCreated', folder: created })
    );
    return folder !== null;
  };

  // Every surface deletes through here, so an organization still in use is never deleted
  // without the user agreeing to drop it from those projects and folders.
  const deleteOrganization = async (organizationId: string, focus: FocusRequest) => {
    const organization = settings?.organizations.find(
      (candidate) => candidate.organization_id === organizationId
    );
    if (!settings || !organization) return;
    const usage = countOrganizationUsage(organizationId, settings.projects, settings.folders);
    if (
      usage > 0 &&
      !window.confirm(
        t('settings.workspace.organizationDelete.confirm', {
          name: organization.display_name,
          count: usage,
        })
      )
    ) {
      return;
    }
    await commitMutation(
      workspacePendingKey.organizationDelete(organizationId),
      async () => await requireWorkspaceSettingsApi().deleteOrganization(organizationId),
      () => ({ type: 'organizationDeleted', organizationId }),
      false,
      focus
    );
  };

  const deleteProject = async (projectId: string, focus: FocusRequest) => {
    await commitMutation(
      workspacePendingKey.projectDelete(projectId),
      async () => await requireWorkspaceSettingsApi().deleteProject(projectId),
      () => ({ type: 'projectDeleted', projectId }),
      true,
      focus
    );
  };

  const updateProjectOrganizations = async (
    projectId: string,
    organizationIds: string[],
    focus: FocusRequest
  ): Promise<boolean> => {
    const project = await commitMutation(
      workspacePendingKey.projectLinks(projectId),
      async () =>
        await requireWorkspaceSettingsApi().updateProjectLinks(projectId, { organizationIds }),
      (updated) => ({ type: 'projectLinksUpdated', project: updated }),
      false,
      focus
    );
    return project !== null;
  };

  const deleteFolder = async (folderId: string, focus: FocusRequest) => {
    await commitMutation(
      workspacePendingKey.folderDelete(folderId),
      async () => await requireWorkspaceSettingsApi().deleteFolder(folderId),
      () => ({ type: 'folderDeleted', folderId }),
      false,
      focus
    );
  };

  const assignFolderToProject = async (
    folderId: string,
    projectId: string,
    focus: FocusRequest
  ): Promise<boolean> => {
    const folder = await commitMutation(
      workspacePendingKey.folderLinks(folderId),
      async () =>
        await requireWorkspaceSettingsApi().updateFolderLinks(folderId, {
          organizationIds: [],
          projectIds: [projectId],
        }),
      (updated) => ({ type: 'folderLinksUpdated', folder: updated }),
      false,
      focus
    );
    return folder !== null;
  };

  const selectFolder = async (): Promise<string | null> => {
    setErrorMessage(null);
    try {
      const result = await requireWorkspaceSettingsApi().selectFolder();
      return result.canceled ? null : result.path;
    } catch {
      setErrorMessage(t('settings.workspace.selectFolderFailed'));
      return null;
    }
  };

  const previewProjectOrder = (projects: WorkspaceSettings['projects']) => {
    dispatchSettings({
      type: 'projectsOrderPreview',
      projectIds: projects.map((project) => project.project_id),
    });
  };

  const persistProjectOrder = async (projectIds: string[]): Promise<void> => {
    const pendingKey = workspacePendingKey.projectsReorder;
    if (!beginPending(pendingKey, pendingGuardRef.current, setPending)) {
      throw new Error('Workspace project reorder is already in progress.');
    }
    setErrorMessage(null);
    try {
      const response = await requireWorkspaceSettingsApi().reorderProjects({ projectIds });
      if (
        response.project_ids.length !== projectIds.length ||
        response.project_ids.some((projectId, index) => projectId !== projectIds[index])
      ) {
        throw new Error('Workspace project order response does not match the request.');
      }
      settingsRevisionRef.current += 1;
      dispatchSettings({ type: 'projectsReordered', projectIds: response.project_ids });
      setProjectGeneration((current) => current + 1);
    } finally {
      endPending(pendingKey, pendingGuardRef.current, setPending);
    }
  };

  const isProjectStructurePending =
    pending.has(workspacePendingKey.projectCreate) ||
    pending.has(workspacePendingKey.projectsReorder) ||
    [...pending].some((key) => key.startsWith('project:delete:'));

  const dragController = useWorkspaceProjectDragController({
    disabled: isProjectStructurePending,
    generation: projectGeneration,
    projects: settings?.projects ?? [],
    t,
    onPreview: previewProjectOrder,
    onPersist: persistProjectOrder,
    onFailure: () => setErrorMessage(t('settings.workspace.saveFailed')),
  });

  const clearFocusRequest = useCallback((request: ResolvedFocusRequest) => {
    setFocusRequest((current) => (current === request ? null : current));
  }, []);

  return {
    addOrganization,
    assignFolderToProject,
    createFolder,
    createProject,
    deleteFolder,
    deleteOrganization,
    deleteProject,
    dragController,
    errorMessage,
    focusRequest,
    isProjectStructurePending,
    pending,
    clearFocusRequest,
    selectFolder,
    settings,
    showLoading,
    updateProjectOrganizations,
  };
}

function beginPending(
  key: WorkspacePendingKey,
  guard: Set<WorkspacePendingKey>,
  setPending: (pending: ReadonlySet<WorkspacePendingKey>) => void
): boolean {
  if (guard.has(key)) return false;
  guard.add(key);
  setPending(new Set(guard));
  return true;
}

function endPending(
  key: WorkspacePendingKey,
  guard: Set<WorkspacePendingKey>,
  setPending: (pending: ReadonlySet<WorkspacePendingKey>) => void
): void {
  guard.delete(key);
  setPending(new Set(guard));
}

function workspaceSettingsReducer(
  settings: WorkspaceSettings | null,
  action: WorkspaceSettingsAction
): WorkspaceSettings | null {
  if (action.type === 'settingsLoaded') return action.settings;
  // A mutation applied to settings that were never read would publish the rest of the
  // workspace as empty. Only a read makes the settings current.
  return settings === null ? null : applyWorkspaceMutation(settings, action);
}

function requireWorkspaceSettingsApi() {
  const api = window.electron?.workspaceSettings;
  if (!api) throw new Error('Workspace settings bridge is unavailable.');
  return api;
}
