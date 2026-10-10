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
    reorderProjects: vi.fn(async ({ projectIds }: { projectIds: string[] }) => ({
      project_ids: projectIds,
    })),
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
  await screen.findByRole('button', { name: 'a' });
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
  expect(screen.getByRole('button', { name: 'a' })).toHaveFocus();
  expect(api.renameProject).not.toHaveBeenCalled();

  await chooseFromMenu('a', 'history.projects.rename');
  await userEvent.clear(screen.getByRole('textbox', { name: 'history.projects.nameLabel' }));
  await userEvent.keyboard('Aurora{Enter}');
  expect(api.renameProject).toHaveBeenCalledOnce();
  expect(api.renameProject).toHaveBeenCalledWith('a', { displayName: 'Aurora' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Aurora' })).toHaveFocus());
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
  await userEvent.click(screen.getByRole('button', { name: 'b' }));
  const folders = within(screen.getByRole('list', { name: 'history.projects.folders' }));
  expect(folders.getAllByRole('listitem').map((row) => row.textContent)).toEqual([
    '/Users/me/own',
    '/Users/me/shared',
  ]);
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

it('フォルダを外すと、ほかのプロジェクトにもあるものはリンクだけ、このプロジェクトだけのものは登録ごと外す', async () => {
  await renderProjects();
  await userEvent.click(screen.getByRole('button', { name: 'a' }));

  await userEvent.click(
    screen.getByRole('button', { name: 'history.projects.removeFolder {"name":"shared"}' })
  );
  expect(api.updateFolderLinks).toHaveBeenCalledWith('shared', {
    organizationIds: [],
    projectIds: ['b'],
  });
  expect(api.deleteFolder).not.toHaveBeenCalled();

  await userEvent.click(
    screen.getByRole('button', { name: 'history.projects.removeFolder {"name":"own"}' })
  );
  expect(api.deleteFolder).toHaveBeenCalledWith('own');
  await waitFor(() => expect(screen.getByRole('button', { name: 'a' })).toHaveFocus());
});

it('フォルダの追加を待っている間は、そのフォルダを外せず、終わった後の内容で外す', async () => {
  let finishLink!: () => void;
  api.updateFolderLinks.mockImplementationOnce(
    async (folderId: string, links: { projectIds: string[] }) => {
      await new Promise<void>((resolve) => {
        finishLink = resolve;
      });
      const updated = store.folders.find((candidate) => candidate.folder_id === folderId)!;
      updated.project_ids = [...links.projectIds].sort();
      return structuredClone(updated);
    }
  );
  api.selectFolder.mockResolvedValue({ canceled: false, path: '/Users/me/own' });
  await renderProjects();
  await userEvent.click(screen.getByRole('button', { name: 'a' }));

  await chooseFromMenu('b', 'history.projects.addFolder');
  const remove = screen.getByRole('button', {
    name: 'history.projects.removeFolder {"name":"own"}',
  });
  await waitFor(() => expect(remove).toBeDisabled());
  await userEvent.click(remove);
  expect(api.deleteFolder).not.toHaveBeenCalled();

  await act(async () => finishLink());
  await waitFor(() => expect(remove).toBeEnabled());
  await userEvent.click(remove);
  expect(api.deleteFolder).not.toHaveBeenCalled();
  expect(api.updateFolderLinks).toHaveBeenLastCalledWith('own', {
    organizationIds: [],
    projectIds: ['b'],
  });
});

it('フォルダ選択の間に外したリンクは、別のプロジェクトへの追加で戻らない', async () => {
  store.projects.push(project('c', 2));
  store.folders = [folder('shared', ['a', 'c'])];
  installApi();
  let choose!: (answer: { canceled: boolean; path: string }) => void;
  api.selectFolder.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        choose = resolve;
      })
  );
  await renderProjects();

  await chooseFromMenu('b', 'history.projects.addFolder');
  await userEvent.click(screen.getByRole('button', { name: 'a' }));
  await userEvent.click(
    screen.getByRole('button', { name: 'history.projects.removeFolder {"name":"shared"}' })
  );
  await waitFor(() => expect(store.folders[0].project_ids).toEqual(['c']));
  await act(async () => choose({ canceled: false, path: '/Users/me/shared' }));

  await waitFor(() =>
    expect(api.updateFolderLinks).toHaveBeenLastCalledWith('shared', {
      organizationIds: [],
      projectIds: ['c', 'b'],
    })
  );
  expect(store.folders[0].project_ids).toEqual(['b', 'c']);
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

it('キーボードで持ち上げて動かすと、並べ替えた順番を保存する', async () => {
  const { container } = render(
    <LocalOwnerContext.Provider value={{ kind: 'account', id: 'user-1' }}>
      <Projects />
    </LocalOwnerContext.Provider>
  );
  await screen.findByRole('button', { name: 'a' });
  const rows = () => Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
  const [first, second] = rows();
  vi.spyOn(first, 'getBoundingClientRect').mockReturnValue(rectAt(0));
  vi.spyOn(second, 'getBoundingClientRect').mockReturnValue(rectAt(40));
  const handle = screen.getByRole('button', { name: /settings\.workspace\.drag\.handle.*"a"/u });
  handle.focus();

  fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
  await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
  fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
  await waitFor(() => expect(rows().map((row) => row.dataset.projectId)).toEqual(['b', 'a']));
  await act(async () => {
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });
  });

  await waitFor(() => expect(api.reorderProjects).toHaveBeenCalledWith({ projectIds: ['b', 'a'] }));
});

function rectAt(top: number): DOMRect {
  return {
    bottom: top + 32,
    height: 32,
    left: 0,
    right: 240,
    top,
    width: 240,
    x: 0,
    y: top,
    toJSON: () => ({}),
  };
}
