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
      selectFolder: vi
        .fn()
        .mockResolvedValueOnce({ canceled: false, path: '/Users/me/aurora' })
        .mockResolvedValueOnce({ canceled: false, path: '/Users/me/billing' }),
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
      realPath: '/Users/me/aurora',
      organizationIds: [],
      projectIds: ['project-a'],
    });
    expect(result.current.settings?.projects).toEqual([project]);
    expect(result.current.settings?.folders).toHaveLength(1);

    createFolder.mockRejectedValueOnce(new Error('not a directory'));
    createProject.mockResolvedValueOnce({ ...project, project_id: 'project-b' });
    await act(async () => result.current.addProjectFromFolder());
    expect(deleteProject).toHaveBeenCalledWith('project-b');
    expect(result.current.settings?.projects).toEqual([project]);
    expect(result.current.errorMessage).toBe('settings.workspace.saveFailed');
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
    const createProject = vi.fn(async () => project('new', 3));
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
      createProject,
      createFolder,
      updateFolderLinks,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    // The sidebar's ＋ on it: nothing is created, its links stay, and the user is told why.
    await act(async () => result.current.addProjectFromFolder());
    expect(createProject).not.toHaveBeenCalled();
    expect(createFolder).not.toHaveBeenCalled();
    expect(folders[0].project_ids).toEqual(['a', 'b']);
    expect(result.current.settings?.projects.map((item) => item.project_id)).toEqual([
      'a',
      'b',
      'c',
    ]);
    expect(result.current.errorMessage).toBe('settings.workspace.folderAlreadyRegistered');

    // Settings' "Add folder" on project c: the folder joins c and keeps a and b.
    await act(async () => {
      expect(
        await result.current.createFolder({
          displayName: 'shared',
          realPath: '/Users/me/shared',
          organizationIds: [],
          projectIds: ['c'],
        })
      ).toBe(true);
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
    await act(async () => result.current.addProjectFromFolder());

    expect(result.current.errorMessage).toBeNull();
    expect(result.current.settings).toEqual(server);
  });

  it('never takes back a project it did not create when the name was taken', async () => {
    // As workspace_settings.py does since names conflict: a taken name is refused.
    const existing = {
      project_id: 'p-1',
      display_name: 'aurora',
      organization_ids: ['org-1'],
      sort_order: 0,
    };
    const projects = [existing];
    const createProject = vi.fn(async ({ displayName }: { displayName: string }) => {
      if (projects.some((item) => item.display_name === displayName))
        return { errorCode: 'PROJECT_NAME_TAKEN' as const };
      const created = { ...existing, project_id: 'p-new', display_name: displayName };
      projects.push(created);
      return created;
    });
    const deleteProject = vi.fn(async () => undefined);
    installWorkspaceApi({
      // The sidebar has not seen the project yet: it was created after the last read.
      get: async () => emptySettings,
      selectFolder: async () => ({ canceled: false, path: '/Volumes/work/aurora' }),
      createProject,
      createFolder: async () => {
        throw new Error('not a directory');
      },
      deleteProject,
    });
    const { result } = renderHook(() => useWorkspaceSettingsController(translate), { wrapper });
    await waitFor(() => expect(result.current.settings).not.toBeNull());

    await act(async () => result.current.addProjectFromFolder());

    expect(createProject.mock.calls.map(([input]) => input.displayName)).toEqual([
      'aurora',
      'aurora 2',
    ]);
    expect(deleteProject).toHaveBeenCalledOnce();
    expect(deleteProject).toHaveBeenCalledWith('p-new');
    expect(projects[0]).toEqual(existing);
    expect(result.current.settings?.projects).toEqual([]);
    expect(result.current.errorMessage).toBe('settings.workspace.saveFailed');
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
