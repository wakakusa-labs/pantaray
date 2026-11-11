import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type {
  TodoImportRefRequest,
  TodoImportRegisterRequest,
  TodoImportResult,
  TodoImportView,
  TodoImportWriteRequest,
} from '../../../electron/src/todos/todoImportContracts';
import { TodoImportPane } from './TodoImportPane';

// The Action's conversation is the task pane's; here it only says whether a run is going.
let running = false;
vi.mock('@/components/task/ActionTaskPane', () => ({
  ActionTaskPane: ({
    title,
    closeButton,
    renderUnderConversation,
  }: {
    title: string;
    closeButton: ReactNode;
    renderUnderConversation: (running: boolean) => ReactNode;
  }) => (
    <section>
      <h1>{title}</h1>
      {closeButton}
      {renderUnderConversation(running)}
    </section>
  ),
}));

afterEach(cleanup);

const view = (overrides: Partial<TodoImportView> = {}): TodoImportView => ({
  import_id: 'import-1',
  scope: 'all',
  organization_id: null,
  revision: 3,
  state: 'open',
  action_id: 'action-1',
  groups: [
    {
      organization: { name: 'Wakakusa', new: false },
      project: { name: '採用', new: true },
      todos: [
        {
          text: '求人票を出す',
          due: '2026-11-20',
          starts_on: null,
          duplicate: false,
          keep_duplicate: false,
        },
        {
          text: '面接の日程を決める',
          due: null,
          starts_on: null,
          duplicate: true,
          keep_duplicate: false,
        },
      ],
    },
    {
      organization: { name: '取引先 B', new: true },
      project: { name: 'サイト改修', new: true },
      todos: [
        {
          text: 'トップページの文言を直す',
          due: null,
          starts_on: null,
          duplicate: false,
          keep_duplicate: false,
        },
      ],
    },
  ],
  issue: null,
  result: null,
  ...overrides,
});

const read = vi.fn<(request: TodoImportRefRequest) => Promise<TodoImportResult>>();
const write = vi.fn<(request: TodoImportWriteRequest) => Promise<TodoImportResult>>();
const register = vi.fn<(request: TodoImportRegisterRequest) => Promise<TodoImportResult>>();
const cancel = vi.fn<(request: TodoImportRefRequest) => Promise<TodoImportResult>>();
const ok = (result: TodoImportView): TodoImportResult => ({ kind: 'ok', result });

beforeEach(() => {
  running = false;
  read.mockReset().mockResolvedValue(ok(view()));
  write.mockReset().mockImplementation(async () => ok(view({ revision: 4 })));
  register.mockReset();
  cancel.mockReset();
  window.electron = {
    todos: { onChanged: () => () => undefined },
    todoImports: { read, write, register, cancel },
  } as unknown as Window['electron'];
});

function renderPane() {
  const handlers = { onRegistered: vi.fn(), onCanceled: vi.fn() };
  render(
    <UiLanguageProvider initialLanguage="ja">
      <TodoImportPane
        importId="import-1"
        today="2026-11-12"
        showOrganizations
        closeButton={null}
        onAddProject={() => undefined}
        {...handlers}
      />
    </UiLanguageProvider>
  );
  return handlers;
}

const registerButton = () => screen.getByRole('button', { name: /件を登録$/ });

it('draws the draft with its new names and counts only the TODOs registering creates', async () => {
  renderPane();
  expect(await screen.findByRole('heading', { name: '採用 新規' })).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Wakakusa' })).toBeVisible();
  expect(screen.getByRole('heading', { name: '取引先 B 新規' })).toBeVisible();
  expect(screen.getByText('すでにある')).toBeVisible();
  // The duplicate not kept is left out of the count.
  expect(registerButton()).toHaveTextContent('2 件を登録');

  await userEvent.click(screen.getByRole('checkbox', { name: 'それでも登録' }));
  expect(write).toHaveBeenLastCalledWith({
    import_id: 'import-1',
    expected_revision: 3,
    draft: {
      groups: [
        {
          organization: 'Wakakusa',
          project: '採用',
          todos: [
            { text: '求人票を出す', due: '2026-11-20', starts_on: null, keep_duplicate: false },
            { text: '面接の日程を決める', due: null, starts_on: null, keep_duplicate: true },
          ],
        },
        {
          organization: '取引先 B',
          project: 'サイト改修',
          todos: [
            { text: 'トップページの文言を直す', due: null, starts_on: null, keep_duplicate: false },
          ],
        },
      ],
    },
  });
  // The draft is read again after the write.
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
});

it('writes an edited text with the whole draft, and shows the draft as it is after a stale write', async () => {
  renderPane();
  const field = (await screen.findAllByRole('textbox', { name: 'TODO' }))[2];
  read.mockResolvedValue(
    ok(
      view({
        revision: 5,
        groups: [
          {
            organization: { name: 'Wakakusa', new: false },
            project: { name: '採用', new: true },
            todos: [
              {
                text: '求人票を書く',
                due: null,
                starts_on: null,
                duplicate: false,
                keep_duplicate: false,
              },
            ],
          },
        ],
      })
    )
  );
  write.mockResolvedValueOnce({
    kind: 'refused',
    refusal: { reason: 'stale_revision', group_index: null, todo_index: null },
  });
  await userEvent.clear(field);
  await userEvent.type(field, 'トップページを直す{Enter}');
  // Enter and the blur that may follow write once.
  fireEvent.blur(field);
  expect(write).toHaveBeenCalledOnce();
  const sent = write.mock.lastCall![0];
  expect(sent.expected_revision).toBe(3);
  expect(sent.draft.groups[1].todos[0].text).toBe('トップページを直す');
  expect(sent.draft.groups[0].todos).toHaveLength(2);

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'そのあいだに Pantaray が下書きを書き換えました。'
  );
  expect(await screen.findByDisplayValue('求人票を書く')).toBeVisible();
  expect(screen.getAllByRole('textbox', { name: 'TODO' })).toHaveLength(1);
});

it('removes a TODO, and its group with it once it is the last', async () => {
  renderPane();
  await userEvent.click(
    await screen.findByRole('button', { name: '下書きから外す: トップページの文言を直す' })
  );
  expect(write.mock.lastCall![0].draft.groups.map((group) => group.project)).toEqual(['採用']);
});

it('holds edits and 登録 while the Action runs, and lets the user cancel', async () => {
  running = true;
  const handlers = renderPane();
  expect(await screen.findByText(/^Pantaray が書き終えると/)).toHaveRole('status');
  expect(registerButton()).toBeDisabled();
  for (const field of screen.getAllByRole('textbox', { name: 'TODO' }))
    expect(field).toBeDisabled();

  cancel.mockResolvedValueOnce(ok(view({ state: 'canceled' })));
  await userEvent.click(screen.getByRole('button', { name: 'やめる' }));
  expect(cancel).toHaveBeenCalledWith({ import_id: 'import-1' });
  expect(handlers.onCanceled).toHaveBeenCalledOnce();
});

it('registers the revision shown and tells how many TODOs it added', async () => {
  const handlers = renderPane();
  register.mockResolvedValueOnce(
    ok(
      view({
        state: 'registered',
        result: {
          organization_ids: ['o'],
          project_ids: ['p1', 'p2'],
          todo_ids: ['t1', 't2'],
          skipped_duplicates: 1,
        },
      })
    )
  );
  await userEvent.click(await screen.findByRole('button', { name: '2 件を登録' }));
  await waitFor(() => expect(handlers.onRegistered).toHaveBeenCalledWith(2));
  expect(register).toHaveBeenCalledWith({ import_id: 'import-1', expected_revision: 3 });
  expect(handlers.onRegistered).toHaveBeenCalledWith(2);
});

it('keeps 登録 closed while the draft has an issue, and says why', async () => {
  read.mockResolvedValue(
    ok(view({ issue: { reason: 'project_name_taken', group_index: 1, todo_index: null } }))
  );
  renderPane();
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '同じ名前のプロジェクトが別の組織にあります。'
  );
  expect(registerButton()).toBeDisabled();
  expect(
    within(screen.getByRole('group', { name: '下書き' })).getAllByRole('listitem')
  ).toHaveLength(3);
});
