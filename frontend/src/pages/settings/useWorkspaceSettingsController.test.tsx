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

    await waitFor(() => expect(result.current.settings).toEqual(emptySettings));
    expect(get).toHaveBeenCalledOnce();
  });

  it('guards duplicate mutation calls synchronously within the same tick', async () => {
    const createRequest = createDeferred<{ organization_id: string; display_name: string }>();
    const createOrganization = vi.fn(() => createRequest.promise);
    installWorkspaceApi({ get: async () => emptySettings, createOrganization });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    act(() => {
      void result.current.addOrganization('Org A');
      void result.current.addOrganization('Org A');
    });

    expect(createOrganization).toHaveBeenCalledOnce();
    await act(async () =>
      createRequest.resolve({ organization_id: 'org-a', display_name: 'Org A' })
    );
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
    await act(async () => initial.resolve(saved));
  });

  it('keeps a mutation that landed while the mount read was still in flight', async () => {
    setCachedWorkspaceSettings(OWNER.id, emptySettings);
    const read = createDeferred<typeof emptySettings>();
    const created = { organization_id: 'org-a', display_name: 'Org A' };
    installWorkspaceApi({ get: () => read.promise, createOrganization: async () => created });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await act(async () => {
      expect(await result.current.addOrganization('Org A')).toBe('org-a');
    });

    await act(async () => read.resolve(emptySettings));

    expect(result.current.settings?.organizations).toEqual([created]);
  });

  it('does not publish the defaults as the current settings when a mutation follows a failed read', async () => {
    installWorkspaceApi({
      get: async () => {
        throw new Error('workspace settings unavailable');
      },
      createOrganization: async () => ({ organization_id: 'org-a', display_name: 'Org A' }),
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.errorMessage).toBe('settings.workspace.loadFailed'));

    await act(async () => {
      await result.current.addOrganization('Org A');
    });

    // The read never answered, so there is nothing current to show or to cache: the one
    // created organization on top of the defaults is not this owner's workspace.
    expect(result.current.settings).toBeNull();
    expect(getCachedWorkspaceSettings(OWNER.id)).toBeNull();
  });

  it('never creates a registered folder again, whose upsert would replace its links', async () => {
    // As create_workspace_folder does: a folder is keyed by its canonical path, and creating it
    // again replaces every project and organization link it had.
    const folders = [
      {
        folder_id: 'shared',
        display_name: 'shared',
        real_path: '/Users/me/shared',
        canonical_real_path: '/Users/me/shared',
        organization_ids: [] as string[],
        project_ids: ['a', 'b'],
      },
    ];
    const project = (projectId: string, sortOrder: number) => ({
      project_id: projectId,
      display_name: projectId,
      organization_ids: [],
      sort_order: sortOrder,
    });
    const createFolder = vi.fn(
      async (input: { displayName: string; realPath: string; projectIds: string[] }) => {
        const existing = folders.find((folder) => folder.canonical_real_path === input.realPath);
        if (!existing) throw new Error('only the registered folder is picked here');
        existing.project_ids = [...input.projectIds];
        existing.organization_ids = [];
        return { ...existing };
      }
    );
    const updateFolderLinks = vi.fn(
      async (folderId: string, links: { organizationIds: string[]; projectIds: string[] }) => {
        const existing = folders.find((folder) => folder.folder_id === folderId)!;
        existing.project_ids = [...links.projectIds].sort();
        return { ...existing };
      }
    );
    installWorkspaceApi({
      get: async () => ({
        ...emptySettings,
        projects: [project('a', 0), project('b', 1), project('c', 2)],
        folders: folders.map((folder) => ({ ...folder })),
      }),
      selectFolder: async () => ({ canceled: false, path: '/Users/me/shared' }),
      createFolder,
      updateFolderLinks,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    // Add folder on project c: the folder joins c and keeps a and b.
    await act(async () => {
      expect(await result.current.addFolderToProject('c')).toBe(true);
    });
    expect(createFolder).not.toHaveBeenCalled();
    expect(folders[0].project_ids).toEqual(['a', 'b', 'c']);
    expect(result.current.settings?.folders[0].project_ids).toEqual(['a', 'b', 'c']);
  });

  it('removes a project in one call, and drops the folders only it held as the backend does', async () => {
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

    await act(async () => {
      expect(await result.current.removeProject('a')).toBe(true);
    });

    expect(deleteProject).toHaveBeenCalledOnce();
    expect(deleteProject).toHaveBeenCalledWith('a');
    expect(deleteFolder).not.toHaveBeenCalled();
    expect(result.current.settings?.projects.map((item) => item.project_id)).toEqual(['b']);
    expect(result.current.settings?.folders).toEqual([folder('shared', ['b'])]);
  });

  it('re-adds a folder whose project was deleted, and ends equal to the backend', async () => {
    // As workspace_settings.py does: deleting a project unregisters the folders only it held.
    type Settings = {
      read_access_scope: 'workspace';
      organizations: never[];
      projects: {
        project_id: string;
        display_name: string;
        sort_order: number;
        organization_ids: string[];
      }[];
      folders: {
        folder_id: string;
        display_name: string;
        real_path: string;
        canonical_real_path: string;
        organization_ids: string[];
        project_ids: string[];
      }[];
    };
    const server: Settings = {
      ...emptySettings,
      projects: [
        { project_id: 'p-1', display_name: 'aurora', sort_order: 0, organization_ids: [] },
      ],
      folders: [
        {
          folder_id: 'f-1',
          display_name: 'aurora',
          real_path: '/Users/me/aurora',
          canonical_real_path: '/Users/me/aurora',
          organization_ids: [],
          project_ids: ['p-1'],
        },
      ],
    };
    installWorkspaceApi({
      get: async () => structuredClone(server),
      deleteProject: async (projectId: string) => {
        server.projects = server.projects.filter((item) => item.project_id !== projectId);
        server.folders = server.folders.filter(
          (item) => !(item.project_ids.length === 1 && item.project_ids[0] === projectId)
        );
      },
      selectFolder: async () => ({ canceled: false, path: '/Users/me/aurora' }),
      createProject: async ({ displayName }: { displayName: string }) => {
        const created = {
          project_id: 'p-2',
          display_name: displayName,
          sort_order: 0,
          organization_ids: [],
        };
        server.projects.push(created);
        return { ...created };
      },
      createFolder: async (input: {
        displayName: string;
        realPath: string;
        projectIds: string[];
      }) => {
        const created = {
          folder_id: 'f-2',
          display_name: input.displayName,
          real_path: input.realPath,
          canonical_real_path: input.realPath,
          organization_ids: [],
          project_ids: input.projectIds,
        };
        server.folders.push(created);
        return structuredClone(created);
      },
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    await act(async () => {
      expect(await result.current.removeProject('p-1')).toBe(true);
    });
    await act(async () => {
      const project = await result.current.createProject('aurora');
      expect(await result.current.addFolderToProject(project!.project_id)).toBe(true);
    });

    expect(result.current.errorMessage).toBeNull();
    expect(result.current.settings).toEqual(server);
  });

  it('refuses a second add while the first is still in flight, and is busy until it ends', async () => {
    const folderRequest = createDeferred<{
      folder_id: string;
      display_name: string;
      real_path: string;
      canonical_real_path: string;
      organization_ids: string[];
      project_ids: string[];
    }>();
    const selectFolder = vi.fn(async () => ({ canceled: false, path: '/Users/me/aurora' }));
    const createFolder = vi.fn(() => folderRequest.promise);
    installWorkspaceApi({
      get: async () => ({
        ...emptySettings,
        projects: [{ project_id: 'p-1', display_name: 'a', sort_order: 0, organization_ids: [] }],
      }),
      selectFolder,
      createFolder,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    let first!: Promise<boolean>;
    act(() => {
      first = result.current.addFolderToProject('p-1');
    });
    await waitFor(() => expect(createFolder).toHaveBeenCalledOnce());
    expect(result.current.busy).toBe(true);
    await act(async () => {
      expect(await result.current.addFolderToProject('p-1')).toBe(false);
    });

    expect(selectFolder).toHaveBeenCalledOnce();
    await act(async () => {
      folderRequest.resolve({
        folder_id: 'f-1',
        display_name: 'aurora',
        real_path: '/Users/me/aurora',
        canonical_real_path: '/Users/me/aurora',
        organization_ids: [],
        project_ids: ['p-1'],
      });
      await first;
    });
    expect(result.current.busy).toBe(false);
    expect(result.current.settings?.folders.map((folder) => folder.project_ids)).toEqual([['p-1']]);
  });

  it('refuses a project delete while an unlink is in flight, so no folder comes back', async () => {
    const server = {
      ...emptySettings,
      projects: [
        { project_id: 'a', display_name: 'a', sort_order: 0, organization_ids: [] },
        { project_id: 'b', display_name: 'b', sort_order: 1, organization_ids: [] },
      ],
      folders: [
        {
          folder_id: 'shared',
          display_name: 'shared',
          real_path: '/Users/me/shared',
          canonical_real_path: '/Users/me/shared',
          organization_ids: [] as string[],
          project_ids: ['a', 'b'],
        },
      ],
    };
    const unlink = createDeferred<void>();
    const deleteProject = vi.fn(async () => undefined);
    installWorkspaceApi({
      get: async () => structuredClone(server),
      updateFolderLinks: async (_folderId: string, links: { projectIds: string[] }) => {
        await unlink.promise;
        server.folders[0].project_ids = links.projectIds;
        return structuredClone(server.folders[0]);
      },
      deleteProject,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    let unlinking!: Promise<boolean>;
    act(() => {
      unlinking = result.current.removeFolderFromProject('shared', 'b');
    });
    await act(async () => {
      expect(await result.current.removeProject('a')).toBe(false);
    });
    expect(deleteProject).not.toHaveBeenCalled();

    await act(async () => {
      unlink.resolve();
      expect(await unlinking).toBe(true);
    });
    expect(result.current.settings).toEqual(server);
  });

  it('creates a project by name with no folder, and refuses a taken name with an error', async () => {
    const existing = {
      project_id: 'p-1',
      display_name: 'aurora',
      sort_order: 0,
      organization_ids: [],
    };
    const createProject = vi
      .fn()
      .mockResolvedValueOnce({ errorCode: 'PROJECT_NAME_TAKEN' })
      .mockResolvedValueOnce({
        ...existing,
        project_id: 'p-2',
        display_name: 'billing',
        sort_order: 1,
      });
    installWorkspaceApi({
      get: async () => ({ ...emptySettings, projects: [existing] }),
      createProject,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    // Taken here: refused before anything is sent.
    await act(async () => expect(await result.current.createProject(' aurora ')).toBeNull());
    expect(createProject).not.toHaveBeenCalled();
    expect(result.current.errorMessage).toBe('history.projects.nameTaken');
    // Taken on the backend since the last read: its refusal says the same.
    await act(async () => expect(await result.current.createProject('billing')).toBeNull());
    expect(result.current.errorMessage).toBe('history.projects.nameTaken');

    await act(async () => {
      expect((await result.current.createProject('billing'))?.project_id).toBe('p-2');
    });
    expect(createProject).toHaveBeenLastCalledWith({ displayName: 'billing', organizationIds: [] });
    expect(result.current.settings?.projects.map((item) => item.display_name)).toEqual([
      'aurora',
      'billing',
    ]);
    expect(result.current.settings?.folders).toEqual([]);
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
