import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { LocalOwnerContext } from '@/context/localOwnerContext';
import { clearWorkspaceSettingsCache } from '@/pages/settings/components/workspaceSettingsCache';
import type { WorkspaceSettings } from '@/pages/settings/components/workspaceSettingsModel';
import { useWorkspaceSettingsController } from '@/pages/settings/useWorkspaceSettingsController';
import { HistoryProjects } from './HistoryProjects';

const t = (key: string, vars?: Record<string, string | number>) =>
  vars ? `${key} ${JSON.stringify(vars)}` : key;

function folder(folderId: string, projectIds: string[]) {
  return {
    folder_id: folderId,
    display_name: folderId,
    real_path: `/Users/me/${folderId}`,
    canonical_real_path: `/Users/me/${folderId}`,
    organization_ids: [] as string[],
    project_ids: projectIds,
  };
}

function project(projectId: string, sortOrder: number, organizationIds: string[] = []) {
  return {
    project_id: projectId,
    display_name: projectId,
    sort_order: sortOrder,
    organization_ids: organizationIds,
  };
}

/** The local backend's workspace, answering as workspace_settings.py does. */
let store: WorkspaceSettings;
let api: ReturnType<typeof createApi>;

function createApi() {
  return {
    get: vi.fn(async () => structuredClone(store)),
    createOrganization: vi.fn(async ({ displayName }: { displayName: string }) => {
      const created = { organization_id: `org-${displayName}`, display_name: displayName };
      store.organizations.push(created);
      return created;
    }),
    renameProject: vi.fn(async (projectId: string, { displayName }: { displayName: string }) => {
      const renamed = store.projects.find((p) => p.project_id === projectId)!;
      renamed.display_name = displayName;
      return { ...renamed };
    }),
    createFolder: vi.fn(),
    deleteProject: vi.fn(async () => undefined),
    deleteFolder: vi.fn(async (folderId: string) => {
      store.folders = store.folders.filter((f) => f.folder_id !== folderId);
    }),
    updateProjectLinks: vi.fn(
      async (projectId: string, { organizationIds }: { organizationIds: string[] }) => {
        const updated = store.projects.find((p) => p.project_id === projectId)!;
        updated.organization_ids = organizationIds;
        return { ...updated };
      }
    ),
    updateFolderLinks: vi.fn(async (folderId: string, links: { projectIds: string[] }) => {
      const updated = store.folders.find((f) => f.folder_id === folderId)!;
      updated.project_ids = [...links.projectIds].sort();
      return structuredClone(updated);
    }),
    selectFolder: vi.fn(),
    openFolder: vi.fn(async () => undefined),
  };
}

function installApi() {
  api = createApi();
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: { workspaceSettings: api },
  });
}

function Projects() {
  return <HistoryProjects projects={useWorkspaceSettingsController(t)} t={t} />;
}

async function renderProjects() {
  render(
    <LocalOwnerContext.Provider value={{ kind: 'account', id: 'user-1' }}>
      <Projects />
    </LocalOwnerContext.Provider>
  );
  await screen.findByText('a');
}

async function chooseFromMenu(projectName: string, item: string) {
  await userEvent.click(
    screen.getByRole('button', { name: `history.projects.menu {"name":"${projectName}"}` })
  );
  await userEvent.click(screen.getByRole('menuitem', { name: item }));
}

const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    },
  });
  store = {
    read_access_scope: 'workspace',
    organizations: [{ organization_id: 'org-x', display_name: 'Xer' }],
    projects: [project('a', 0, ['org-x']), project('b', 1)],
    folders: [folder('own', ['a']), folder('shared', ['a', 'b'])],
  };
  installApi();
});

afterEach(() => {
  cleanup();
  clearWorkspaceSettingsCache();
  delete window.electron;
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  }
});

it('名前の変更は Enter で保存し、Esc で取り消し、ほかのプロジェクトと同じ名前は送らずに知らせる', async () => {
  await renderProjects();

  await chooseFromMenu('a', 'history.projects.rename');
  const field = screen.getByRole('textbox', { name: 'history.projects.nameLabel' });
  expect(field).toHaveFocus();
  await userEvent.clear(field);
  await userEvent.type(field, 'b{Enter}');
  expect(api.renameProject).not.toHaveBeenCalled();
  expect(screen.getByRole('alert')).toHaveTextContent('history.projects.nameTaken {"name":"b"}');

  await userEvent.keyboard('{Escape}');
  expect(screen.getByRole('button', { name: 'history.projects.menu {"name":"a"}' })).toHaveFocus();
  expect(api.renameProject).not.toHaveBeenCalled();

  await chooseFromMenu('a', 'history.projects.rename');
  await userEvent.clear(screen.getByRole('textbox', { name: 'history.projects.nameLabel' }));
  await userEvent.keyboard('Aurora{Enter}');
  expect(api.renameProject).toHaveBeenCalledOnce();
  expect(api.renameProject).toHaveBeenCalledWith('a', { displayName: 'Aurora' });
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: 'history.projects.menu {"name":"Aurora"}' })
    ).toHaveFocus()
  );
});

it('IME で変換中の Enter では保存せず、確定後の Enter で保存する', async () => {
  await renderProjects();

  await chooseFromMenu('a', 'history.projects.rename');
  const field = screen.getByRole('textbox', { name: 'history.projects.nameLabel' });
  await userEvent.clear(field);
  await userEvent.type(field, 'あおば');
  fireEvent.keyDown(field, { key: 'Enter', isComposing: true });
  fireEvent.keyDown(field, { key: 'Enter', keyCode: 229 });
  expect(api.renameProject).not.toHaveBeenCalled();

  fireEvent.keyDown(field, { key: 'Enter' });
  await waitFor(() =>
    expect(api.renameProject).toHaveBeenCalledWith('a', { displayName: 'あおば' })
  );
});

it('前の名前の保存が遅れて終わっても、いま変更中の別のプロジェクトの欄は閉じない', async () => {
  let finishA!: () => void;
  api.renameProject.mockImplementationOnce(
    async (projectId: string, { displayName }: { displayName: string }) => {
      await new Promise<void>((resolve) => {
        finishA = resolve;
      });
      const renamed = store.projects.find((item) => item.project_id === projectId)!;
      renamed.display_name = displayName;
      return { ...renamed };
    }
  );
  await renderProjects();

  await chooseFromMenu('a', 'history.projects.rename');
  await userEvent.clear(screen.getByRole('textbox', { name: 'history.projects.nameLabel' }));
  await userEvent.keyboard('Alpha{Enter}');
  await chooseFromMenu('b', 'history.projects.rename');
  const fieldB = screen.getByRole('textbox', { name: 'history.projects.nameLabel' });
  expect(fieldB).toHaveValue('b');

  await act(async () => finishA());

  await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument());
  expect(screen.getByRole('textbox', { name: 'history.projects.nameLabel' })).toBe(fieldB);
  expect(fieldB).toHaveFocus();
});

it('フォルダの追加は選んだフォルダをこのプロジェクトに加え、登録済みならほかのリンクを残す', async () => {
  api.selectFolder.mockResolvedValue({ canceled: false, path: '/Users/me/own' });
  await renderProjects();

  await chooseFromMenu('b', 'history.projects.addFolder');

  expect(api.createFolder).not.toHaveBeenCalled();
  expect(api.updateFolderLinks).toHaveBeenCalledWith('own', {
    organizationIds: [],
    projectIds: ['a', 'b'],
  });
  await waitFor(() =>
    expect(screen.getByText('b')).toHaveAttribute('title', '/Users/me/own\n/Users/me/shared')
  );
});

it('組織はなしを含めて一つを選んで保存し、新しい組織も名前で足せる', async () => {
  await renderProjects();

  await chooseFromMenu('a', 'history.projects.setOrganization');
  const dialog = screen.getByRole('dialog', {
    name: 'history.projects.organizationTitle {"name":"a"}',
  });
  expect(within(dialog).getByRole('radio', { name: 'Xer' })).toBeChecked();
  await userEvent.click(
    within(dialog).getByRole('radio', { name: 'history.projects.organizationNone' })
  );
  await userEvent.click(within(dialog).getByRole('button', { name: 'common.save' }));
  expect(api.updateProjectLinks).toHaveBeenLastCalledWith('a', { organizationIds: [] });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('button', { name: 'history.projects.menu {"name":"a"}' })).toHaveFocus();

  await chooseFromMenu('b', 'history.projects.setOrganization');
  const second = screen.getByRole('dialog');
  await userEvent.type(
    within(second).getByRole('textbox', { name: 'history.projects.organizationNew' }),
    'Wakakusa{Enter}'
  );
  expect(api.createOrganization).toHaveBeenCalledWith({ displayName: 'Wakakusa' });
  expect(await within(second).findByRole('radio', { name: 'Wakakusa' })).toBeChecked();
  await userEvent.click(within(second).getByRole('button', { name: 'common.save' }));
  expect(api.updateProjectLinks).toHaveBeenLastCalledWith('b', {
    organizationIds: ['org-Wakakusa'],
  });
});

it('Finder で開くは最初のフォルダを開き、フォルダのないプロジェクトでは押せない', async () => {
  store.folders = [folder('own', ['a'])];
  installApi();
  await renderProjects();

  await chooseFromMenu('a', 'history.projects.openInFinder');
  expect(api.openFolder).toHaveBeenCalledWith('own');

  await userEvent.click(screen.getByRole('button', { name: 'history.projects.menu {"name":"b"}' }));
  expect(screen.getByRole('menuitem', { name: 'history.projects.openInFinder' })).toBeDisabled();
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('menu')).toBeNull();
  expect(screen.getByRole('button', { name: 'history.projects.menu {"name":"b"}' })).toHaveFocus();
});

it('プロジェクトは確認してから削除し、キャンセルならメニューのボタンへ戻る', async () => {
  await renderProjects();

  await chooseFromMenu('b', 'common.delete');
  expect(
    screen.getByRole('dialog', { name: 'history.projects.deleteConfirmTitle {"name":"b"}' })
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'common.cancel' }));
  expect(screen.getByRole('button', { name: 'history.projects.menu {"name":"b"}' })).toHaveFocus();
  expect(api.deleteProject).not.toHaveBeenCalled();

  await chooseFromMenu('b', 'common.delete');
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  await waitFor(() => expect(api.deleteProject).toHaveBeenCalledWith('b'));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'history.projects.add' })).toHaveFocus()
  );
});
