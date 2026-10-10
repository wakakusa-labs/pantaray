import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LocalOwnerContext } from '@/context/localOwnerContext';
import type { Translate } from './types';
import {
  clearWorkspaceSettingsCache,
  getCachedWorkspaceSettings,
  setCachedWorkspaceSettings,
} from './components/workspaceSettingsCache';
import { useWorkspaceSettingsController } from './useWorkspaceSettingsController';

const translate = ((key: string) => key) as Translate;
const OWNER = { kind: 'account', id: 'user-1' } as const;
const emptySettings = {
  read_access_scope: 'workspace' as const,
  organizations: [],
  projects: [],
  folders: [],
};
const testFocus = { onSuccess: 'focus-success', onFailure: 'focus-failure' };

// The owner boundary mounts this controller for one confirmed owner and unmounts it when the
// owner changes, so the controller is always exercised inside a single owner's scope here.
function wrapper({ children }: { children: ReactNode }) {
  return <LocalOwnerContext.Provider value={OWNER}>{children}</LocalOwnerContext.Provider>;
}

describe('useWorkspaceSettingsController', () => {
  afterEach(() => {
    cleanup();
    clearWorkspaceSettingsCache();
    delete window.electron;
  });

  it('owns the mount load and exposes the current settings', async () => {
    const get = vi.fn(async () => emptySettings);
    installWorkspaceApi({ get });

    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });

    await waitFor(() => expect(result.current.showLoading).toBe(false));
    expect(result.current.settings).toEqual(emptySettings);
    expect(get).toHaveBeenCalledOnce();
  });

  it('guards duplicate mutation calls synchronously within the same tick', async () => {
    const deleteRequest = createDeferred<void>();
    const deleteOrganization = vi.fn(() => deleteRequest.promise);
    installWorkspaceApi({
      get: async () => ({
        ...emptySettings,
        organizations: [{ organization_id: 'org-a', display_name: 'Org A' }],
      }),
      deleteOrganization,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.showLoading).toBe(false));

    act(() => {
      void result.current.deleteOrganization('org-a', testFocus);
      void result.current.deleteOrganization('org-a', testFocus);
    });

    expect(deleteOrganization).toHaveBeenCalledOnce();
    await act(async () => deleteRequest.resolve());
    await waitFor(() => expect(result.current.pending.size).toBe(0));
  });

  it('shows the settings cached for this owner before the read answers', async () => {
    const saved = {
      ...emptySettings,
      organizations: [{ organization_id: 'saved-org', display_name: 'Saved' }],
    };
    setCachedWorkspaceSettings(OWNER.id, saved);
    const initial = createDeferred<typeof saved>();
    installWorkspaceApi({ get: () => initial.promise });

    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });

    expect(result.current.settings).toEqual(saved);
    expect(result.current.showLoading).toBe(false);
    await act(async () => initial.resolve(saved));
  });

  it('keeps a mutation that landed while the mount read was still in flight', async () => {
    setCachedWorkspaceSettings(OWNER.id, emptySettings);
    const read = createDeferred<typeof emptySettings>();
    const created = {
      project_id: 'project-a',
      display_name: 'Project A',
      organization_ids: [],
      sort_order: 0,
    };
    installWorkspaceApi({ get: () => read.promise, createProject: async () => created });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await act(async () => {
      expect(await result.current.createProject('Project A', [])).toBe(true);
    });

    await act(async () => read.resolve(emptySettings));

    expect(result.current.settings?.projects).toEqual([created]);
  });

  it('does not publish the defaults as the current settings when a mutation follows a failed read', async () => {
    installWorkspaceApi({
      get: async () => {
        throw new Error('workspace settings unavailable');
      },
      createProject: async () => ({
        project_id: 'project-a',
        display_name: 'Project A',
        organization_ids: [],
        sort_order: 0,
      }),
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.showLoading).toBe(false));

    await act(async () => {
      await result.current.createProject('Project A', []);
    });

    // The read never answered, so there is nothing current to show or to cache: the one
    // created project on top of the defaults is not this owner's workspace.
    expect(result.current.settings).toBeNull();
    expect(getCachedWorkspaceSettings(OWNER.id)).toBeNull();
  });

  it('adds a chosen folder as a project of its own, and takes the project back if the folder fails', async () => {
    const project = {
      project_id: 'project-a',
      display_name: 'aurora',
      organization_ids: [],
      sort_order: 0,
    };
    const createProject = vi.fn(async () => project);
    const createFolder = vi.fn(async () => ({
      folder_id: 'folder-a',
      display_name: 'aurora',
      real_path: '/Users/me/aurora',
      canonical_real_path: '/Users/me/aurora',
      organization_ids: [],
      project_ids: ['project-a'],
    }));
    const deleteProject = vi.fn(async () => undefined);
    installWorkspaceApi({
      get: async () => emptySettings,
      selectFolder: async () => ({ canceled: false, path: '/Users/me/aurora/' }),
      createProject,
      createFolder,
      deleteProject,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.showLoading).toBe(false));

    await act(async () => result.current.addProjectFromFolder());
    expect(createProject).toHaveBeenCalledWith({ displayName: 'aurora', organizationIds: [] });
    expect(createFolder).toHaveBeenCalledWith({
      displayName: 'aurora',
      realPath: '/Users/me/aurora/',
      organizationIds: [],
      projectIds: ['project-a'],
    });
    expect(result.current.settings?.projects).toEqual([project]);
    expect(result.current.settings?.folders).toHaveLength(1);

    createFolder.mockRejectedValueOnce(new Error('already registered'));
    createProject.mockResolvedValueOnce({ ...project, project_id: 'project-b' });
    await act(async () => result.current.addProjectFromFolder());
    expect(deleteProject).toHaveBeenCalledWith('project-b');
    expect(result.current.settings?.projects).toEqual([project]);
    expect(result.current.errorMessage).toBe('settings.workspace.saveFailed');
  });

  it('removes a project with the folders only it holds, keeping a folder another project shares', async () => {
    const folder = (folderId: string, projectIds: string[]) => ({
      folder_id: folderId,
      display_name: folderId,
      real_path: `/Users/me/${folderId}`,
      canonical_real_path: `/Users/me/${folderId}`,
      organization_ids: [],
      project_ids: projectIds,
    });
    const project = (projectId: string, sortOrder: number) => ({
      project_id: projectId,
      display_name: projectId,
      organization_ids: [],
      sort_order: sortOrder,
    });
    const deleteFolder = vi.fn(async () => undefined);
    const deleteProject = vi.fn(async () => undefined);
    installWorkspaceApi({
      get: async () => ({
        ...emptySettings,
        projects: [project('a', 0), project('b', 1)],
        folders: [folder('own', ['a']), folder('shared', ['a', 'b'])],
      }),
      deleteFolder,
      deleteProject,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    deleteFolder.mockRejectedValueOnce(new Error('offline'));
    await act(async () => {
      expect(await result.current.removeProject('a')).toBe(false);
    });
    // The project stays while its folder could not go, so nothing is left registered unseen.
    expect(deleteProject).not.toHaveBeenCalled();

    await act(async () => {
      expect(await result.current.removeProject('a')).toBe(true);
    });
    expect(deleteFolder).toHaveBeenLastCalledWith('own');
    expect(deleteFolder).toHaveBeenCalledTimes(2);
    expect(deleteProject).toHaveBeenCalledWith('a');
    expect(result.current.settings?.projects.map((item) => item.project_id)).toEqual(['b']);
    expect(result.current.settings?.folders.map((item) => item.folder_id)).toEqual(['shared']);
  });
});

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function installWorkspaceApi(
  workspaceSettings: Partial<NonNullable<NonNullable<Window['electron']>['workspaceSettings']>>
) {
  Object.defineProperty(window, 'electron', { configurable: true, value: { workspaceSettings } });
}
