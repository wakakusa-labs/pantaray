import { useEffect, useReducer, useRef, useState } from 'react';

import { useLocalOwner } from '@/context/localOwnerContext';

import type { Translate } from './types';
import { useWorkspaceProjectDragController } from './useWorkspaceProjectDragController';
import {
  getCachedWorkspaceSettings,
  setCachedWorkspaceSettings,
} from './components/workspaceSettingsCache';
import { applyWorkspaceMutation, displayNameFromPath } from './components/workspaceSettingsModel';
import type {
  WorkspaceMutationEvent,
  WorkspaceSettings,
} from './components/workspaceSettingsModel';

type WorkspaceSettingsAction =
  | { type: 'settingsLoaded'; settings: WorkspaceSettings }
  | WorkspaceMutationEvent;

export type WorkspacePendingKey =
  | 'organization:create'
  | 'project:create'
  | `project:delete:${string}`
  | `project:links:${string}`
  | `project:rename:${string}`
  | `folder:create:${string}`
  | `folder:${string}`;

export const workspacePendingKey = {
  organizationCreate: 'organization:create' as WorkspacePendingKey,
  projectCreate: 'project:create' as WorkspacePendingKey,
  projectDelete: (projectId: string): WorkspacePendingKey => `project:delete:${projectId}`,
  projectLinks: (projectId: string): WorkspacePendingKey => `project:links:${projectId}`,
  projectRename: (projectId: string): WorkspacePendingKey => `project:rename:${projectId}`,
  folderCreate: (projectId: string): WorkspacePendingKey => `folder:create:${projectId}`,
  // One key for every change to a folder; it marks that folder's controls as busy.
  folder: (folderId: string): WorkspacePendingKey => `folder:${folderId}`,
};

/** Names taken between reading the settings and creating the project are rare; a few suffice. */
const MAX_PROJECT_NAME_ATTEMPTS = 5;

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
  // Workspace edits are rare, so they run one at a time: a change asked for while another is
  // in flight, including one whose folder dialog is still open, is refused and does nothing.
  const lockRef = useRef(false);
  const [busy, setBusy] = useState(false);
  // `null` is "nothing has been read yet", which is not the same as a workspace whose
  // lists happen to be empty: defaults nobody confirmed must never be shown, cached or
  // mutated as if they were this owner's settings.
  const [settings, dispatchSettings] = useReducer(
    workspaceSettingsReducer,
    getCachedWorkspaceSettings(ownerId)
  );
  // A folder dialog can answer after the first read has landed; what it acts on is read then.
  const latestSettingsRef = useRef(settings);
  useEffect(() => {
    latestSettingsRef.current = settings;
  });
  const [projectGeneration, setProjectGeneration] = useState(0);
  const [pending, setPending] = useState<ReadonlySet<WorkspacePendingKey>>(new Set());
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

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
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  useEffect(() => {
    if (settings) setCachedWorkspaceSettings(ownerId, settings);
  }, [settings, ownerId]);

  const locked =
    <Args extends unknown[], Result>(refused: Result, work: (...args: Args) => Promise<Result>) =>
    async (...args: Args): Promise<Result> => {
      if (lockRef.current) return refused;
      lockRef.current = true;
      setBusy(true);
      try {
        return await work(...args);
      } finally {
        lockRef.current = false;
        setBusy(false);
      }
    };

  // Runs one request inside a change that holds the lock; its pending key only marks the
  // control it belongs to as busy.
  const commitMutation = async <Result>(
    pendingKey: WorkspacePendingKey,
    operation: () => Promise<Result>,
    // Null for an answer that changed nothing, such as a refused project name.
    toEvent: (result: Result) => WorkspaceMutationEvent | null,
    advancesProjectGeneration = false
  ): Promise<Result | null> => {
    setPending((current) => new Set(current).add(pendingKey));
    setErrorMessage(null);
    try {
      const result = await operation();
      const event = toEvent(result);
      if (event === null) return result;
      settingsRevisionRef.current += 1;
      dispatchSettings(event);
      if (advancesProjectGeneration) setProjectGeneration((current) => current + 1);
      return result;
    } catch {
      setErrorMessage(t('settings.workspace.saveFailed'));
      return null;
    } finally {
      setPending((current) => new Set([...current].filter((key) => key !== pendingKey)));
    }
  };

  const addOrganization = locked(null, async (displayName: string): Promise<string | null> => {
    const trimmedName = displayName.trim();
    if (!trimmedName) return null;
    const organization = await commitMutation(
      workspacePendingKey.organizationCreate,
      async () =>
        await requireWorkspaceSettingsApi().createOrganization({ displayName: trimmedName }),
      (created) => ({ type: 'organizationCreated', organization: created })
    );
    return organization?.organization_id ?? null;
  });

  // The picker answers with the canonical path (featureRuntime.ts), the form the backend keys
  // folders by. Creating a registered folder again would replace all of its links.
  const findRegisteredFolder = (realPath: string) =>
    latestSettingsRef.current?.folders.find((folder) => folder.canonical_real_path === realPath);

  const updateProjectOrganizations = locked(
    false,
    async (projectId: string, organizationIds: string[]): Promise<boolean> => {
      const project = await commitMutation(
        workspacePendingKey.projectLinks(projectId),
        async () =>
          await requireWorkspaceSettingsApi().updateProjectLinks(projectId, { organizationIds }),
        (updated) => ({ type: 'projectLinksUpdated', project: updated })
      );
      return project !== null;
    }
  );

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

  /** The sidebar's ＋: a chosen folder becomes a project of its own, named after the folder. */
  const addProjectFromFolder = locked(undefined, async (): Promise<void> => {
    // Without the current folders a registered one could not be told apart.
    if (!settings) return;
    const realPath = await selectFolder();
    if (!realPath) return;
    if (findRegisteredFolder(realPath)) {
      setErrorMessage(t('settings.workspace.folderAlreadyRegistered'));
      return;
    }
    const displayName = displayNameFromPath(realPath);
    const project = await createNumberedProject(displayName);
    if (!project) return;
    const folder = await commitMutation(
      workspacePendingKey.folderCreate(project.project_id),
      async () =>
        await requireWorkspaceSettingsApi().createFolder({
          displayName,
          realPath,
          organizationIds: [],
          projectIds: [project.project_id],
        }),
      (created) => ({ type: 'folderCreated', folder: created })
    );
    if (folder) return;
    // A project without its folder would be listed with nothing behind it.
    await commitMutation(
      workspacePendingKey.projectDelete(project.project_id),
      async () => await requireWorkspaceSettingsApi().deleteProject(project.project_id),
      () => ({ type: 'projectDeleted', projectId: project.project_id }),
      true
    );
    setErrorMessage(t('settings.workspace.saveFailed'));
  });

  /**
   * Creates a project under the folder's name, numbered past names that are taken (" 2", " 3",
   * …). The backend refuses a taken name rather than answering with that project, so what this
   * returns was created here and is safe to take back.
   */
  const createNumberedProject = async (displayName: string) => {
    const knownNames = new Set(settings?.projects.map((project) => project.display_name));
    let copy = 1;
    for (let attempt = 0; attempt < MAX_PROJECT_NAME_ATTEMPTS; attempt += 1) {
      let projectName = displayName;
      while (knownNames.has(projectName)) projectName = `${displayName} ${++copy}`;
      knownNames.add(projectName);
      const created = await commitMutation(
        workspacePendingKey.projectCreate,
        async () =>
          await requireWorkspaceSettingsApi().createProject({
            displayName: projectName,
            organizationIds: [],
          }),
        (result) => ('errorCode' in result ? null : { type: 'projectCreated', project: result }),
        true
      );
      if (created === null) return null;
      if (!('errorCode' in created)) return created;
    }
    setErrorMessage(t('settings.workspace.saveFailed'));
    return null;
  };

  /** The sidebar's remove; the backend unregisters the folders only this project holds. */
  const removeProject = locked(false, async (projectId: string): Promise<boolean> => {
    const removed = await commitMutation(
      workspacePendingKey.projectDelete(projectId),
      async () => await requireWorkspaceSettingsApi().deleteProject(projectId),
      () => ({ type: 'projectDeleted', projectId }),
      true
    );
    return removed !== null;
  });

  /** A name another project has is refused here: the backend keeps project names unique. */
  const renameProject = locked(false, async (projectId: string, displayName: string) => {
    const name = displayName.trim();
    const project = settings?.projects.find((candidate) => candidate.project_id === projectId);
    if (!settings || !project || !name) return false;
    if (name === project.display_name) return true;
    if (settings.projects.some((candidate) => candidate.display_name === name)) {
      setErrorMessage(t('history.projects.nameTaken', { name }));
      return false;
    }
    const renamed = await commitMutation(
      workspacePendingKey.projectRename(projectId),
      async () =>
        await requireWorkspaceSettingsApi().renameProject(projectId, { displayName: name }),
      (updated) => ({ type: 'projectRenamed', project: updated })
    );
    return renamed !== null;
  });

  // Holds the lock from before the dialog opens, so nothing changes the folder meanwhile.
  const addFolderToProject = locked(false, async (projectId: string) => {
    const realPath = await selectFolder();
    if (!realPath) return false;
    const registered = findRegisteredFolder(realPath);
    if (registered) {
      // A folder already in the workspace joins this project as well, keeping its other links.
      if (registered.project_ids.includes(projectId)) return true;
      const linked = await commitMutation(
        workspacePendingKey.folder(registered.folder_id),
        async () =>
          await requireWorkspaceSettingsApi().updateFolderLinks(registered.folder_id, {
            organizationIds: registered.organization_ids,
            projectIds: [...registered.project_ids, projectId],
          }),
        (updated) => ({ type: 'folderLinksUpdated', folder: updated })
      );
      return linked !== null;
    }
    const folder = await commitMutation(
      workspacePendingKey.folderCreate(projectId),
      async () =>
        await requireWorkspaceSettingsApi().createFolder({
          displayName: displayNameFromPath(realPath),
          realPath,
          organizationIds: [],
          projectIds: [projectId],
        }),
      (created) => ({ type: 'folderCreated', folder: created })
    );
    return folder !== null;
  });

  /**
   * Takes a folder out of one project. A folder no other project holds is unregistered with
   * it: a registered folder stays inside the agent's boundary, so it must stay in a project.
   */
  const removeFolderFromProject = locked(false, async (folderId: string, projectId: string) => {
    const folder = settings?.folders.find((candidate) => candidate.folder_id === folderId);
    if (!folder) return false;
    const otherProjectIds = folder.project_ids.filter((candidate) => candidate !== projectId);
    const removed =
      otherProjectIds.length === 0
        ? await commitMutation(
            workspacePendingKey.folder(folderId),
            async () => await requireWorkspaceSettingsApi().deleteFolder(folderId),
            () => ({ type: 'folderDeleted', folderId })
          )
        : await commitMutation(
            workspacePendingKey.folder(folderId),
            async () =>
              await requireWorkspaceSettingsApi().updateFolderLinks(folderId, {
                organizationIds: folder.organization_ids,
                projectIds: otherProjectIds,
              }),
            (updated) => ({ type: 'folderLinksUpdated', folder: updated })
          );
    return removed !== null;
  });

  const openFolder = async (folderId: string): Promise<void> => {
    setErrorMessage(null);
    try {
      await requireWorkspaceSettingsApi().openFolder(folderId);
    } catch {
      setErrorMessage(t('history.projects.openFailed'));
    }
  };

  const previewProjectOrder = (projects: WorkspaceSettings['projects']) => {
    dispatchSettings({
      type: 'projectsOrderPreview',
      projectIds: projects.map((project) => project.project_id),
    });
  };

  const persistProjectOrder = async (projectIds: string[]): Promise<void> => {
    if (lockRef.current) throw new Error('Another workspace change is in progress.');
    lockRef.current = true;
    setBusy(true);
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
      lockRef.current = false;
      setBusy(false);
    }
  };

  const dragController = useWorkspaceProjectDragController({
    disabled: busy,
    generation: projectGeneration,
    projects: settings?.projects ?? [],
    t,
    onPreview: previewProjectOrder,
    onPersist: persistProjectOrder,
    onFailure: () => setErrorMessage(t('settings.workspace.saveFailed')),
  });

  return {
    addFolderToProject,
    addOrganization,
    addProjectFromFolder,
    busy,
    dragController,
    errorMessage,
    openFolder,
    pending,
    removeFolderFromProject,
    removeProject,
    renameProject,
    settings,
    updateProjectOrganizations,
  };
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
