import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { WorkspaceSettingsSection } from './WorkspaceSettingsSection';
import type { Translate } from '../types';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { clearWorkspaceSettingsCache, setCachedWorkspaceSettings } from './workspaceSettingsCache';
import type {
  WorkspaceFolder,
  WorkspaceOrganization,
  WorkspaceProject,
} from './workspaceSettingsModel';
import { t as translateMessage } from '@/i18n/translate';

const translate: Translate = ((key: string) => key) as Translate;
const japaneseTranslate: Translate = (key, vars) => translateMessage('ja', key, vars);
const OWNER = { kind: 'account', id: 'user-1' } as const;

const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
const originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close');
const showModal = vi.fn(function (this: HTMLDialogElement) {
  this.setAttribute('open', '');
});
const closeDialog = vi.fn(function (this: HTMLDialogElement) {
  this.removeAttribute('open');
});

const emptySettings = {
  read_access_scope: 'workspace' as const,
  organizations: [],
  projects: [],
  folders: [],
};

function createDeferred<T>() {
  let resolveValue: (value: T) => void = () => {};
  let rejectValue: (reason?: unknown) => void = () => {};
  const promise = new Promise<T>((resolve, reject) => {
    resolveValue = resolve;
    rejectValue = reject;
  });
  return { promise, reject: rejectValue, resolve: resolveValue };
}

function renderWorkspaceSettingsSection(t: Translate = translate) {
  return render(
    <LocalOwnerContext.Provider value={OWNER}>
      <MemoryRouter>
        <WorkspaceSettingsSection t={t} />
      </MemoryRouter>
    </LocalOwnerContext.Provider>
  );
}

interface TestWorkspaceProject {
  project_id: string;
  display_name: string;
  organization_ids: string[];
}

type UpdateProjectLinks = (
  projectId: string,
  input: { organizationIds: string[] }
) => Promise<TestWorkspaceProject>;

function installProjectOrganizationSettings(
  organizationIds: string[],
  updateImplementation?: UpdateProjectLinks
) {
  const settings = {
    read_access_scope: 'workspace' as const,
    organizations: [
      { organization_id: 'org-a', display_name: 'Org A' },
      { organization_id: 'org-b', display_name: 'Org B' },
      { organization_id: 'org-c', display_name: 'Org C' },
    ],
    projects: [
      {
        project_id: 'project-a',
        display_name: 'Project A',
        organization_ids: [...organizationIds],
      },
    ],
    folders: [],
  };
  const updateProjectLinks = vi.fn<UpdateProjectLinks>(
    updateImplementation ??
      (async (projectId, input) => {
        const project = settings.projects.find((candidate) => candidate.project_id === projectId);
        if (!project) throw new Error('Project not found.');
        project.organization_ids = [...input.organizationIds];
        return { ...project, organization_ids: [...project.organization_ids] };
      })
  );
  const workspaceSettings = {
    get: vi.fn(async () => settings),
    createOrganization: vi.fn(),
    createProject: vi.fn(),
    createFolder: vi.fn(),
    deleteOrganization: vi.fn(),
    deleteProject: vi.fn(),
    deleteFolder: vi.fn(),
    updateProjectLinks,
    updateFolderLinks: vi.fn(),
    updateReadAccessScope: vi.fn(),
    selectFolder: vi.fn(),
  };
  window.electron = { workspaceSettings } as unknown as Window['electron'];
  return workspaceSettings;
}

type CreateOrganization = (input: { displayName: string }) => Promise<WorkspaceOrganization>;

function installOrganizationPickerSettings(
  organizations: WorkspaceOrganization[],
  createImplementation: CreateOrganization
) {
  const workspaceSettings = installProjectOrganizationSettings([]);
  workspaceSettings.get.mockResolvedValue({
    read_access_scope: 'workspace',
    organizations: organizations.map((organization) => ({ ...organization })),
    projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
    folders: [],
  });
  workspaceSettings.createOrganization.mockImplementation(createImplementation);
  return workspaceSettings;
}

async function openProjectOrganizationPicker(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('heading', { name: 'Project A' });
  await user.click(screen.getByRole('button', { name: 'Project Aの組織を選択' }));
  return screen.getByRole('dialog', { name: 'Project Aの組織を選択' });
}

type DeleteOrganization = (organizationId: string) => Promise<void>;
type DeleteFolder = (folderId: string) => Promise<void>;

function installOrganizationManagerSettings(
  organizations: WorkspaceOrganization[],
  deleteImplementation?: DeleteOrganization
) {
  const settings = {
    read_access_scope: 'workspace' as const,
    organizations: organizations.map((organization) => ({ ...organization })),
    projects: [],
    folders: [],
  };
  const deleteOrganization = vi.fn<DeleteOrganization>(async (organizationId) => {
    if (deleteImplementation) await deleteImplementation(organizationId);
    settings.organizations = settings.organizations.filter(
      (organization) => organization.organization_id !== organizationId
    );
  });
  const workspaceSettings = {
    get: vi.fn(async () => ({
      ...settings,
      organizations: settings.organizations.map((organization) => ({ ...organization })),
    })),
    createOrganization: vi.fn(),
    createProject: vi.fn(),
    createFolder: vi.fn(),
    deleteOrganization,
    deleteProject: vi.fn(),
    deleteFolder: vi.fn(),
    updateProjectLinks: vi.fn(),
    updateFolderLinks: vi.fn(),
    updateReadAccessScope: vi.fn(),
    selectFolder: vi.fn(),
  };
  window.electron = { workspaceSettings } as unknown as Window['electron'];
  return workspaceSettings;
}

function installDeletionFocusSettings(
  projects: WorkspaceProject[],
  folders: WorkspaceFolder[] = [],
  deleteFolderImplementation?: DeleteFolder
) {
  const settings = {
    read_access_scope: 'workspace' as const,
    organizations: [] as WorkspaceOrganization[],
    projects: projects.map((project) => ({ ...project })),
    folders: folders.map((folder) => ({ ...folder })),
  };
  const workspaceSettings = {
    get: vi.fn(async () => ({
      ...settings,
      projects: settings.projects.map((project) => ({ ...project })),
      folders: settings.folders.map((folder) => ({ ...folder })),
    })),
    createOrganization: vi.fn(),
    createProject: vi.fn(),
    createFolder: vi.fn(),
    deleteOrganization: vi.fn(),
    deleteProject: vi.fn(async (projectId: string) => {
      settings.projects = settings.projects.filter((project) => project.project_id !== projectId);
    }),
    deleteFolder: vi.fn(async (folderId: string) => {
      if (deleteFolderImplementation) await deleteFolderImplementation(folderId);
      settings.folders = settings.folders.filter((folder) => folder.folder_id !== folderId);
    }),
    updateProjectLinks: vi.fn(),
    updateFolderLinks: vi.fn(),
    updateReadAccessScope: vi.fn(),
    selectFolder: vi.fn(),
  };
  window.electron = { workspaceSettings } as unknown as Window['electron'];
  return workspaceSettings;
}

/** The detail column of the selected project, found by its heading. */
async function findProjectDetail(name: string): Promise<HTMLElement> {
  const detail = (await screen.findByRole('heading', { name })).closest('article');
  if (!(detail instanceof HTMLElement)) throw new Error(`Project detail ${name} was not rendered.`);
  return detail;
}

function openProjectCreatePopover() {
  fireEvent.click(screen.getByRole('button', { name: 'settings.workspace.addProject' }));
  return screen.getByRole('dialog', { name: 'settings.workspace.projectCreate.title' });
}

function openOrganizationDialog() {
  fireEvent.click(
    within(openProjectCreatePopover()).getByRole('button', {
      name: 'settings.workspace.organizationManager.open',
    })
  );
  return screen.getByRole('dialog', { name: 'settings.workspace.organizationManager.title' });
}

describe('WorkspaceSettingsSection', () => {
  beforeEach(() => {
    showModal.mockClear();
    closeDialog.mockClear();
    Object.defineProperties(HTMLDialogElement.prototype, {
      showModal: { configurable: true, value: showModal },
      close: { configurable: true, value: closeDialog },
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearWorkspaceSettingsCache();
    delete window.electron;
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
    }
    if (originalClose) {
      Object.defineProperty(HTMLDialogElement.prototype, 'close', originalClose);
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, 'close');
    }
  });

  it('registers folders automatically through the native folder picker', async () => {
    const workspaceSettings = {
      get: vi.fn(async () => ({
        read_access_scope: 'workspace',
        organizations: [],
        projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
        folders: [],
      })),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(async () => ({
        folder_id: 'folder-1',
        display_name: 'Project',
        real_path: '/Users/example/Project',
        canonical_real_path: '/Users/example/Project',
        organization_ids: [],
        project_ids: ['project-a'],
      })),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(async () => ({
        canceled: false,
        path: '/Users/example/Project',
      })),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });

    fireEvent.click(screen.getByRole('button', { name: 'settings.workspace.selectFolder' }));

    await waitFor(() => {
      expect(workspaceSettings.createFolder).toHaveBeenCalledWith({
        displayName: 'Project',
        realPath: '/Users/example/Project',
        organizationIds: [],
        projectIds: ['project-a'],
      });
    });
  });

  it('offers the getting started hint and the primary add project action only while empty', async () => {
    const settings = {
      read_access_scope: 'workspace' as const,
      organizations: [],
      projects: [] as { project_id: string; display_name: string; organization_ids: string[] }[],
      folders: [],
    };
    const workspaceSettings = {
      get: vi.fn(async () => settings),
      createOrganization: vi.fn(),
      createProject: vi.fn(async () => ({
        project_id: 'project-new',
        display_name: 'New Project',
        organization_ids: [],
      })),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    expect(await screen.findByText('settings.workspace.gettingStarted')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'settings.workspace.addProject' })).toBeEnabled();

    const popover = openProjectCreatePopover();
    fireEvent.change(
      within(popover).getByRole('textbox', { name: 'settings.workspace.projectPlaceholder' }),
      { target: { value: 'New Project' } }
    );
    fireEvent.click(within(popover).getByRole('button', { name: 'settings.workspace.addProject' }));

    await waitFor(() => {
      expect(screen.queryByText('settings.workspace.gettingStarted')).toBeNull();
    });
  });

  it('expands a folderless project and focuses its primary add folder action after creation', async () => {
    const settings = {
      read_access_scope: 'workspace' as const,
      organizations: [],
      projects: [] as { project_id: string; display_name: string; organization_ids: string[] }[],
      folders: [],
    };
    const workspaceSettings = {
      get: vi.fn(async () => settings),
      createOrganization: vi.fn(),
      createProject: vi.fn(async () => ({
        project_id: 'project-new',
        display_name: 'New Project',
        organization_ids: [],
      })),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    const popover = await waitFor(() => openProjectCreatePopover());
    fireEvent.change(
      within(popover).getByRole('textbox', { name: 'settings.workspace.projectPlaceholder' }),
      { target: { value: 'New Project' } }
    );
    fireEvent.click(within(popover).getByRole('button', { name: 'settings.workspace.addProject' }));

    await waitFor(() => {
      const addFolder = screen.getByRole('button', { name: 'settings.workspace.selectFolder' });
      expect(addFolder).toHaveClass('workspace-button-primary');
      expect(addFolder).toHaveFocus();
    });
    expect(screen.getByRole('button', { name: 'New Project' })).toHaveAttribute(
      'aria-current',
      'true'
    );
  });

  it('withholds workspace mutations until the initial settings baseline is loaded', async () => {
    const initialSettings = {
      read_access_scope: 'workspace' as const,
      organizations: [{ organization_id: 'org-a', display_name: 'Org A' }],
      projects: [
        {
          project_id: 'project-a',
          display_name: 'Project A',
          sort_order: 0,
          organization_ids: ['org-a'],
        },
      ],
      folders: [],
    };
    const initialLoad = createDeferred<typeof initialSettings>();
    const workspaceSettings = {
      get: vi.fn(() => initialLoad.promise),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    expect(screen.getByText('common.loading')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'settings.workspace.addProject' })).toBeNull();

    initialLoad.resolve(initialSettings);

    expect(
      await screen.findByRole('button', { name: 'settings.workspace.addProject' })
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Project A' })).toBeInTheDocument();
  });

  it('uses cached workspace settings immediately when remounted', async () => {
    const workspaceSettings = {
      get: vi
        .fn()
        .mockResolvedValueOnce({
          read_access_scope: 'workspace',
          organizations: [],
          projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
          folders: [],
        })
        .mockResolvedValueOnce({
          read_access_scope: 'workspace',
          organizations: [],
          projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
          folders: [],
        }),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    const firstRender = renderWorkspaceSettingsSection();
    await screen.findByRole('heading', { name: 'Project A' });
    firstRender.unmount();

    renderWorkspaceSettingsSection();

    expect(screen.getByRole('heading', { name: 'Project A' })).toBeInTheDocument();
    expect(screen.queryByText('common.loading')).toBeNull();
    await waitFor(() => {
      expect(workspaceSettings.get).toHaveBeenCalledTimes(2);
    });
  });

  it('shows a load failure instead of keeping the initial spinner forever', async () => {
    window.electron = {
      workspaceSettings: {
        get: vi.fn(async () => {
          throw new Error('workspace settings unavailable');
        }),
        createOrganization: vi.fn(),
        createProject: vi.fn(),
        createFolder: vi.fn(),
        deleteOrganization: vi.fn(),
        deleteProject: vi.fn(),
        deleteFolder: vi.fn(),
        updateProjectLinks: vi.fn(),
        updateFolderLinks: vi.fn(),
        updateReadAccessScope: vi.fn(),
        selectFolder: vi.fn(),
      },
    } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    expect(await screen.findByRole('alert')).toHaveTextContent('settings.workspace.loadFailed');
    expect(screen.queryByText('common.loading')).toBeNull();
  });

  it('renders a successful organization create response without reloading', async () => {
    const user = userEvent.setup();
    const workspaceSettings = {
      get: vi.fn(async () => emptySettings),
      createOrganization: vi.fn(async () => ({
        organization_id: 'org-new',
        display_name: 'New Org',
      })),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    await screen.findByRole('button', { name: 'settings.workspace.addProject' });
    const organizationDialog = openOrganizationDialog();
    const organizationInput = within(organizationDialog).getByLabelText(
      'settings.workspace.organizationPlaceholder'
    );
    await user.type(organizationInput, 'New Org');
    await user.click(
      within(organizationDialog).getByRole('button', {
        name: 'settings.workspace.addOrganization',
      })
    );

    await waitFor(() => {
      expect(workspaceSettings.createOrganization).toHaveBeenCalledWith({
        displayName: 'New Org',
      });
      expect(organizationInput).toHaveValue('');
      expect(within(organizationDialog).getByText('New Org')).toBeInTheDocument();
      expect(workspaceSettings.get).toHaveBeenCalledOnce();
    });
  });

  it('leaves the Enter that commits an IME conversion to the IME in workspace name fields', async () => {
    const workspaceSettings = {
      get: vi.fn(async () => emptySettings),
      createOrganization: vi.fn(async () => ({
        organization_id: 'org-new',
        display_name: '新しい組織',
      })),
      createProject: vi.fn(async () => ({
        project_id: 'project-new',
        display_name: '新しい案件',
        organization_ids: [],
      })),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    await screen.findByRole('button', { name: 'settings.workspace.addProject' });
    const organizationDialog = openOrganizationDialog();
    const organizationInput = within(organizationDialog).getByLabelText(
      'settings.workspace.organizationPlaceholder'
    );
    fireEvent.change(organizationInput, { target: { value: '新しい組織' } });
    fireEvent.keyDown(organizationInput, { key: 'Enter', isComposing: true });
    expect(workspaceSettings.createOrganization).not.toHaveBeenCalled();
    fireEvent.keyDown(organizationInput, { key: 'Enter' });
    await waitFor(() => {
      expect(workspaceSettings.createOrganization).toHaveBeenCalledWith({
        displayName: '新しい組織',
      });
    });
    // Closing the manager returns to the project popover it was opened from.
    fireEvent.click(within(organizationDialog).getByRole('button', { name: 'common.close' }));

    const popover = screen.getByRole('dialog', { name: 'settings.workspace.projectCreate.title' });
    const projectInput = within(popover).getByRole('textbox', {
      name: 'settings.workspace.projectPlaceholder',
    });
    fireEvent.change(projectInput, { target: { value: '新しい案件' } });
    fireEvent.keyDown(projectInput, { key: 'Enter', isComposing: true });
    expect(workspaceSettings.createProject).not.toHaveBeenCalled();
    fireEvent.keyDown(projectInput, { key: 'Enter' });
    await waitFor(() => {
      expect(workspaceSettings.createProject).toHaveBeenCalledOnce();
    });
  });

  it('delegates organization dialog focus and closing to the native dialog API', async () => {
    const user = userEvent.setup();
    window.electron = {
      workspaceSettings: {
        get: vi.fn(async () => emptySettings),
        createOrganization: vi.fn(),
        createProject: vi.fn(),
        createFolder: vi.fn(),
        deleteOrganization: vi.fn(),
        deleteProject: vi.fn(),
        deleteFolder: vi.fn(),
        updateProjectLinks: vi.fn(),
        updateFolderLinks: vi.fn(),
        updateReadAccessScope: vi.fn(),
        selectFolder: vi.fn(),
      },
    } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    await screen.findByRole('button', { name: 'settings.workspace.addProject' });
    openOrganizationDialog();

    expect(showModal).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('settings.workspace.organizationPlaceholder')).toHaveFocus();
    await user.click(screen.getByRole('button', { name: 'common.close' }));

    await waitFor(() => expect(closeDialog).toHaveBeenCalledOnce());
  });

  it('does not let a stale cached mount response overwrite a mutation commit', async () => {
    const cachedSettings = {
      read_access_scope: 'workspace' as const,
      organizations: [{ organization_id: 'org-cached', display_name: 'Cached Org' }],
      projects: [],
      folders: [],
    };
    const staleRefresh = createDeferred<typeof cachedSettings>();
    const workspaceSettings = {
      get: vi.fn(() => staleRefresh.promise),
      createOrganization: vi.fn(async () => ({
        organization_id: 'org-new',
        display_name: 'New Org',
      })),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    setCachedWorkspaceSettings(OWNER.id, cachedSettings);
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    const organizationDialog = openOrganizationDialog();
    expect(within(organizationDialog).getByText('Cached Org')).toBeInTheDocument();
    fireEvent.change(
      within(organizationDialog).getByLabelText('settings.workspace.organizationPlaceholder'),
      {
        target: { value: 'New Org' },
      }
    );
    fireEvent.click(
      within(organizationDialog).getByRole('button', {
        name: 'settings.workspace.addOrganization',
      })
    );
    await waitFor(() => {
      expect(workspaceSettings.createOrganization).toHaveBeenCalledWith({ displayName: 'New Org' });
      expect(within(organizationDialog).getByText('New Org')).toBeInTheDocument();
      expect(workspaceSettings.get).toHaveBeenCalledOnce();
    });

    staleRefresh.resolve(cachedSettings);

    await waitFor(() => {
      expect(screen.getByText('New Org')).toBeInTheDocument();
      expect(screen.getByText('Cached Org')).toBeInTheDocument();
    });
  });

  it('shows folders under expanded projects without organization chips', async () => {
    const workspaceSettings = {
      get: vi.fn(async () => ({
        read_access_scope: 'workspace',
        organizations: [
          { organization_id: 'org-a', display_name: 'Org A' },
          { organization_id: 'org-b', display_name: 'Org B' },
        ],
        projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
        folders: [
          {
            folder_id: 'folder-a',
            display_name: 'Folder A',
            real_path: '/Users/example/FolderA',
            canonical_real_path: '/Users/example/FolderA',
            organization_ids: [],
            project_ids: ['project-a'],
          },
        ],
      })),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    const projectDetail = await findProjectDetail('Project A');

    expect(within(projectDetail).getByText('Folder A')).toBeInTheDocument();
    expect(within(projectDetail).queryByRole('button', { name: 'Org B' })).toBeNull();
    expect(within(projectDetail).queryByRole('button', { name: 'common.save' })).toBeNull();
  });

  it('deletes created workspace settings entries', async () => {
    const workspaceSettings = {
      get: vi.fn(async () => ({
        read_access_scope: 'workspace',
        organizations: [{ organization_id: 'org-a', display_name: 'Org A' }],
        projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
        folders: [
          {
            folder_id: 'folder-a',
            display_name: 'Folder A',
            real_path: '/Users/example/FolderA',
            canonical_real_path: '/Users/example/FolderA',
            organization_ids: [],
            project_ids: ['project-a'],
          },
        ],
      })),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    await screen.findAllByText('Org A');
    const organizationDialog = openOrganizationDialog();
    fireEvent.click(
      within(organizationDialog).getByRole('button', { name: 'common.delete Org A' })
    );
    await waitFor(() => {
      expect(workspaceSettings.deleteOrganization).toHaveBeenCalledWith('org-a');
    });

    const projectDetail = await findProjectDetail('Project A');
    const deleteProjectButton = within(projectDetail).getByRole('button', {
      name: 'common.delete Project A',
    });
    expect(deleteProjectButton).toHaveClass('workspace-project-delete');
    expect(within(projectDetail).queryByRole('menu')).toBeNull();

    fireEvent.click(within(projectDetail).getByRole('button', { name: 'common.delete Folder A' }));
    await waitFor(() => {
      expect(workspaceSettings.deleteFolder).toHaveBeenCalledWith('folder-a');
    });
    expect(
      screen.getByRole('button', { name: 'settings.workspace.drag.handle' })
    ).not.toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(deleteProjectButton);
    await waitFor(() => {
      expect(workspaceSettings.deleteProject).toHaveBeenCalledWith('project-a');
    });
  });

  it('focuses the next organization delete button after deletion', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installOrganizationManagerSettings([
      { organization_id: 'org-b', display_name: 'Org B' },
      { organization_id: 'org-a', display_name: 'Org A' },
    ]);

    renderWorkspaceSettingsSection();

    await screen.findByText('Org A');
    const organizationDialog = openOrganizationDialog();
    await user.click(
      within(organizationDialog).getByRole('button', { name: 'common.delete Org A' })
    );

    await waitFor(() => {
      expect(workspaceSettings.deleteOrganization).toHaveBeenCalledWith('org-a');
      expect(
        within(organizationDialog).getByRole('button', { name: 'common.delete Org B' })
      ).toHaveFocus();
    });
  });

  it('focuses the organization input after deleting the final organization', async () => {
    const user = userEvent.setup();
    installOrganizationManagerSettings([{ organization_id: 'org-a', display_name: 'Org A' }]);

    renderWorkspaceSettingsSection();

    await screen.findByText('Org A');
    const organizationDialog = openOrganizationDialog();
    await user.click(
      within(organizationDialog).getByRole('button', { name: 'common.delete Org A' })
    );

    await waitFor(() => {
      expect(within(organizationDialog).queryByText('Org A')).toBeNull();
      expect(
        within(organizationDialog).getByLabelText('settings.workspace.organizationPlaceholder')
      ).toHaveFocus();
    });
  });

  it('marks organization deletion busy, prevents duplicate IPC, and restores focus on failure', async () => {
    const user = userEvent.setup();
    const deleteRequest = createDeferred<void>();
    const workspaceSettings = installOrganizationManagerSettings(
      [{ organization_id: 'org-a', display_name: 'Org A' }],
      () => deleteRequest.promise
    );

    renderWorkspaceSettingsSection();

    await screen.findByText('Org A');
    const organizationDialog = openOrganizationDialog();
    const deleteButton = within(organizationDialog).getByRole('button', {
      name: 'common.delete Org A',
    });
    await user.click(deleteButton);
    await waitFor(() => expect(deleteButton).toHaveAttribute('aria-busy', 'true'));
    expect(deleteButton).toBeEnabled();
    await user.click(deleteButton);
    expect(workspaceSettings.deleteOrganization).toHaveBeenCalledOnce();
    within(organizationDialog).getByLabelText('settings.workspace.organizationPlaceholder').focus();
    deleteRequest.reject(new Error('delete failed'));

    await waitFor(() => {
      expect(
        within(organizationDialog).getByRole('button', { name: 'common.delete Org A' })
      ).toHaveFocus();
      expect(screen.getByRole('alert')).toHaveTextContent('settings.workspace.saveFailed');
    });
  });

  it('selects the next project and focuses its heading after deletion', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installDeletionFocusSettings([
      { project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] },
      { project_id: 'project-b', display_name: 'Project B', sort_order: 1, organization_ids: [] },
    ]);

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });
    await user.click(screen.getByRole('button', { name: 'common.delete Project A' }));

    await waitFor(() => {
      expect(workspaceSettings.deleteProject).toHaveBeenCalledWith('project-a');
      expect(screen.getByRole('heading', { name: 'Project B' })).toHaveFocus();
    });
    expect(screen.getByRole('button', { name: 'Project B' })).toHaveAttribute(
      'aria-current',
      'true'
    );
  });

  it('hands a deleted selected project to the one that took its place', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installDeletionFocusSettings(
      [
        { project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] },
        { project_id: 'project-b', display_name: 'Project B', sort_order: 1, organization_ids: [] },
        { project_id: 'project-c', display_name: 'Project C', sort_order: 2, organization_ids: [] },
      ],
      [
        {
          folder_id: 'folder-a',
          display_name: 'Folder A',
          real_path: '/workspace/a',
          canonical_real_path: '/workspace/a',
          organization_ids: [],
          project_ids: ['project-a'],
        },
        {
          folder_id: 'folder-b',
          display_name: 'Folder B',
          real_path: '/workspace/b',
          canonical_real_path: '/workspace/b',
          organization_ids: [],
          project_ids: ['project-a'],
        },
      ]
    );

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });
    expect(screen.getByRole('button', { name: 'common.delete Folder B' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Project B' }));
    await user.click(screen.getByRole('button', { name: 'common.delete Project B' }));

    await waitFor(() => {
      expect(workspaceSettings.deleteProject).toHaveBeenCalledWith('project-b');
      expect(screen.getByRole('heading', { name: 'Project C' })).toHaveFocus();
    });
  });

  it('deletes the project shown after the unassigned fallback and selects the next one', async () => {
    const user = userEvent.setup();
    const folder = (folderId: string, projectIds: string[]) => ({
      folder_id: folderId,
      display_name: folderId,
      real_path: `/workspace/${folderId}`,
      canonical_real_path: `/workspace/${folderId}`,
      organization_ids: [],
      project_ids: projectIds,
    });
    const workspaceSettings = installDeletionFocusSettings(
      [
        { project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] },
        { project_id: 'project-b', display_name: 'Project B', sort_order: 1, organization_ids: [] },
      ],
      [folder('loose', []), folder('held', ['project-a'])]
    );
    workspaceSettings.updateFolderLinks.mockImplementation(async () =>
      folder('loose', ['project-b'])
    );

    renderWorkspaceSettingsSection();
    await user.click(
      await screen.findByRole('button', { name: 'settings.workspace.unassigned.title' })
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'settings.workspace.unassigned.project loose' }),
      'project-b'
    );
    await user.click(screen.getByRole('button', { name: 'settings.workspace.unassigned.assign' }));
    // Nothing is left unassigned, so the first project is shown.
    expect(await screen.findByRole('heading', { name: 'Project A' })).toBeInTheDocument();

    // Deleting it leaves its folder unassigned; the next project still takes its place.
    await user.click(screen.getByRole('button', { name: 'common.delete Project A' }));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Project B' })).toHaveFocus();
    });
    expect(screen.getByRole('button', { name: 'Project B' })).toHaveAttribute(
      'aria-current',
      'true'
    );
  });

  it('focuses the add project button after deleting the final project', async () => {
    const user = userEvent.setup();
    installDeletionFocusSettings([
      { project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] },
    ]);

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });
    await user.click(screen.getByRole('button', { name: 'common.delete Project A' }));

    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Project A' })).toBeNull();
      expect(screen.getByRole('button', { name: 'settings.workspace.addProject' })).toHaveFocus();
    });
  });

  it('focuses the next folder delete button after deletion', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installDeletionFocusSettings(
      [{ project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] }],
      [
        {
          folder_id: 'folder-a',
          display_name: 'Folder A',
          real_path: '/workspace/a',
          canonical_real_path: '/workspace/a',
          organization_ids: [],
          project_ids: ['project-a'],
        },
        {
          folder_id: 'folder-b',
          display_name: 'Folder B',
          real_path: '/workspace/b',
          canonical_real_path: '/workspace/b',
          organization_ids: [],
          project_ids: ['project-a'],
        },
      ]
    );

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });
    await user.click(screen.getByRole('button', { name: 'common.delete Folder A' }));

    await waitFor(() => {
      expect(workspaceSettings.deleteFolder).toHaveBeenCalledWith('folder-a');
      expect(screen.getByRole('button', { name: 'common.delete Folder B' })).toHaveFocus();
    });
  });

  it('focuses the folder picker after deleting the final folder', async () => {
    const user = userEvent.setup();
    installDeletionFocusSettings(
      [{ project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] }],
      [
        {
          folder_id: 'folder-a',
          display_name: 'Folder A',
          real_path: '/workspace/a',
          canonical_real_path: '/workspace/a',
          organization_ids: [],
          project_ids: ['project-a'],
        },
      ]
    );

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });
    await user.click(screen.getByRole('button', { name: 'common.delete Folder A' }));

    await waitFor(() => {
      expect(screen.queryByText('Folder A')).toBeNull();
      expect(screen.getByRole('button', { name: 'settings.workspace.selectFolder' })).toHaveFocus();
    });
  });

  it('restores folder delete focus to the source project when a shared folder deletion fails', async () => {
    const user = userEvent.setup();
    const deleteRequest = createDeferred<void>();
    const workspaceSettings = installDeletionFocusSettings(
      [
        { project_id: 'project-a', display_name: 'Project A', sort_order: 0, organization_ids: [] },
        { project_id: 'project-b', display_name: 'Project B', sort_order: 1, organization_ids: [] },
      ],
      [
        {
          folder_id: 'folder-a',
          display_name: 'Folder A',
          real_path: '/workspace/a',
          canonical_real_path: '/workspace/a',
          organization_ids: [],
          project_ids: ['project-a', 'project-b'],
        },
      ],
      () => deleteRequest.promise
    );

    renderWorkspaceSettingsSection();

    await screen.findByRole('heading', { name: 'Project A' });
    await user.click(screen.getByRole('button', { name: 'Project B' }));
    const projectBDetail = await findProjectDetail('Project B');
    const projectBDelete = within(projectBDetail).getByRole('button', {
      name: 'common.delete Folder A',
    });
    expect(projectBDelete.id).toContain('project-b');

    projectBDelete.focus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(workspaceSettings.deleteFolder).toHaveBeenCalledWith('folder-a'));
    screen.getByRole('button', { name: 'Project A' }).focus();
    deleteRequest.reject(new Error('delete failed'));

    await waitFor(() => {
      expect(projectBDelete).toHaveFocus();
      expect(screen.getByRole('alert')).toHaveTextContent('settings.workspace.saveFailed');
    });
  });

  it('renders every resolved legacy organization chip without an add or overflow control', async () => {
    window.electron = {
      workspaceSettings: {
        get: vi.fn(async () => ({
          read_access_scope: 'workspace',
          organizations: [
            { organization_id: 'org-b', display_name: 'Org B' },
            { organization_id: 'org-a', display_name: 'Org A' },
            { organization_id: 'org-c', display_name: 'Org C' },
          ],
          projects: [
            {
              project_id: 'project-a',
              display_name: 'Project A',
              organization_ids: ['org-b', 'missing-org', 'org-c', 'org-a'],
            },
          ],
          folders: [],
        })),
        createOrganization: vi.fn(),
        createProject: vi.fn(),
        createFolder: vi.fn(),
        deleteOrganization: vi.fn(),
        deleteProject: vi.fn(),
        deleteFolder: vi.fn(),
        updateProjectLinks: vi.fn(),
        updateFolderLinks: vi.fn(),
        updateReadAccessScope: vi.fn(),
        selectFolder: vi.fn(),
      },
    } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();

    const projectCard = await findProjectDetail('Project A');
    expect(within(projectCard).getByText('Org A')).toBeInTheDocument();
    expect(within(projectCard).getByText('Org B')).toBeInTheDocument();
    expect(within(projectCard).getByText('Org C')).toBeInTheDocument();
    expect(within(projectCard).queryByText('missing-org')).toBeNull();
    expect(
      within(projectCard).queryByRole('button', {
        name: 'settings.workspace.projectOrganization.select',
      })
    ).toBeNull();
  });

  it('removes an organization by keyboard and preserves every other project link', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installProjectOrganizationSettings(['org-a', 'missing-org', 'org-b']);

    renderWorkspaceSettingsSection(japaneseTranslate);
    const projectCard = await findProjectDetail('Project A');
    const removeButton = within(projectCard).getByRole('button', { name: 'Org Aを外す' });

    removeButton.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(workspaceSettings.updateProjectLinks).toHaveBeenCalledWith('project-a', {
        organizationIds: ['missing-org', 'org-b'],
      });
      expect(within(projectCard).queryByRole('button', { name: 'Org Aを外す' })).toBeNull();
      expect(within(projectCard).getByRole('button', { name: 'Org Bを外す' })).toHaveFocus();
    });
    expect(screen.getByRole('button', { name: 'Project A' })).toHaveAttribute(
      'aria-current',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Project Aを移動' })).not.toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });

  it('replaces unresolved links from the add chip and turns the removed chip back into add', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installProjectOrganizationSettings(['missing-org']);

    renderWorkspaceSettingsSection(japaneseTranslate);
    await screen.findByRole('heading', { name: 'Project A' });
    const addTrigger = screen.getByRole('button', { name: 'Project Aの組織を選択' });
    expect(addTrigger).toHaveClass('workspace-organization-add-trigger');

    addTrigger.focus();
    await user.keyboard('{Enter}');
    const picker = await screen.findByRole('dialog', { name: 'Project Aの組織を選択' });
    const organizationOption = within(picker).getByRole('button', { name: 'Org A' });
    expect(organizationOption).toHaveAttribute('aria-pressed', 'false');

    organizationOption.focus();
    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(workspaceSettings.updateProjectLinks).toHaveBeenNthCalledWith(1, 'project-a', {
        organizationIds: ['org-a'],
      });
      expect(screen.queryByRole('dialog', { name: 'Project Aの組織を選択' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Project Aの組織を選択' })).toBeNull();
    });

    const removeButton = screen.getByRole('button', { name: 'Org Aを外す' });
    expect(removeButton).toHaveFocus();
    await user.keyboard(' ');
    await waitFor(() => {
      expect(workspaceSettings.updateProjectLinks).toHaveBeenNthCalledWith(2, 'project-a', {
        organizationIds: [],
      });
      expect(screen.getByRole('button', { name: 'Project Aの組織を選択' })).toHaveClass(
        'workspace-organization-add-trigger'
      );
    });
  });

  it('ignores a held Enter on the remove chip that selecting an organization focused', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installProjectOrganizationSettings([]);

    renderWorkspaceSettingsSection(japaneseTranslate);
    await screen.findByRole('heading', { name: 'Project A' });
    await user.click(screen.getByRole('button', { name: 'Project Aの組織を選択' }));
    const picker = screen.getByRole('dialog', { name: 'Project Aの組織を選択' });
    within(picker).getByRole('button', { name: 'Org A' }).focus();
    await user.keyboard('{Enter}');
    const removeButton = await screen.findByRole('button', { name: 'Org Aを外す' });
    await waitFor(() => expect(removeButton).toHaveFocus());

    // fireEvent returns false when the handler cancels the key's default activation.
    expect(fireEvent.keyDown(removeButton, { key: 'Enter', repeat: true })).toBe(false);
    expect(workspaceSettings.updateProjectLinks).toHaveBeenCalledTimes(1);

    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(workspaceSettings.updateProjectLinks).toHaveBeenLastCalledWith('project-a', {
        organizationIds: [],
      });
    });
  });

  it('restores the add chip focus when organization selection fails', async () => {
    const user = userEvent.setup();
    installProjectOrganizationSettings([], async () => {
      throw new Error('save failed');
    });

    renderWorkspaceSettingsSection(japaneseTranslate);
    await screen.findByRole('heading', { name: 'Project A' });
    const addTrigger = screen.getByRole('button', { name: 'Project Aの組織を選択' });

    addTrigger.focus();
    await user.keyboard('{Enter}');
    const picker = await screen.findByRole('dialog', { name: 'Project Aの組織を選択' });
    const organizationOption = within(picker).getByRole('button', { name: 'Org A' });
    organizationOption.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Project Aの組織を選択' })).toBeNull();
      expect(addTrigger).toHaveFocus();
    });
  });

  it('creates an organization from the project picker and links it to that project', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installOrganizationPickerSettings(
      [{ organization_id: 'org-a', display_name: 'Northwind' }],
      async ({ displayName }) => ({ organization_id: 'org-new', display_name: displayName })
    );

    renderWorkspaceSettingsSection(japaneseTranslate);
    const picker = await openProjectOrganizationPicker(user);
    expect(within(picker).getByRole('button', { name: 'Northwind' })).toBeInTheDocument();
    await user.click(within(picker).getByRole('button', { name: '組織を追加' }));
    expect(within(picker).getByRole('textbox', { name: '組織名' })).toHaveFocus();
    await user.keyboard('Acme{Enter}');

    await waitFor(() => {
      expect(workspaceSettings.createOrganization).toHaveBeenCalledWith({ displayName: 'Acme' });
      expect(workspaceSettings.updateProjectLinks).toHaveBeenCalledWith('project-a', {
        organizationIds: ['org-new'],
      });
      expect(screen.queryByRole('dialog', { name: 'Project Aの組織を選択' })).toBeNull();
      expect(screen.getByRole('button', { name: 'Acmeを外す' })).toHaveFocus();
    });
  });

  it('returns from the new organization field to the list with Escape', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installOrganizationPickerSettings(
      [{ organization_id: 'org-a', display_name: 'Northwind' }],
      async () => {
        throw new Error('not expected');
      }
    );

    renderWorkspaceSettingsSection(japaneseTranslate);
    const picker = await openProjectOrganizationPicker(user);
    await user.click(within(picker).getByRole('button', { name: '組織を追加' }));
    await user.keyboard('Acme{Escape}');

    expect(screen.getByRole('dialog', { name: 'Project Aの組織を選択' })).toBe(picker);
    expect(within(picker).queryByRole('textbox')).toBeNull();
    expect(within(picker).getByRole('button', { name: 'Northwind' })).toBeInTheDocument();
    expect(within(picker).getByRole('button', { name: '組織を追加' })).toHaveFocus();
    expect(workspaceSettings.createOrganization).not.toHaveBeenCalled();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Project Aの組織を選択' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Project Aの組織を選択' })).toHaveFocus();
  });

  it('offers only adding an organization when none exist', async () => {
    const user = userEvent.setup();
    installOrganizationPickerSettings([], async ({ displayName }) => ({
      organization_id: 'org-new',
      display_name: displayName,
    }));

    renderWorkspaceSettingsSection(japaneseTranslate);
    const picker = await openProjectOrganizationPicker(user);

    const options = within(picker).getAllByRole('button');
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveAccessibleName('組織を追加');
    expect(options[0]).toHaveFocus();
    expect(within(picker).queryByRole('group')).toBeNull();
  });

  it('deletes unused organizations from the project picker without asking or selecting them', async () => {
    const user = userEvent.setup();
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    const workspaceSettings = installOrganizationPickerSettings(
      [
        { organization_id: 'org-a', display_name: 'Acme' },
        { organization_id: 'org-b', display_name: 'Northwind' },
      ],
      async () => {
        throw new Error('not expected');
      }
    );

    renderWorkspaceSettingsSection(japaneseTranslate);
    const picker = await openProjectOrganizationPicker(user);
    await user.click(within(picker).getByRole('button', { name: '削除 Acme' }));

    await waitFor(() => {
      expect(workspaceSettings.deleteOrganization).toHaveBeenCalledWith('org-a');
      expect(within(picker).queryByRole('button', { name: 'Acme' })).toBeNull();
      expect(within(picker).getByRole('button', { name: '削除 Northwind' })).toHaveFocus();
    });
    expect(screen.getByRole('dialog', { name: 'Project Aの組織を選択' })).toBe(picker);

    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(workspaceSettings.deleteOrganization).toHaveBeenCalledWith('org-b');
      expect(within(picker).getByRole('button', { name: '組織を追加' })).toHaveFocus();
    });
    expect(confirm).not.toHaveBeenCalled();
    expect(workspaceSettings.updateProjectLinks).not.toHaveBeenCalled();
  });

  it('asks before deleting an organization a project uses and removes it from that project', async () => {
    const user = userEvent.setup();
    const confirm = vi.fn(() => false);
    vi.stubGlobal('confirm', confirm);
    const workspaceSettings = installProjectOrganizationSettings(['org-a']);

    renderWorkspaceSettingsSection(japaneseTranslate);
    const projectDetail = await findProjectDetail('Project A');
    expect(within(projectDetail).getByRole('button', { name: 'Org Aを外す' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'プロジェクトを追加' }));
    const popover = screen.getByRole('dialog', { name: 'プロジェクトを作成' });

    await user.click(within(popover).getByRole('button', { name: '削除 Org A' }));
    expect(confirm).toHaveBeenCalledWith(
      '『Org A』は 1 件のプロジェクトやフォルダで使われています。削除すると、そこからも外れます。削除しますか？'
    );
    expect(workspaceSettings.deleteOrganization).not.toHaveBeenCalled();
    expect(within(popover).getByRole('button', { name: 'Org A' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(within(projectDetail).getByRole('button', { name: 'Org Aを外す' })).toBeInTheDocument();

    confirm.mockReturnValue(true);
    await user.click(within(popover).getByRole('button', { name: '削除 Org A' }));

    await waitFor(() => {
      expect(workspaceSettings.deleteOrganization).toHaveBeenCalledWith('org-a');
      expect(within(popover).queryByRole('button', { name: 'Org A' })).toBeNull();
      expect(within(projectDetail).queryByRole('button', { name: 'Org Aを外す' })).toBeNull();
      expect(within(popover).getByRole('button', { name: '削除 Org B' })).toHaveFocus();
    });
    expect(workspaceSettings.deleteOrganization).toHaveBeenCalledOnce();
  });

  it('keeps the draft open without linking when organization creation is refused', async () => {
    const user = userEvent.setup();
    const workspaceSettings = installOrganizationPickerSettings([], async () => {
      throw new Error('create failed');
    });

    renderWorkspaceSettingsSection(japaneseTranslate);
    const picker = await openProjectOrganizationPicker(user);
    await user.click(within(picker).getByRole('button', { name: '組織を追加' }));
    const input = within(picker).getByRole('textbox', { name: '組織名' });

    await user.keyboard('   {Enter}');
    expect(workspaceSettings.createOrganization).not.toHaveBeenCalled();

    await user.clear(input);
    await user.keyboard('Acme{Enter}');
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        japaneseTranslate('settings.workspace.saveFailed')
      );
    });
    expect(workspaceSettings.createOrganization).toHaveBeenCalledWith({ displayName: 'Acme' });
    expect(workspaceSettings.updateProjectLinks).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Project Aの組織を選択' })).toBe(picker);
    expect(input).toHaveValue('Acme');
    expect(input).toHaveFocus();
  });

  it('restores the selected remove chip focus when organization removal fails', async () => {
    const user = userEvent.setup();
    installProjectOrganizationSettings(['org-a', 'org-b'], async () => {
      throw new Error('save failed');
    });

    renderWorkspaceSettingsSection(japaneseTranslate);
    await screen.findByRole('heading', { name: 'Project A' });
    const removeButton = screen.getByRole('button', { name: 'Org Bを外す' });

    removeButton.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Org Aを外す' })).toBeInTheDocument();
      expect(removeButton).toHaveFocus();
    });
  });

  it('keeps another project operable while a link save is pending', async () => {
    const user = userEvent.setup();
    const pendingUpdate = createDeferred<TestWorkspaceProject>();
    const settings = {
      read_access_scope: 'workspace' as const,
      organizations: [
        { organization_id: 'org-a', display_name: 'Org A' },
        { organization_id: 'org-b', display_name: 'Org B' },
      ],
      projects: [
        {
          project_id: 'project-a',
          display_name: 'Project A',
          sort_order: 0,
          organization_ids: [] as string[],
        },
        {
          project_id: 'project-b',
          display_name: 'Project B',
          sort_order: 1,
          organization_ids: [] as string[],
        },
      ],
      folders: [],
    };
    const updateProjectLinks = vi.fn<UpdateProjectLinks>(async (projectId, input) => {
      if (projectId === 'project-a') return await pendingUpdate.promise;
      const project = settings.projects.find((candidate) => candidate.project_id === projectId)!;
      project.organization_ids = [...input.organizationIds];
      return { ...project, organization_ids: [...project.organization_ids] };
    });
    const workspaceSettings = {
      get: vi.fn(async () => settings),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks,
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection(japaneseTranslate);
    await screen.findByRole('heading', { name: 'Project A' });
    const projectAAdd = screen.getByRole('button', { name: 'Project Aの組織を選択' });
    await user.click(projectAAdd);
    await user.click(
      within(screen.getByRole('dialog', { name: 'Project Aの組織を選択' })).getByRole('button', {
        name: 'Org A',
      })
    );

    await waitFor(() => {
      expect(workspaceSettings.updateProjectLinks).toHaveBeenCalledWith('project-a', {
        organizationIds: ['org-a'],
      });
      expect(projectAAdd).toHaveAttribute('aria-busy', 'true');
    });

    await user.click(screen.getByRole('button', { name: 'Project B' }));
    const projectBAdd = screen.getByRole('button', { name: 'Project Bの組織を選択' });
    expect(projectBAdd).toBeEnabled();
    await user.click(projectBAdd);
    await user.click(
      within(screen.getByRole('dialog', { name: 'Project Bの組織を選択' })).getByRole('button', {
        name: 'Org B',
      })
    );

    await waitFor(() => {
      expect(workspaceSettings.updateProjectLinks).toHaveBeenCalledWith('project-b', {
        organizationIds: ['org-b'],
      });
      expect(screen.getByRole('button', { name: 'Org Bを外す' })).toHaveFocus();
    });
    await user.click(screen.getByRole('button', { name: 'Project A' }));
    expect(screen.getByRole('button', { name: 'Project Aの組織を選択' })).toHaveAttribute(
      'aria-busy',
      'true'
    );

    pendingUpdate.resolve({
      project_id: 'project-a',
      display_name: 'Project A',
      organization_ids: ['org-a'],
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Org Aを外す' })).toBeInTheDocument();
    });
  });

  it('shows every project in persisted order with an enabled drag handle', async () => {
    window.electron = {
      workspaceSettings: {
        get: vi.fn(async () => ({
          read_access_scope: 'workspace',
          organizations: [{ organization_id: 'org-a', display_name: 'Org A' }],
          projects: [
            {
              project_id: 'project-z',
              display_name: 'Zulu',
              sort_order: 0,
              organization_ids: [],
            },
            {
              project_id: 'project-a',
              display_name: 'Alpha',
              sort_order: 1,
              organization_ids: ['org-a'],
            },
          ],
          folders: [],
        })),
        createOrganization: vi.fn(),
        createProject: vi.fn(),
        createFolder: vi.fn(),
        deleteOrganization: vi.fn(),
        deleteProject: vi.fn(),
        deleteFolder: vi.fn(),
        updateProjectLinks: vi.fn(),
        updateFolderLinks: vi.fn(),
        updateReadAccessScope: vi.fn(),
        selectFolder: vi.fn(),
      },
    } as unknown as Window['electron'];

    const { container } = renderWorkspaceSettingsSection();
    await screen.findByRole('heading', { name: 'Zulu' });

    expect(
      Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
        (card) => card.dataset.projectId
      )
    ).toEqual(['project-z', 'project-a']);
    expect(screen.getAllByRole('button', { name: 'settings.workspace.drag.handle' })).toHaveLength(
      2
    );
    for (const handle of screen.getAllByRole('button', {
      name: 'settings.workspace.drag.handle',
    })) {
      expect(handle).toBeEnabled();
    }
  });

  it('creates a project from an optional single organization selection', async () => {
    const settings = {
      read_access_scope: 'workspace' as const,
      organizations: [
        { organization_id: 'org-a', display_name: 'Org A' },
        { organization_id: 'org-b', display_name: 'Org B' },
      ],
      projects: [],
      folders: [],
    };
    const workspaceSettings = {
      get: vi.fn(async () => settings),
      createOrganization: vi.fn(),
      createProject: vi.fn(async () => ({
        project_id: 'project-new',
        display_name: 'New Project',
        organization_ids: ['org-b'],
      })),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    const trigger = await screen.findByRole('button', { name: 'settings.workspace.addProject' });
    fireEvent.click(trigger);
    const popover = screen.getByRole('dialog', {
      name: 'settings.workspace.projectCreate.title',
    });
    const orgA = await within(popover).findByRole('button', { name: 'Org A' });
    const orgB = within(popover).getByRole('button', { name: 'Org B' });
    expect(orgA).toHaveAttribute('aria-pressed', 'false');
    expect(orgB).toHaveAttribute('aria-pressed', 'false');

    fireEvent.change(
      within(popover).getByRole('textbox', { name: 'settings.workspace.projectPlaceholder' }),
      { target: { value: 'New Project' } }
    );
    fireEvent.click(orgB);
    expect(orgA).toHaveAttribute('aria-pressed', 'false');
    expect(orgB).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(within(popover).getByRole('button', { name: 'settings.workspace.addProject' }));

    await waitFor(() => {
      expect(workspaceSettings.createProject).toHaveBeenCalledWith({
        displayName: 'New Project',
        organizationIds: ['org-b'],
      });
      expect(
        screen.queryByRole('dialog', { name: 'settings.workspace.projectCreate.title' })
      ).toBeNull();
      expect(screen.getByRole('button', { name: 'settings.workspace.selectFolder' })).toHaveFocus();
    });
    expect(trigger).not.toHaveFocus();
  });

  it('closes the project popover with Escape and restores trigger focus', async () => {
    window.electron = {
      workspaceSettings: {
        get: vi.fn(async () => emptySettings),
        createOrganization: vi.fn(),
        createProject: vi.fn(),
        createFolder: vi.fn(),
        deleteOrganization: vi.fn(),
        deleteProject: vi.fn(),
        deleteFolder: vi.fn(),
        updateProjectLinks: vi.fn(),
        updateFolderLinks: vi.fn(),
        updateReadAccessScope: vi.fn(),
        selectFolder: vi.fn(),
      },
    } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    await screen.findByText('settings.workspace.gettingStarted');
    const trigger = screen.getByRole('button', { name: 'settings.workspace.addProject' });
    fireEvent.click(trigger);
    expect(
      screen.getByRole('dialog', { name: 'settings.workspace.projectCreate.title' })
    ).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => {
      expect(
        screen.queryByRole('dialog', { name: 'settings.workspace.projectCreate.title' })
      ).toBeNull();
      expect(trigger).toHaveFocus();
    });
  });

  it('moves focus into the organization dialog opened from the project create popover', async () => {
    const user = userEvent.setup();
    window.electron = {
      workspaceSettings: {
        get: vi.fn(async () => emptySettings),
        createOrganization: vi.fn(),
        createProject: vi.fn(),
        createFolder: vi.fn(),
        deleteOrganization: vi.fn(),
        deleteProject: vi.fn(),
        deleteFolder: vi.fn(),
        updateProjectLinks: vi.fn(),
        updateFolderLinks: vi.fn(),
        updateReadAccessScope: vi.fn(),
        selectFolder: vi.fn(),
      },
    } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    await screen.findByText('settings.workspace.gettingStarted');
    const projectTrigger = screen.getByRole('button', {
      name: 'settings.workspace.addProject',
    });
    await user.click(projectTrigger);
    const popover = screen.getByRole('dialog', {
      name: 'settings.workspace.projectCreate.title',
    });

    await user.click(
      within(popover).getByRole('button', {
        name: 'settings.workspace.organizationManager.open',
      })
    );

    const organizationDialog = screen.getByRole('dialog', {
      name: 'settings.workspace.organizationManager.title',
    });
    await waitFor(() => {
      expect(
        within(organizationDialog).getByLabelText('settings.workspace.organizationPlaceholder')
      ).toHaveFocus();
      expect(projectTrigger).not.toHaveFocus();
    });
    expect(
      screen.queryByRole('dialog', { name: 'settings.workspace.projectCreate.title' })
    ).toBeNull();
  });

  it('restores the project draft and selects the organization created while managing them', async () => {
    const user = userEvent.setup();
    const settings = {
      read_access_scope: 'workspace' as const,
      organizations: [] as WorkspaceOrganization[],
      projects: [],
      folders: [],
    };
    const workspaceSettings = {
      get: vi.fn(async () => ({
        ...settings,
        organizations: settings.organizations.map((organization) => ({ ...organization })),
      })),
      createOrganization: vi.fn(async () => ({
        organization_id: 'org-new',
        display_name: 'New Org',
      })),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    await screen.findByText('settings.workspace.gettingStarted');
    await user.click(screen.getByRole('button', { name: 'settings.workspace.addProject' }));
    const popover = screen.getByRole('dialog', {
      name: 'settings.workspace.projectCreate.title',
    });
    await user.type(
      within(popover).getByRole('textbox', { name: 'settings.workspace.projectPlaceholder' }),
      'Draft Project'
    );
    await user.click(
      within(popover).getByRole('button', {
        name: 'settings.workspace.organizationManager.open',
      })
    );

    const organizationDialog = screen.getByRole('dialog', {
      name: 'settings.workspace.organizationManager.title',
    });
    await user.type(
      within(organizationDialog).getByLabelText('settings.workspace.organizationPlaceholder'),
      'New Org'
    );
    await user.click(
      within(organizationDialog).getByRole('button', {
        name: 'settings.workspace.addOrganization',
      })
    );
    await waitFor(() => {
      expect(workspaceSettings.createOrganization).toHaveBeenCalledWith({ displayName: 'New Org' });
    });
    await user.click(within(organizationDialog).getByRole('button', { name: 'common.close' }));

    const restoredPopover = await screen.findByRole('dialog', {
      name: 'settings.workspace.projectCreate.title',
    });
    expect(
      within(restoredPopover).getByRole('textbox', {
        name: 'settings.workspace.projectPlaceholder',
      })
    ).toHaveValue('Draft Project');
    expect(within(restoredPopover).getByRole('button', { name: 'New Org' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });

  it('keeps focus on the popover submit control when project creation fails', async () => {
    const user = userEvent.setup();
    const workspaceSettings = {
      get: vi.fn(async () => emptySettings),
      createOrganization: vi.fn(),
      createProject: vi.fn(async () => {
        throw new Error('create project failed');
      }),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    await screen.findByText('settings.workspace.gettingStarted');
    const trigger = screen.getByRole('button', { name: 'settings.workspace.addProject' });
    await user.click(trigger);
    const popover = screen.getByRole('dialog', {
      name: 'settings.workspace.projectCreate.title',
    });
    await user.type(
      within(popover).getByRole('textbox', { name: 'settings.workspace.projectPlaceholder' }),
      'New Project'
    );
    const submit = within(popover).getByRole('button', { name: 'settings.workspace.addProject' });
    await user.click(submit);

    expect(await screen.findByRole('alert')).toHaveTextContent('settings.workspace.saveFailed');
    expect(
      screen.getByRole('dialog', { name: 'settings.workspace.projectCreate.title' })
    ).toBeInTheDocument();
    expect(submit).toHaveFocus();
    expect(trigger).not.toHaveFocus();
  });

  it('moves a project folder to unassigned locally when its project is deleted', async () => {
    const user = userEvent.setup();
    const workspaceSettings = {
      get: vi.fn(async () => ({
        read_access_scope: 'workspace' as const,
        organizations: [],
        projects: [
          {
            project_id: 'project-a',
            display_name: 'Project A',
            sort_order: 0,
            organization_ids: [],
          },
        ],
        folders: [
          {
            folder_id: 'folder-a',
            display_name: 'Folder A',
            real_path: '/Users/example/folder-a',
            canonical_real_path: '/Users/example/folder-a',
            organization_ids: [],
            project_ids: ['project-a'],
          },
        ],
      })),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(async () => undefined),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    await user.click(await screen.findByRole('button', { name: 'common.delete Project A' }));

    // With no project left, the folders it held are what the detail column shows.
    expect(
      await screen.findByRole('button', { name: 'settings.workspace.unassigned.title' })
    ).toHaveAttribute('aria-current', 'true');
    expect(screen.getByText('Folder A')).toBeInTheDocument();
    expect(workspaceSettings.deleteProject).toHaveBeenCalledOnce();
    expect(workspaceSettings.get).toHaveBeenCalledOnce();
    expect(workspaceSettings.createFolder).not.toHaveBeenCalled();
    expect(workspaceSettings.deleteFolder).not.toHaveBeenCalled();
    expect(workspaceSettings.updateFolderLinks).not.toHaveBeenCalled();
  });

  it('shows and assigns only folders without a valid project link', async () => {
    const settings = {
      read_access_scope: 'workspace' as const,
      organizations: [{ organization_id: 'org-a', display_name: 'Org A' }],
      projects: [{ project_id: 'project-a', display_name: 'Project A', organization_ids: [] }],
      folders: [
        {
          folder_id: 'folder-unassigned',
          display_name: 'Unassigned Folder',
          real_path: '/Users/example/unassigned',
          canonical_real_path: '/Users/example/unassigned',
          organization_ids: ['org-a'],
          project_ids: ['missing-project'],
        },
        {
          folder_id: 'folder-assigned',
          display_name: 'Assigned Folder',
          real_path: '/Users/example/assigned',
          canonical_real_path: '/Users/example/assigned',
          organization_ids: [],
          project_ids: ['project-a'],
        },
      ],
    };
    const workspaceSettings = {
      get: vi.fn(async () => settings),
      createOrganization: vi.fn(),
      createProject: vi.fn(),
      createFolder: vi.fn(),
      deleteOrganization: vi.fn(),
      deleteProject: vi.fn(),
      deleteFolder: vi.fn(),
      updateProjectLinks: vi.fn(),
      updateFolderLinks: vi.fn(async (folderId: string) => ({
        ...settings.folders.find((folder) => folder.folder_id === folderId)!,
        organization_ids: [],
        project_ids: ['project-a'],
      })),
      updateReadAccessScope: vi.fn(),
      selectFolder: vi.fn(),
    };
    window.electron = { workspaceSettings } as unknown as Window['electron'];

    renderWorkspaceSettingsSection();
    const unassignedItem = await screen.findByRole('button', {
      name: 'settings.workspace.unassigned.title',
    });
    expect(unassignedItem).not.toHaveAttribute('aria-current');
    expect(screen.queryByText('Unassigned Folder')).toBeNull();
    fireEvent.click(unassignedItem);
    expect(unassignedItem).toHaveAttribute('aria-current', 'true');

    const unassignedSection = screen
      .getByRole('heading', { name: 'settings.workspace.unassigned.title' })
      .closest('.workspace-unassigned-folders');
    if (!(unassignedSection instanceof HTMLElement)) {
      throw new Error('Unassigned section was not rendered.');
    }
    expect(within(unassignedSection).getByText('Unassigned Folder')).toBeInTheDocument();
    expect(within(unassignedSection).queryByText('Assigned Folder')).toBeNull();
    expect(within(unassignedSection).getByText('Org A')).toBeInTheDocument();
    fireEvent.change(
      within(unassignedSection).getByRole('combobox', {
        name: 'settings.workspace.unassigned.project Unassigned Folder',
      }),
      { target: { value: 'project-a' } }
    );
    fireEvent.click(
      within(unassignedSection).getByRole('button', {
        name: 'settings.workspace.unassigned.assign',
      })
    );

    await waitFor(() => {
      expect(workspaceSettings.updateFolderLinks).toHaveBeenCalledWith('folder-unassigned', {
        organizationIds: [],
        projectIds: ['project-a'],
      });
    });
    // With nothing left unassigned, the item goes and the project takes the detail column.
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'settings.workspace.unassigned.title' })
      ).toBeNull();
    });
    expect(screen.getByRole('heading', { name: 'Project A' })).toBeInTheDocument();
  });
});
