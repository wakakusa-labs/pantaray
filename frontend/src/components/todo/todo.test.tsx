import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { expectIconButtonTooltips, expectTooltipIsName } from '@/tests/iconTooltip';

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
    onSetDates: vi.fn(),
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
  expect(screen.getByRole('button', { name: '期限 日 11/8: 見積書を送る' })).not.toHaveClass(
    'todo-row__due--late'
  );
  expect(screen.getByRole('img', { name: '毎週くり返す' })).toBeInTheDocument();

  renderRow({ due: { date: '2026-11-06', repeat: 'none' } });
  expect(screen.getByRole('button', { name: '期限 金 11/6: 見積書を送る' })).toHaveClass(
    'todo-row__due--late'
  );

  renderRow({
    due: { date: '2026-11-05', repeat: 'none' },
    state: { kind: 'done', by: 'user', at: '2026-11-04T12:00:00Z' },
  });
  expect(screen.getByRole('button', { name: '期限 木 11/5: 見積書を送る' })).not.toHaveClass(
    'todo-row__due--late'
  );
});

it('waits on a row while its write is pending', () => {
  renderRow({}, true, true);
  expect(body()).toHaveAttribute('aria-current', 'true');
  expect(screen.getByRole('checkbox', { name: '完了: 見積書を送る' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '削除: 見積書を送る' })).toBeDisabled();
});

// happy-dom has no Popover API, so the picker's panel is opened and closed by its toggle event.
const toggle = (trigger: HTMLElement, newState: 'open' | 'closed') => {
  const panel = trigger.nextElementSibling!;
  act(() => {
    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState }));
  });
  return within(panel as HTMLElement);
};
const day = (picker: ReturnType<typeof within>, name: string) =>
  picker.getByRole('button', { name: new RegExp(`^2026年${name}`) });

it('picks a row’s start, then its due day, and begins again from a day before the start', () => {
  const handlers = renderRow({
    starts_on: '2026-11-10',
    due: { date: '2026-11-20', repeat: 'weekly' },
    state: { kind: 'not_started', reason: 'scheduled' },
  });
  const picker = toggle(
    screen.getByRole('button', { name: '開始日 火 11/10から: 見積書を送る' }),
    'open'
  );
  expect(picker.getByRole('button', { name: /^開始 / })).toHaveAttribute('aria-pressed', 'true');
  // A start after the due day takes the due day away; the next click picks the due day.
  fireEvent.click(day(picker, '11月24日'));
  expect(picker.getByRole('button', { name: /^期限 / })).toHaveAttribute('aria-pressed', 'true');
  expect(picker.getByRole('button', { name: /^期限 / })).toHaveTextContent('なし');
  // A day before the start begins the range again from it.
  fireEvent.click(day(picker, '11月12日'));
  expect(handlers.onSetDates).not.toHaveBeenCalled();
  expect(picker.getByRole('button', { name: /^開始 / })).toHaveTextContent('木 11/12');
  fireEvent.click(day(picker, '11月14日'));
  expect(handlers.onSetDates).toHaveBeenCalledOnce();
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({
    startsOn: '2026-11-12',
    due: { date: '2026-11-14', repeat: 'weekly' },
  });
  expect(day(picker, '11月13日')).toHaveAttribute('data-range', 'between');
});

it('writes only the date that changed, and the start alone when the panel closes on it', () => {
  const handlers = renderRow({
    starts_on: '2026-11-10',
    due: { date: '2026-11-20', repeat: 'none' },
    state: { kind: 'not_started', reason: 'scheduled' },
  });
  const trigger = screen.getByRole('button', { name: '期限 金 11/20: 見積書を送る' });
  // From the due day, the first click picks the due day.
  fireEvent.click(day(toggle(trigger, 'open'), '11月18日'));
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({
    due: { date: '2026-11-18', repeat: 'none' },
  });
  toggle(trigger, 'closed');
  expect(handlers.onSetDates).toHaveBeenCalledOnce();

  // A start picked and the panel let go: the start is handed over once, on its own.
  const start = screen.getByRole('button', { name: '開始日 火 11/10から: 見積書を送る' });
  fireEvent.click(day(toggle(start, 'open'), '11月9日'));
  toggle(start, 'closed');
  toggle(start, 'closed');
  expect(handlers.onSetDates).toHaveBeenCalledTimes(2);
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({ startsOn: '2026-11-09' });

  fireEvent.click(toggle(start, 'open').getByRole('button', { name: '開始を未定にする' }));
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({ startsOn: null });
  fireEvent.click(toggle(trigger, 'open').getByRole('button', { name: '期限なし' }));
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({ due: null });
});

it('drops the dates chosen while a write on the row is pending', () => {
  const handlers = renderRow({ due: { date: '2026-11-20', repeat: 'none' } }, false, true);
  const trigger = screen.getByRole('button', { name: '期限 金 11/20: 見積書を送る' });
  fireEvent.click(day(toggle(trigger, 'open'), '11月18日'));
  toggle(trigger, 'closed');
  expect(handlers.onSetDates).not.toHaveBeenCalled();
});

it('once Pantaray started, changes only the due day, never before the start', () => {
  const handlers = renderRow({
    starts_on: '2026-11-02',
    due: { date: '2026-11-08', repeat: 'weekly' },
    state: { kind: 'working', note: null },
  });
  expect(screen.queryByRole('button', { name: /^開始日/ })).toBeNull();
  expect(stateWord()).toHaveTextContent(/^作業中$/);
  const picker = toggle(screen.getByRole('button', { name: '期限 日 11/8: 見積書を送る' }), 'open');
  expect(picker.queryByRole('button', { name: /^開始 / })).toBeNull();
  expect(picker.queryByRole('button', { name: '開始を未定にする' })).toBeNull();
  expect(day(picker, '11月1日')).toHaveAttribute('aria-disabled', 'true');
  fireEvent.click(day(picker, '11月1日'));
  expect(handlers.onSetDates).not.toHaveBeenCalled();
  fireEvent.click(day(picker, '11月20日'));
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({
    due: { date: '2026-11-20', repeat: 'weekly' },
  });
});

it('moves the focus over the days with the arrow keys, into the next month past this one', () => {
  renderRow({ due: { date: '2026-11-27', repeat: 'none' } });
  const picker = toggle(
    screen.getByRole('button', { name: '期限 金 11/27: 見積書を送る' }),
    'open'
  );
  expect(day(picker, '11月27日')).toHaveFocus();
  expect(day(picker, '11月27日')).toHaveAttribute('tabindex', '0');
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' });
  expect(day(picker, '11月28日')).toHaveFocus();
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
  expect(day(picker, '11月21日')).toHaveFocus();
  // The six weeks in view end on Sunday 12/6; a week down from 12/5 is the next month's.
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  expect(day(picker, '12月12日')).toHaveFocus();
  expect(picker.getByRole('group', { name: '2026年12月' })).toBeInTheDocument();
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

  // A TODO is one line: pasted lines are joined, and Shift+Enter adds as Enter does.
  fireEvent.change(addField(), { target: { value: '見積書を\n  送る\n' } });
  expect(addField()).toHaveValue('見積書を 送る ');
  await userEvent.type(addField(), '{Shift>}{Enter}{/Shift}');
  expect(onSubmit).toHaveBeenLastCalledWith('p1', TODAY, '見積書を 送る', null);
  expect(onImport).not.toHaveBeenCalled();
});

const modeButton = () => screen.getByRole('button', { name: 'ファイルやテキストから取り込む' });

it('imports text of several lines once its button turns importing on, then turns back', async () => {
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
  expect(modeButton()).toHaveAttribute('aria-pressed', 'false');
  expect(screen.queryByRole('button', { name: '添付' })).toBeNull();
  expectIconButtonTooltips(document.querySelector('.todo-add-bar__choices')!);
  await userEvent.click(modeButton());
  // Importing: the field says so, the send button drafts, and one TODO's choices step aside.
  expect(modeButton()).toHaveAttribute('aria-pressed', 'true');
  expect(addField()).toHaveAttribute(
    'placeholder',
    'テキストを貼るか、ファイルを添付すると、TODO の下書きを作ります'
  );
  expectTooltipIsName(screen.getByRole('button', { name: '下書きを作る' }));
  expect(screen.getByRole('button', { name: '添付' })).toBeVisible();
  expect(screen.queryByRole('combobox', { name: 'プロジェクト' })).toBeNull();
  expect(screen.queryByRole('button', { name: /期限/ })).toBeNull();
  await userEvent.type(addField(), '見積書を送る{Shift>}{Enter}{/Shift}請求書を作る');
  // Lines kept when importing goes off become one TODO's line.
  await userEvent.click(modeButton());
  expect(addField()).toHaveValue('見積書を送る 請求書を作る');
  await userEvent.click(modeButton());
  fireEvent.change(addField(), { target: { value: '見積書を送る\n請求書を作る' } });
  await userEvent.type(addField(), '{Enter}');
  expect(onSubmit).not.toHaveBeenCalled();
  expect(onImport).toHaveBeenLastCalledWith('見積書を送る\n請求書を作る', {
    files: [],
    images: [],
  });
  expect(addField()).toHaveValue('');
  expect(modeButton()).toHaveAttribute('aria-pressed', 'false');
  expect(addField()).toHaveAttribute('placeholder', 'TODO を追加');
  expect(screen.getByRole('button', { name: '追加' })).toBeVisible();
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
  // A file dropped while adding turns importing on in view.
  fireEvent.drop(bar, {
    dataTransfer: { files: [new File(['x'], 'board.png', { type: 'image/png' })] },
  });
  expect(modeButton()).toHaveAttribute('aria-pressed', 'true');
  expect(await screen.findByRole('img')).toBeVisible();
  // An image alone imports.
  await userEvent.click(screen.getByRole('button', { name: '下書きを作る' }));
  expect(onSubmit).not.toHaveBeenCalled();
  expect(onImport).toHaveBeenLastCalledWith('', {
    files: [],
    images: [{ kind: 'image', storage_path: 'images/board.png' }],
  });
  expect(screen.queryByRole('img')).toBeNull();

  expect(modeButton()).toHaveAttribute('aria-pressed', 'false');

  fireEvent.paste(addField(), {
    clipboardData: { files: [new File(['x'], 'board.png', { type: 'image/png' })] },
  });
  expect(modeButton()).toHaveAttribute('aria-pressed', 'true');
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

it('in a scope with no project, imports but says a single TODO needs a project', async () => {
  onImport.mockClear();
  const onSubmit = vi.fn(async () => true);
  render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={[]}
        disabled={false}
        onSubmit={onSubmit}
        onImport={onImport}
      />
    )
  );
  await userEvent.type(addField(), '見積書を送る{Enter}');
  expect(onSubmit).not.toHaveBeenCalled();
  expect(screen.getByRole('alert')).toHaveTextContent('TODO を入れるプロジェクトがありません');
  expect(addField()).toHaveValue('見積書を送る');

  await userEvent.click(modeButton());
  expect(screen.queryByRole('alert')).toBeNull();
  await userEvent.type(addField(), '{Shift>}{Enter}{/Shift}請求書を作る{Enter}');
  expect(onImport).toHaveBeenLastCalledWith('見積書を送る\n請求書を作る', {
    files: [],
    images: [],
  });
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
  await userEvent.click(modeButton());
  await userEvent.type(addField(), 'a{Shift>}{Enter}{/Shift}b{Enter}');
  expect(pendingImport).toHaveBeenCalledOnce();
  expect(modeButton()).toBeDisabled();
  const notes = new File(['# 議事録'], 'notes.md', { type: 'text/markdown' });
  expect(screen.getByRole('button', { name: '添付' })).toBeDisabled();
  fireEvent.drop(container.querySelector('fieldset')!, { dataTransfer: { files: [notes] } });
  fireEvent.paste(addField(), { clipboardData: { files: [notes] } });
  await userEvent.upload(container.querySelector<HTMLInputElement>('input[type="file"]')!, notes);
  expect(attachFile).not.toHaveBeenCalled();

  await act(async () => answer(true));
  expect(screen.queryByText('notes.md')).toBeNull();
  expect(modeButton()).toBeEnabled();
  window.electron = undefined;
});

it('discards the files staged for an import turned off, those still being written too', async () => {
  let written: (file: unknown) => void = () => undefined;
  const attachFile = vi
    .fn()
    .mockResolvedValueOnce({ attachmentId: 'a-staged', name: 'notes.md', byteSize: 12 })
    .mockReturnValueOnce(new Promise((resolve) => (written = resolve)));
  const discardAttachment = vi.fn(async () => undefined);
  window.electron = {
    actions: { attachFile, attachImage: vi.fn(), discardAttachment },
  } as unknown as Window['electron'];
  const { container } = render(
    ja(
      <TodoAddBar
        today={TODAY}
        projects={PROJECTS}
        disabled={false}
        onSubmit={vi.fn(async () => true)}
        onImport={onImport}
      />
    )
  );
  const bar = container.querySelector('fieldset')!;
  const notes = new File(['# 議事録'], 'notes.md', { type: 'text/markdown' });
  fireEvent.drop(bar, { dataTransfer: { files: [notes] } });
  expect(await screen.findByText('notes.md')).toBeVisible();
  await userEvent.click(modeButton());
  expect(discardAttachment).toHaveBeenLastCalledWith({ attachmentId: 'a-staged' });
  expect(screen.queryByText('notes.md')).toBeNull();

  // A file whose write lands after importing went off is discarded, not shown.
  fireEvent.drop(bar, { dataTransfer: { files: [notes] } });
  await userEvent.click(modeButton());
  await act(async () => written({ attachmentId: 'a-late', name: 'notes.md', byteSize: 12 }));
  expect(discardAttachment).toHaveBeenLastCalledWith({ attachmentId: 'a-late' });
  await userEvent.click(modeButton());
  expect(screen.queryByText('notes.md')).toBeNull();
  window.electron = undefined;
});

it('adds to the project and the dates chosen, shown short beside the calendar icon', async () => {
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
  const dates = screen.getByRole('button', { name: '今日から、期限なし' });
  expect(dates).toHaveTextContent(/^$/);
  expectTooltipIsName(dates);
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'プロジェクト' }), '');
  const picker = toggle(dates, 'open');
  fireEvent.click(day(picker, '11月9日'));
  fireEvent.click(day(picker, '11月12日'));
  expect(dates).toHaveAccessibleName('月 11/9から、期限 木 11/12');
  expect(dates).toHaveTextContent(/^11\/9 → 11\/12$/);
  await userEvent.type(addField(), '請求書');
  await userEvent.click(screen.getByRole('button', { name: '追加' }));
  expect(onSubmit).toHaveBeenLastCalledWith(null, '2026-11-09', '請求書', {
    date: '2026-11-12',
    repeat: 'none',
  });

  // With no start, a TODO waits until one is set; a start of today is left out of the text.
  fireEvent.click(toggle(dates, 'open').getByRole('button', { name: '開始を未定にする' }));
  expect(dates).toHaveTextContent(/^未定 → 11\/12$/);
  await userEvent.type(addField(), '見積書{Enter}');
  expect(onSubmit).toHaveBeenLastCalledWith(null, null, '見積書', {
    date: '2026-11-12',
    repeat: 'none',
  });
  fireEvent.click(day(toggle(dates, 'open'), '11月7日'));
  toggle(dates, 'closed');
  expect(dates).toHaveTextContent(/^→ 11\/12$/);

  rerender(bar(PROJECTS.slice(0, 1)));
  expect(screen.queryByRole('combobox', { name: 'プロジェクト' })).toBeNull();
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
