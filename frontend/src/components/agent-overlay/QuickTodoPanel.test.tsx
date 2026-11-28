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
  origin: { kind: 'written' }, starts_on: null, due: null, state, deletion_request: null, conversation: { kind: 'none' },
});
const STARTING = { kind: 'ok', result: todo({ kind: 'not_started', reason: 'starting' }) } as const;

const create = vi.fn<(request: TodoCreateRequest) => Promise<TodoWriteResult<TodoView>>>();
const openTask = vi.fn();
const hide = vi.fn();
// Each document staged gets the next id, as Electron main gives each a fresh one.
const FILE_IDS = [
  '0f8fad5b-d9cb-469f-a165-70867728950e',
  '7c9e6679-7425-40de-944b-e07fc1f90ae7',
] as const;
const IMAGE_PATH = 'user-1/2026-11-17/3f2b9a8e-1c4d-4e5f-8a6b-7c8d9e0f1a2b.png';
const attachFile = vi.fn();
const attachImage = vi.fn();
const discardAttachment = vi.fn();

beforeEach(() => {
  create.mockReset().mockResolvedValue(STARTING);
  openTask.mockReset().mockResolvedValue(undefined);
  hide.mockReset();
  let staged = 0;
  attachFile
    .mockReset()
    .mockImplementation(async ({ bytes, name }: { bytes: ArrayBuffer; name: string }) => ({
      attachmentId: FILE_IDS[staged++],
      name,
      byteSize: bytes.byteLength,
    }));
  attachImage.mockReset().mockResolvedValue({ kind: 'stored', storagePath: IMAGE_PATH });
  discardAttachment.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      ipcRenderer: { on: () => () => {} },
      workspaceSettings: { get: async () => SETTINGS },
      todos: { create },
      actions: { attachFile, attachImage, discardAttachment },
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
  pick('Organization', 'Xer');
  pick('Project', 'Sales deck');

  fireEvent.change(field, { target: { value: 'Draft the agenda\n  from the last notes ' } });
  // Pasted lines are joined as they land, and Shift+Enter adds no line.
  expect(field).toHaveValue('Draft the agenda from the last notes ');
  expect(fireEvent.keyDown(field, { key: 'Enter', shiftKey: true })).toBe(false);
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
  expect(screen.getByRole('button', { name: 'Project: Other' })).toBeInTheDocument();
  fireEvent.change(field, { target: { value: 'Book the room' } });
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Send' })));

  expect(create.mock.lastCall?.[0]).toMatchObject({ project_id: null, organization_id: 'xer' });
  expect(hide).toHaveBeenCalledOnce();
  expect(openTask).not.toHaveBeenCalled();

  cleanup();
  await renderPanel();
  expect(screen.getByRole('button', { name: 'Organization: Xer' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Project: Other' })).toBeInTheDocument();
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

const attach = async (...files: File[]) => {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => fireEvent.change(input, { target: { files } }));
};
const notes = (name = 'notes.txt') => new File(['Meeting notes'], name, { type: 'text/plain' });
const sentFile = (index: number, name = 'notes.txt') => ({
  attachment_id: FILE_IDS[index],
  name,
  byte_size: 13,
});

it('sends the files and images attached with the TODO, which then holds them', async () => {
  const field = await renderPanel();
  await attach(
    notes(),
    new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' })
  );
  expect(screen.getByRole('list', { name: 'Attachments, 2' })).toBeInTheDocument();
  fireEvent.change(field, { target: { value: 'Summarize the notes' } });
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));

  expect(create.mock.lastCall?.[0]).toMatchObject({
    text: 'Summarize the notes',
    files: [sentFile(0)],
    images: [{ kind: 'image', storage_path: IMAGE_PATH }],
  });
  expect(screen.queryByRole('list', { name: /Attachments/ })).toBeNull();
  expect(discardAttachment).not.toHaveBeenCalled();
});

it('keeps the files of a send whose answer never came, and discards the rest when removed or closed', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  create
    .mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce({ kind: 'refused', code: 'TODO_ATTACHMENT_UNAVAILABLE' });
  const field = await renderPanel();
  fireEvent.change(field, { target: { value: 'Summarize the notes' } });
  await attach(notes());
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));
  await attach(notes('plan.md'));
  // The attachments are part of the TODO, so another one is a new TODO.
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));
  const [lost, changed] = create.mock.calls.map(([request]) => request);
  expect(changed.client_todo_id).not.toBe(lost.client_todo_id);
  expect(changed.files).toEqual([sentFile(0), sentFile(1, 'plan.md')]);
  expect(screen.getByRole('alert')).toHaveTextContent("That didn't work. Please try again.");

  // The lost send may have added a TODO that holds notes.txt.
  fireEvent.click(screen.getByRole('button', { name: 'Remove notes.txt' }));
  expect(discardAttachment).not.toHaveBeenCalled();
  fireEvent.mouseDown(screen.getByRole('button', { name: 'Hide Notification' }));
  expect(discardAttachment).toHaveBeenCalledExactlyOnceWith({ attachmentId: FILE_IDS[1] });
  expect(hide).toHaveBeenCalledOnce();
});

it('never discards the files of a send still waiting for its answer when the panel closes', async () => {
  let answer: (result: TodoWriteResult<TodoView>) => void = () => undefined;
  create.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
  const field = await renderPanel();
  fireEvent.change(field, { target: { value: 'Summarize the notes' } });
  await attach(notes());
  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));

  // The TODO may be saved holding the file, so closing leaves it staged.
  fireEvent.mouseDown(screen.getByRole('button', { name: 'Hide Notification' }));
  await act(async () => answer(STARTING));

  expect(create.mock.lastCall?.[0].files).toEqual([sentFile(0)]);
  expect(hide).toHaveBeenCalled();
  expect(discardAttachment).not.toHaveBeenCalled();
});
