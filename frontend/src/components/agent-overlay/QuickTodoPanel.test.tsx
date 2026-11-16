import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type { WorkspaceSettings } from '../../../electron/src/settings/workspaceSettingsFetch';
import type {
  TodoCreateRequest,
  TodoView,
  TodoWriteResult,
} from '../../../electron/src/todos/todoContracts';
import AgentOverlay from '../AgentOverlay';

const SETTINGS: WorkspaceSettings = {
  read_access_scope: 'workspace',
  organizations: [
    { organization_id: 'xer', display_name: 'Xer', icon_data_url: null },
    { organization_id: 'wakakusa', display_name: 'Wakakusa', icon_data_url: null },
  ],
  projects: [
    { project_id: 'sales', display_name: 'Sales deck', sort_order: 1, organization_ids: ['xer'] },
    { project_id: 'pantaray', display_name: 'Pantaray', sort_order: 0, organization_ids: ['xer'] },
    { project_id: 'site', display_name: 'Site', sort_order: 2, organization_ids: ['wakakusa'] },
  ],
  folders: [],
};

// prettier-ignore
const todo = (state: TodoView['state']): TodoView => ({
  todo_id: 'todo-1', project_id: null, organization_id: null, text: 'Draft the agenda', author: 'user',
  origin: 'written', starts_on: null, due: null, state, deletion_request: null, conversation: { kind: 'none' },
});
const STARTING = { kind: 'ok', result: todo({ kind: 'not_started', reason: 'starting' }) } as const;

const create = vi.fn<(request: TodoCreateRequest) => Promise<TodoWriteResult<TodoView>>>();
const openTask = vi.fn();
const hide = vi.fn();

beforeEach(() => {
  create.mockReset().mockResolvedValue(STARTING);
  openTask.mockReset().mockResolvedValue(undefined);
  hide.mockReset();
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      ipcRenderer: { on: () => () => {} },
      workspaceSettings: { get: async () => SETTINGS },
      todos: { create },
      agentOverlay: { onSnapshot: () => () => {}, resize: vi.fn(), hide, openTask },
      orchestration: { onEvent: () => () => {}, onStatus: () => () => {} },
    },
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

async function renderPanel() {
  render(
    <UiLanguageProvider initialLanguage="en">
      <AgentOverlay entryMode="standalone" />
    </UiLanguageProvider>
  );
  await act(async () => undefined);
  return screen.getByRole('textbox', { name: 'Message' });
}

// happy-dom has no Popover API: the choices are in the document while their menu is closed.
const pick = (menu: string, choice: string) =>
  fireEvent.click(
    within(screen.getByRole('menu', { name: menu })).getByRole('menuitemradio', { name: choice })
  );

it('adds the TODO in the project picked, one line, and opens a TODO starting today in the main window', async () => {
  const field = await renderPanel();
  expect(field).toHaveFocus();
  expect(screen.queryByRole('button', { name: /permissions/ })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Add files' })).toBeNull();
  pick('Organization', 'Xer');
  pick('Project', 'Sales deck');

  fireEvent.change(field, { target: { value: 'Draft the agenda\n  from the last notes ' } });
  fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
  fireEvent.keyDown(field, { key: 'Enter', isComposing: true });
  expect(create).not.toHaveBeenCalled();
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));

  expect(create).toHaveBeenCalledExactlyOnceWith({
    client_todo_id: expect.any(String),
    project_id: 'sales',
    organization_id: null,
    text: 'Draft the agenda from the last notes',
    due: null,
  });
  expect(openTask).toHaveBeenCalledExactlyOnceWith({ kind: 'todo', id: 'todo-1' });
  // Main closes the panel once the main window shows the TODO.
  expect(hide).not.toHaveBeenCalled();
});

it('puts a TODO with no project in the organization, closes when it starts later, and starts there next time', async () => {
  create.mockResolvedValue({
    kind: 'ok',
    result: todo({ kind: 'not_started', reason: 'scheduled' }),
  });
  const field = await renderPanel();
  pick('Organization', 'Wakakusa');
  pick('Project', 'Site');
  // Another organization keeps no project of the one before.
  pick('Organization', 'Xer');
  expect(screen.getByRole('button', { name: 'Project: No project' })).toBeInTheDocument();
  fireEvent.change(field, { target: { value: 'Book the room' } });
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Send' })));

  expect(create.mock.lastCall?.[0]).toMatchObject({ project_id: null, organization_id: 'xer' });
  expect(hide).toHaveBeenCalledOnce();
  expect(openTask).not.toHaveBeenCalled();

  cleanup();
  await renderPanel();
  expect(screen.getByRole('button', { name: 'Organization: Xer' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Project: No project' })).toBeInTheDocument();
});

it('resends an unanswered TODO under the same id, keeps a refused one, and says when the main window does not open', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  openTask.mockRejectedValueOnce(new Error('The main window could not be opened.'));
  create.mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({
    kind: 'refused',
    code: 'PROJECT_NOT_FOUND',
  });
  const field = await renderPanel();
  fireEvent.change(field, { target: { value: 'Send the estimate' } });
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));
  expect(screen.getByRole('alert')).toHaveTextContent("That didn't work. Please try again.");
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));
  expect(screen.getByRole('alert')).toHaveTextContent('This project no longer exists.');
  expect(field).toHaveValue('Send the estimate');

  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));
  const [first, second, third] = create.mock.calls.map(([request]) => request.client_todo_id);
  expect(second).toBe(first);
  // A refused TODO was not added, so the next send is a new one.
  expect(third).not.toBe(first);
  expect(field).toHaveValue('');
  expect(screen.getByRole('alert')).toHaveTextContent(
    'The TODO was added, but the main window could not be opened.'
  );
});
