import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { expectIconButtonTooltips } from '@/tests/iconTooltip';

import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { DeletedTodoRow } from './DeletedTodoRow';
import { OrganizationRail } from './OrganizationRail';
import { TodoAddBar } from './TodoAddBar';
import { TodoConversationPane } from './TodoConversationPane';
import { TodoProjectGroup } from './TodoProjectGroup';
import { TodoRow } from './TodoRow';

afterEach(cleanup);

const TODAY = '2026-11-07';
const ja = (ui: ReactNode) => <UiLanguageProvider initialLanguage="ja">{ui}</UiLanguageProvider>;

const makeTodo = (overrides: Partial<TodoView> = {}): TodoView => ({
  todo_id: 'todo-1',
  project_id: 'project-1',
  text: '見積書を送る',
  author: 'user',
  origin: 'written',
  starts_on: '2026-11-02',
  due: null,
  state: { kind: 'not_started', reason: 'starting' },
  deletion_request: null,
  conversation: { kind: 'none' },
  ...overrides,
});

function renderRow(overrides: Partial<TodoView> = {}, selected = false, pending = false) {
  const handlers = {
    onToggleCheck: vi.fn(),
    onSelect: vi.fn(),
    onDelete: vi.fn(),
    onSetDue: vi.fn(),
    onSetStart: vi.fn(),
  };
  const row = (
    <ul>
      <TodoRow
        todo={makeTodo(overrides)}
        today={TODAY}
        selected={selected}
        pending={pending}
        {...handlers}
      />
    </ul>
  );
  cleanup();
  render(ja(row));
  return handlers;
}

const body = () => screen.getByRole('button', { name: /^見積書を送る/ });
const stateWord = () => document.querySelector('.todo-row__state');

it('checks the TODO with a checkbox of its own, apart from opening the TODO', async () => {
  const handlers = renderRow();
  const check = screen.getByRole('checkbox', { name: '完了: 見積書を送る' });
  expect(check).not.toBeChecked();
  await userEvent.click(check);
  await userEvent.click(body());
  expect(handlers.onToggleCheck).toHaveBeenCalledOnce();
  expect(handlers.onSelect).toHaveBeenCalledOnce();
  expectIconButtonTooltips(screen.getByRole('listitem'));

  renderRow({ state: { kind: 'done', by: 'ai', at: '2026-11-06T12:00:00Z' } });
  expect(screen.getByRole('checkbox', { name: '完了: 見積書を送る' })).toBeChecked();
  expect(stateWord()).toHaveTextContent(/^完了$/);
});

it('says where the TODO stands in a word, Pantaray’s deletion request first', () => {
  renderRow({ state: { kind: 'waiting', reason: 'approval', note: '確認を' } });
  expect(stateWord()).toHaveTextContent(/^返事待ち$/);

  renderRow({
    state: { kind: 'working', note: '下書き中' },
    deletion_request: { request_id: 'r1', reason: '重複です' },
  });
  expect(stateWord()).toHaveTextContent(/^消してよい？$/);
  // Pantaray's request waits for an answer, not for a start date.
  expect(screen.queryByRole('button', { name: /^開始日/ })).toBeNull();
});

it('draws a due day gone by in the late tone, never a done one, and marks a weekly one', () => {
  renderRow({ due: { date: '2026-11-08', repeat: 'weekly' } });
  expect(screen.getByText('日 11/8')).not.toHaveClass('todo-row__due--late');
  expect(screen.getByRole('img', { name: '毎週くり返す' })).toBeInTheDocument();

  renderRow({ due: { date: '2026-11-06', repeat: 'none' } });
  expect(screen.getByText('金 11/6')).toHaveClass('todo-row__due--late');

  renderRow({
    due: { date: '2026-11-05', repeat: 'none' },
    state: { kind: 'done', by: 'user', at: '2026-11-04T12:00:00Z' },
  });
  expect(screen.getByText('木 11/5')).not.toHaveClass('todo-row__due--late');
});

it('waits on a row while its write is pending', () => {
  renderRow({}, true, true);
  expect(body()).toHaveAttribute('aria-current', 'true');
  expect(screen.getByRole('checkbox', { name: '完了: 見積書を送る' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '削除: 見積書を送る' })).toBeDisabled();
});

it('changes the due day from its row, keeping its repeat, and takes it away', () => {
  const handlers = renderRow({ due: { date: '2026-11-08', repeat: 'weekly' } });
  expect(screen.getByRole('button', { name: '期限 日 11/8: 見積書を送る' })).toHaveTextContent(
    '日 11/8'
  );
  const picker = screen.getByRole('dialog', { name: '期限' });
  // 来週 is the Monday after today (Saturday 11/7); 明日 is the date it has, so nothing changes.
  fireEvent.click(within(picker).getByRole('button', { name: '明日' }));
  expect(handlers.onSetDue).not.toHaveBeenCalled();
  fireEvent.click(within(picker).getByRole('button', { name: '来週' }));
  expect(handlers.onSetDue).toHaveBeenLastCalledWith({ date: '2026-11-09', repeat: 'weekly' });
  fireEvent.click(within(picker).getByRole('button', { name: '期限をなくす' }));
  expect(handlers.onSetDue).toHaveBeenLastCalledWith(null);

  // A date picked from the field's calendar is taken; a typed one waits for Enter, since typing
  // passes through other dates on the way.
  const field = within(picker).getByLabelText('日付');
  fireEvent.change(field, { target: { value: '2026-11-20' } });
  expect(handlers.onSetDue).toHaveBeenLastCalledWith({ date: '2026-11-20', repeat: 'weekly' });
  fireEvent.keyDown(field, { key: '2' });
  fireEvent.change(field, { target: { value: '0002-11-20' } });
  expect(handlers.onSetDue).toHaveBeenCalledTimes(3);
  fireEvent.change(field, { target: { value: '2026-12-01' } });
  fireEvent.keyDown(field, { key: 'Enter' });
  expect(handlers.onSetDue).toHaveBeenLastCalledWith({ date: '2026-12-01', repeat: 'weekly' });
});

it('changes the start of a TODO Pantaray has not started, and only of one', () => {
  const handlers = renderRow({
    starts_on: '2026-11-18',
    state: { kind: 'not_started', reason: 'scheduled' },
  });
  screen.getByRole('button', { name: '開始日 水 11/18から: 見積書を送る' });
  const picker = screen.getByRole('dialog', { name: '開始日' });
  expect(within(picker).queryByRole('button', { name: '期限をなくす' })).toBeNull();
  fireEvent.click(within(picker).getByRole('button', { name: '明日' }));
  expect(handlers.onSetStart).toHaveBeenLastCalledWith('2026-11-08');
  fireEvent.click(within(picker).getByRole('button', { name: '未定にする' }));
  expect(handlers.onSetStart).toHaveBeenLastCalledWith(null);

  // With no start, Pantaray waits for the user to give it one from the same word.
  const unset = renderRow({
    starts_on: null,
    state: { kind: 'not_started', reason: 'unscheduled' },
  });
  expect(screen.getByRole('button', { name: '開始日未定: 見積書を送る' })).toHaveTextContent(
    /^開始日未定$/
  );
  fireEvent.click(
    within(screen.getByRole('dialog', { name: '開始日' })).getByRole('button', { name: '今日' })
  );
  expect(unset.onSetStart).toHaveBeenLastCalledWith(TODAY);

  renderRow({ state: { kind: 'working', note: null } });
  expect(screen.queryByRole('button', { name: /^開始日/ })).toBeNull();
  expect(stateWord()).toHaveTextContent(/^作業中$/);
});

it('answers Pantaray’s deletion request under the conversation, and waits while pending', async () => {
  const onAnswerDeletion = vi.fn();
  const pane = (pending: boolean) =>
    ja(
      <TodoConversationPane
        todo={makeTodo({ deletion_request: { request_id: 'r1', reason: '重複です' } })}
        pending={pending}
        deletionFailure={null}
        onAnswerDeletion={onAnswerDeletion}
        onAddProject={vi.fn()}
        closeButton={null}
      />
    );
  const { rerender } = render(pane(false));
  const prompt = screen.getByRole('group', {
    name: 'Pantaray がこの TODO を消したいと言っています',
  });
  expect(prompt).toHaveTextContent('重複です');
  await userEvent.click(within(prompt).getByRole('button', { name: '残す' }));
  expect(onAnswerDeletion).toHaveBeenLastCalledWith('r1', 'keep');
  await userEvent.click(within(prompt).getByRole('button', { name: '消す' }));
  expect(onAnswerDeletion).toHaveBeenLastCalledWith('r1', 'approve');

  rerender(pane(true));
  for (const name of ['消す', '残す']) {
    expect(screen.getByRole('button', { name })).toBeDisabled();
  }
});

const PROJECTS = [
  { projectId: 'p1', projectName: '見積もり', organization: null },
  { projectId: null, projectName: null, organization: null },
];
const addField = () => screen.getByRole('textbox', { name: 'TODO を追加' });
const onImport = vi.fn(async () => true);

it('adds to the first project from today with no due, and an IME’s Enter sends nothing', async () => {
  const onSubmit = vi.fn(async () => true);
  render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={PROJECTS}
        disabled={false}
        onSubmit={onSubmit}
        onImport={onImport}
      />
    )
  );
  expect(screen.getByRole('button', { name: '追加' })).toBeDisabled();
  await userEvent.type(addField(), '見積書を送る');
  // The Enter that ends a composition belongs to the IME.
  fireEvent.keyDown(addField(), { key: 'Enter', isComposing: true });
  fireEvent.keyDown(addField(), { key: 'Enter', keyCode: 229 });
  expect(onSubmit).not.toHaveBeenCalled();
  await userEvent.type(addField(), '{Enter}');
  expect(onSubmit).toHaveBeenLastCalledWith('p1', TODAY, '見積書を送る', null);
  expect(addField()).toHaveValue('');
  expect(onImport).not.toHaveBeenCalled();
});

it('imports text of several lines instead of adding it as one TODO', async () => {
  onImport.mockClear();
  const onSubmit = vi.fn(async () => true);
  render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={PROJECTS}
        disabled={false}
        onSubmit={onSubmit}
        onImport={onImport}
      />
    )
  );
  // Shift+Enter starts a new line; the project and dates, one TODO's, step aside.
  await userEvent.type(addField(), '見積書を送る{Shift>}{Enter}{/Shift}請求書を作る');
  expect(screen.queryByRole('combobox', { name: 'プロジェクト' })).toBeNull();
  expect(screen.queryByRole('button', { name: '期限なし' })).toBeNull();
  await userEvent.type(addField(), '{Enter}');
  expect(onSubmit).not.toHaveBeenCalled();
  expect(onImport).toHaveBeenLastCalledWith('見積書を送る\n請求書を作る', {
    files: [],
    images: [],
  });
  expect(addField()).toHaveValue('');
});

it('imports attached documents and images, staged as the composers stage them', async () => {
  onImport.mockClear();
  const attachFile = vi.fn(async ({ name }: { name: string }) => ({
    attachmentId: '6f1c7a64-5d1e-4c4b-9b5b-3d0d7c3f2a10',
    name,
    byteSize: 12,
  }));
  const attachImage = vi.fn(async () => ({ kind: 'stored', storagePath: 'images/board.png' }));
  window.electron = {
    actions: { attachFile, attachImage, discardAttachment: vi.fn(async () => undefined) },
  } as unknown as Window['electron'];
  const onSubmit = vi.fn(async () => true);
  const { container } = render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={PROJECTS}
        disabled={false}
        onSubmit={onSubmit}
        onImport={onImport}
      />
    )
  );
  const bar = container.querySelector('fieldset')!;
  fireEvent.drop(bar, {
    dataTransfer: { files: [new File(['x'], 'board.png', { type: 'image/png' })] },
  });
  expect(await screen.findByRole('img')).toBeVisible();
  // An image alone imports.
  await userEvent.click(screen.getByRole('button', { name: '下書きを作る' }));
  expect(onSubmit).not.toHaveBeenCalled();
  expect(onImport).toHaveBeenLastCalledWith('', {
    files: [],
    images: [{ kind: 'image', storage_path: 'images/board.png' }],
  });
  expect(screen.queryByRole('img')).toBeNull();

  fireEvent.drop(bar, {
    dataTransfer: { files: [new File(['x'], 'board.png', { type: 'image/png' })] },
  });
  await screen.findByRole('img');
  await userEvent.click(screen.getByRole('button', { name: '添付' }));
  await userEvent.upload(
    container.querySelector<HTMLInputElement>('input[type="file"]')!,
    new File(['# 議事録'], 'notes.md', { type: 'text/markdown' })
  );
  expect(await screen.findByText('notes.md')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '下書きを作る' }));
  expect(onImport).toHaveBeenLastCalledWith('', {
    files: [
      { attachment_id: '6f1c7a64-5d1e-4c4b-9b5b-3d0d7c3f2a10', name: 'notes.md', byte_size: 12 },
    ],
    images: [{ kind: 'image', storage_path: 'images/board.png' }],
  });
  window.electron = undefined;
});

it('adds no file while an import is starting', async () => {
  let answer: (started: boolean) => void = () => undefined;
  const pendingImport = vi.fn(() => new Promise<boolean>((resolve) => (answer = resolve)));
  const attachFile = vi.fn(async ({ name }: { name: string }) => ({
    attachmentId: '6f1c7a64-5d1e-4c4b-9b5b-3d0d7c3f2a10',
    name,
    byteSize: 12,
  }));
  window.electron = {
    actions: { attachFile, attachImage: vi.fn(), discardAttachment: vi.fn(async () => undefined) },
  } as unknown as Window['electron'];
  const { container } = render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={PROJECTS}
        disabled={false}
        onSubmit={vi.fn(async () => true)}
        onImport={pendingImport}
      />
    )
  );
  await userEvent.type(addField(), 'a{Shift>}{Enter}{/Shift}b{Enter}');
  expect(pendingImport).toHaveBeenCalledOnce();
  const notes = new File(['# 議事録'], 'notes.md', { type: 'text/markdown' });
  expect(screen.getByRole('button', { name: '添付' })).toBeDisabled();
  fireEvent.drop(container.querySelector('fieldset')!, { dataTransfer: { files: [notes] } });
  fireEvent.paste(addField(), { clipboardData: { files: [notes] } });
  await userEvent.upload(container.querySelector<HTMLInputElement>('input[type="file"]')!, notes);
  expect(attachFile).not.toHaveBeenCalled();

  await act(async () => answer(true));
  expect(screen.queryByText('notes.md')).toBeNull();
  expect(screen.getByRole('button', { name: '添付' })).toBeEnabled();
  window.electron = undefined;
});

it('adds to the project, due and start chosen, and offers no project with one in view', async () => {
  const onSubmit = vi.fn(async () => true);
  const bar = (projects: typeof PROJECTS) =>
    ja(
      <TodoAddBar
        today={TODAY}
        projects={projects}
        disabled={false}
        onSubmit={onSubmit}
        onImport={onImport}
      />
    );
  const { rerender } = render(bar(PROJECTS));
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'プロジェクト' }), '');
  await userEvent.click(screen.getByRole('button', { name: '期限なし' }));
  fireEvent.click(
    within(screen.getByRole('dialog', { name: '期限' })).getByRole('button', { name: '明日' })
  );
  fireEvent.click(
    within(screen.getByRole('dialog', { name: '開始日' })).getByRole('button', { name: '来週' })
  );
  expect(screen.getByRole('button', { name: '期限 日 11/8' })).toBeVisible();
  expect(screen.getByRole('button', { name: '開始 月 11/9' })).toBeVisible();
  await userEvent.type(addField(), '請求書');
  await userEvent.click(screen.getByRole('button', { name: '追加' }));
  expect(onSubmit).toHaveBeenLastCalledWith(null, '2026-11-09', '請求書', {
    date: '2026-11-08',
    repeat: 'none',
  });

  rerender(bar(PROJECTS.slice(0, 1)));
  expect(screen.queryByRole('combobox', { name: 'プロジェクト' })).toBeNull();
});

it('adds a TODO with no start, which a date chosen later replaces', async () => {
  const onSubmit = vi.fn(async () => true);
  render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={PROJECTS}
        disabled={false}
        onSubmit={onSubmit}
        onImport={onImport}
      />
    )
  );
  const startPicker = () => within(screen.getByRole('dialog', { name: '開始日' }));
  fireEvent.click(startPicker().getByRole('button', { name: '未定にする' }));
  expect(screen.getByRole('button', { name: '開始 未定' })).toBeVisible();
  await userEvent.type(addField(), '請求書{Enter}');
  expect(onSubmit).toHaveBeenLastCalledWith('p1', null, '請求書', null);

  fireEvent.click(startPicker().getByRole('button', { name: '今日' }));
  expect(screen.getByRole('button', { name: '開始 今日' })).toBeVisible();
});

it('counts a group’s done TODOs and names its organization after it when one is given', () => {
  const group = (organizationName: string | null) =>
    ja(
      <TodoProjectGroup
        projectName={organizationName ? null : '販路開拓'}
        organizationName={organizationName}
        doneCount={2}
        totalCount={5}
      >
        {null}
      </TodoProjectGroup>
    );
  const { rerender } = render(group(null));
  expect(screen.getByRole('region', { name: '販路開拓' })).toHaveTextContent('2 / 5');
  rerender(group('Wakakusa'));
  expect(screen.getByRole('region', { name: 'プロジェクトなし Wakakusa' })).toBeInTheDocument();
});

it('counts the days a deleted TODO can still be restored', async () => {
  const onRestore = vi.fn();
  render(
    ja(
      <ul>
        <DeletedTodoRow
          item={{
            todo_id: 'todo-1',
            project_id: null,
            text: '展示会の見積もり',
            deleted_by: 'ai_approved_by_user',
            deleted_at: '2026-11-03T12:00:00Z',
            purge_on: '2026-12-03',
          }}
          projectName={null}
          today={TODAY}
          pending={false}
          onRestore={onRestore}
        />
      </ul>
    )
  );
  const row = screen.getByRole('listitem');
  expect(row).toHaveTextContent('あと 26 日で完全に消えます');
  expect(row).toHaveTextContent('11/3 に Pantaray が消したいと言い');
  await userEvent.click(screen.getByRole('button', { name: '戻す: 展示会の見積もり' }));
  expect(onRestore).toHaveBeenCalledOnce();
});

it('presses the organization in view and selects another', async () => {
  const onSelect = vi.fn();
  render(
    ja(
      <OrganizationRail
        organizations={[
          { organization_id: 'org-1', display_name: 'wakakusa', icon_data_url: null },
          { organization_id: 'org-2', display_name: 'llm-jp', icon_data_url: null },
        ]}
        selected={{ scope: 'organization', organization_id: 'org-1' }}
        onSelect={onSelect}
        onAdd={vi.fn()}
      />
    )
  );
  const pressed = (name: string) =>
    screen.getByRole('button', { name }).getAttribute('aria-pressed');
  expect(screen.getByRole('button', { name: 'wakakusa' })).toHaveTextContent('W');
  expect(['wakakusa', 'llm-jp', 'すべての組織'].map(pressed)).toEqual(['true', 'false', 'false']);
  await userEvent.click(screen.getByRole('button', { name: '組織なし' }));
  expect(onSelect).toHaveBeenCalledWith({ scope: 'no_organization' });
});
