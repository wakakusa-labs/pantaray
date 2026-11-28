import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { expectIconButtonTooltips } from '@/tests/iconTooltip';

import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { DeletedTodoRow } from './DeletedTodoRow';
import { OrganizationRail } from './OrganizationRail';
import { TodoConversationPane } from './TodoConversationPane';
import { TodoImportBar } from './TodoImportBar';
import { TodoNewLine } from './TodoNewLine';
import { TodoProjectGroup } from './TodoProjectGroup';
import { TodoProjectLine } from './TodoProjectLine';
import { TodoRow } from './TodoRow';
import { dueDay } from './todoDates';

afterEach(cleanup);

const TODAY = '2026-11-07';
const ja = (ui: ReactNode) => <UiLanguageProvider initialLanguage="ja">{ui}</UiLanguageProvider>;

const makeTodo = (overrides: Partial<TodoView> = {}): TodoView => ({
  todo_id: 'todo-1',
  project_id: 'project-1',
  organization_id: null,
  text: '見積書を送る',
  author: 'user',
  origin: { kind: 'written' },
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

it('shows a TODO handed over to the user with no state word, like an ordinary one', () => {
  renderRow({
    author: 'ai',
    origin: { kind: 'suggestion' },
    state: { kind: 'handed_over' },
    conversation: { kind: 'suggestion', suggestion_id: 'sug-1' },
  });
  expect(screen.getByRole('checkbox', { name: '完了: 見積書を送る' })).not.toBeChecked();
  expect(stateWord()).toHaveTextContent(/^$/);
  expect(stateWord()).toHaveClass('todo-row__state', { exact: true });
  // Nothing will start it, so it has no start date to change.
  expect(screen.queryByRole('button', { name: /^開始日/ })).toBeNull();
});

it('draws a due day gone by in the late tone, never a done one, and marks a weekly one', () => {
  renderRow({ due: { date: '2026-11-08', repeat: 'weekly' } });
  expect(screen.getByRole('button', { name: '期限 11/8(日): 見積書を送る' })).not.toHaveClass(
    'todo-row__due--late'
  );
  expect(screen.getByRole('img', { name: '毎週くり返す' })).toBeInTheDocument();

  renderRow({ due: { date: '2026-11-06', repeat: 'none' } });
  expect(screen.getByRole('button', { name: '期限 11/6(金): 見積書を送る' })).toHaveClass(
    'todo-row__due--late'
  );

  renderRow({
    due: { date: '2026-11-05', repeat: 'none' },
    state: { kind: 'done', by: 'user', at: '2026-11-04T12:00:00Z' },
  });
  expect(screen.getByRole('button', { name: '期限 11/5(木): 見積書を送る' })).not.toHaveClass(
    'todo-row__due--late'
  );
});

it('waits on a row while its write is pending', () => {
  renderRow({}, true, true);
  expect(body()).toHaveAttribute('aria-current', 'true');
  expect(screen.getByRole('checkbox', { name: '完了: 見積書を送る' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '削除: 見積書を送る' })).toBeDisabled();
});

// happy-dom has no Popover API, so the picker's panel is opened and closed by the events a
// browser fires: `beforetoggle` as a panel closes, then `toggle`.
const toggleEvent = (trigger: HTMLElement, type: 'beforetoggle' | 'toggle', newState: string) =>
  act(() => {
    trigger.nextElementSibling!.dispatchEvent(Object.assign(new Event(type), { newState }));
  });
const toggle = (trigger: HTMLElement, newState: 'open' | 'closed') => {
  if (newState === 'closed') toggleEvent(trigger, 'beforetoggle', newState);
  toggleEvent(trigger, 'toggle', newState);
  return within(trigger.nextElementSibling as HTMLElement);
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
    screen.getByRole('button', { name: '開始日 11/10(火)から: 見積書を送る' }),
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
  expect(picker.getByRole('button', { name: /^開始 / })).toHaveTextContent('11/12(木)');
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
  const trigger = screen.getByRole('button', { name: '期限 11/20(金): 見積書を送る' });
  // From the due day, the first click picks the due day.
  fireEvent.click(day(toggle(trigger, 'open'), '11月18日'));
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({
    due: { date: '2026-11-18', repeat: 'none' },
  });
  toggle(trigger, 'closed');
  expect(handlers.onSetDates).toHaveBeenCalledOnce();

  // A start picked and the panel let go: the start is handed over once, on its own.
  const start = screen.getByRole('button', { name: '開始日 11/10(火)から: 見積書を送る' });
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
  const trigger = screen.getByRole('button', { name: '期限 11/20(金): 見積書を送る' });
  fireEvent.click(day(toggle(trigger, 'open'), '11月18日'));
  toggle(trigger, 'closed');
  expect(handlers.onSetDates).not.toHaveBeenCalled();
});

it('moves a started TODO’s start on its due day’s calendar, and keeps a done one’s', () => {
  const started = {
    starts_on: '2026-11-02',
    due: { date: '2026-11-08', repeat: 'weekly' },
    state: { kind: 'working', note: null },
    conversation: { kind: 'action', action_id: 'action-1' },
  } satisfies Partial<TodoView>;
  const handlers = renderRow(started);
  // The state word says the work, not the start.
  expect(screen.queryByRole('button', { name: /^開始日/ })).toBeNull();
  expect(stateWord()).toHaveTextContent(/^作業中$/);
  const picker = toggle(
    screen.getByRole('button', { name: '期限 11/8(日): 見積書を送る' }),
    'open'
  );
  fireEvent.click(picker.getByRole('button', { name: /^開始 / }));
  fireEvent.click(day(picker, '11月10日'));
  fireEvent.click(day(picker, '11月20日'));
  expect(handlers.onSetDates).toHaveBeenLastCalledWith({
    startsOn: '2026-11-10',
    due: { date: '2026-11-20', repeat: 'weekly' },
  });

  // Done, or waiting in a Suggestion only the user's answer starts, it has no start to move.
  for (const fixed of [
    { ...started, state: { kind: 'done', by: 'ai', at: '2026-11-06T12:00:00Z' } },
    {
      ...started,
      state: { kind: 'waiting', reason: 'approval', note: null },
      conversation: { kind: 'suggestion', suggestion_id: 'sug-1' },
    },
  ] satisfies Partial<TodoView>[]) {
    const done = renderRow(fixed);
    const fixedPicker = toggle(
      screen.getByRole('button', { name: '期限 11/8(日): 見積書を送る' }),
      'open'
    );
    expect(fixedPicker.queryByRole('button', { name: /^開始 / })).toBeNull();
    expect(fixedPicker.queryByRole('button', { name: '開始を未定にする' })).toBeNull();
    expect(day(fixedPicker, '11月1日')).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(day(fixedPicker, '11月20日'));
    expect(done.onSetDates).toHaveBeenLastCalledWith({
      due: { date: '2026-11-20', repeat: 'weekly' },
    });
  }
});

it('writes a day with its weekday in each language’s own order', () => {
  expect(dueDay('ja', '2026-11-28')).toBe('11/28(土)');
  expect(dueDay('en', '2026-11-28')).toBe('Sat, 11/28');
});

it('moves the focus over the days with the arrow keys, into the next month past this one', () => {
  renderRow({ due: { date: '2026-11-27', repeat: 'none' } });
  const picker = toggle(
    screen.getByRole('button', { name: '期限 11/27(金): 見積書を送る' }),
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
        requestedFile={null}
        handedPreview={null}
        pageControls={null}
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

function renderNewLine(onCreate: (text: string) => Promise<boolean>) {
  render(
    ja(
      <ul>
        <TodoNewLine id="new-line" placeName="見積もり" onCreate={onCreate} />
      </ul>
    )
  );
  return screen.getByRole('textbox', { name: '見積もりに TODO を追加' });
}

it('adds the line on Enter and stays on the emptied line, but not on an IME’s or a blank Enter', async () => {
  const onCreate = vi.fn(async () => true);
  const line = renderNewLine(onCreate);
  await userEvent.click(line);
  await userEvent.keyboard('{Enter}');
  await userEvent.type(line, '   {Enter}');
  // The Enter that ends a composition belongs to the IME, and Shift+Enter adds nothing.
  await userEvent.type(line, ' 見積書を送る ');
  fireEvent.keyDown(line, { key: 'Enter', isComposing: true });
  fireEvent.keyDown(line, { key: 'Enter', keyCode: 229 });
  await userEvent.keyboard('{Shift>}{Enter}{/Shift}');
  expect(onCreate).not.toHaveBeenCalled();
  await userEvent.keyboard('{Enter}');
  expect(onCreate).toHaveBeenCalledExactlyOnceWith('見積書を送る');
  expect(line).toHaveValue('');
  expect(line).toHaveFocus();
});

it('leaves an IME’s Escape to the IME, and clears the line on a plain Escape', async () => {
  const line = renderNewLine(vi.fn(async () => true));
  await userEvent.type(line, '見積書');
  fireEvent.keyDown(line, { key: 'Escape', isComposing: true });
  fireEvent.keyDown(line, { key: 'Escape', keyCode: 229 });
  expect(line).toHaveValue('見積書');
  expect(line).toHaveFocus();
  await userEvent.keyboard('{Escape}');
  expect(line).toHaveValue('');
  expect(line).not.toHaveFocus();
});

it('keeps what was typed while the add was answering, and the whole line when it failed', async () => {
  let answer: (added: boolean) => void = () => undefined;
  const onCreate = vi.fn(() => new Promise<boolean>((resolve) => (answer = resolve)));
  const line = renderNewLine(onCreate);
  await userEvent.type(line, '見積書{Enter}');
  // A second Enter while the first add is answering sends nothing more.
  await userEvent.type(line, ' 請求書{Enter}');
  expect(onCreate).toHaveBeenCalledOnce();
  await act(async () => answer(true));
  expect(line).toHaveValue('請求書');

  await userEvent.type(line, '{Enter}');
  await userEvent.type(line, 'を作る');
  await act(async () => answer(false));
  expect(onCreate).toHaveBeenLastCalledWith('請求書');
  expect(line).toHaveValue('請求書を作る');
});

it('joins pasted lines into one, and Escape clears the line and leaves it', async () => {
  const line = renderNewLine(vi.fn(async () => true));
  await userEvent.click(line);
  await userEvent.paste('見積書を\n  送る\n');
  expect(line).toHaveValue('見積書を 送る ');
  await userEvent.keyboard('{Escape}');
  expect(line).toHaveValue('');
  expect(line).not.toHaveFocus();
});

function renderProjectLine(onCreate: (name: string) => Promise<boolean>, failure: string | null) {
  render(ja(<TodoProjectLine failure={failure} onCreate={onCreate} />));
  return screen.getByRole('textbox', { name: 'プロジェクトを追加' });
}

it('names a project on Enter and empties the line, but not on an IME’s Enter or Escape', async () => {
  const onCreate = vi.fn(async () => true);
  const line = renderProjectLine(onCreate, null);
  await userEvent.click(line);
  await userEvent.keyboard('   {Enter}');
  await userEvent.type(line, '{Backspace}{Backspace}{Backspace}販路開拓 ');
  fireEvent.keyDown(line, { key: 'Enter', isComposing: true });
  fireEvent.keyDown(line, { key: 'Enter', keyCode: 229 });
  fireEvent.keyDown(line, { key: 'Escape', isComposing: true });
  fireEvent.keyDown(line, { key: 'Escape', keyCode: 229 });
  await userEvent.keyboard('{Shift>}{Enter}{/Shift}');
  expect(onCreate).not.toHaveBeenCalled();
  expect(line).toHaveValue('販路開拓 ');
  expect(line).toHaveFocus();
  await userEvent.keyboard('{Enter}');
  expect(onCreate).toHaveBeenCalledExactlyOnceWith('販路開拓');
  expect(line).toHaveValue('');

  await userEvent.type(line, '採用');
  await userEvent.keyboard('{Escape}');
  expect(line).toHaveValue('');
  expect(line).not.toHaveFocus();
  expect(onCreate).toHaveBeenCalledOnce();
});

it('keeps a refused name on the line and tells why under it', async () => {
  const line = renderProjectLine(
    vi.fn(async () => false),
    '見積もり という名前のプロジェクトがすでにあります。'
  );
  expect(screen.queryByRole('alert')).toBeNull();
  await userEvent.type(line, '見積もり{Enter}');
  expect(line).toHaveValue('見積もり');
  expect(screen.getByRole('alert')).toHaveTextContent(
    '見積もり という名前のプロジェクトがすでにあります。'
  );
});

const importField = () => screen.getByRole('textbox', { name: 'ファイルやテキストから取り込む' });
const draftButton = () => screen.getByRole('button', { name: '下書きを作る' });
const renderImportBar = (onImport: ComponentProps<typeof TodoImportBar>['onImport']) =>
  render(ja(<TodoImportBar disabled={false} onImport={onImport} />));

it('imports text of several lines on Enter, but not on an IME’s Enter', async () => {
  const onImport = vi.fn(async () => true);
  renderImportBar(onImport);
  expect(draftButton()).toBeDisabled();
  expectIconButtonTooltips(document.querySelector('.todo-import-bar__choices')!);
  await userEvent.type(importField(), '見積書を送る{Shift>}{Enter}{/Shift}請求書を作る');
  fireEvent.keyDown(importField(), { key: 'Enter', isComposing: true });
  expect(onImport).not.toHaveBeenCalled();
  await userEvent.type(importField(), '{Enter}');
  expect(onImport).toHaveBeenLastCalledWith('見積書を送る\n請求書を作る', {
    files: [],
    images: [],
  });
  expect(importField()).toHaveValue('');
});

it('imports attached documents and images, staged as the composers stage them', async () => {
  const onImport = vi.fn(async () => true);
  const attachFile = vi.fn(async ({ name }: { name: string }) => ({
    attachmentId: '6f1c7a64-5d1e-4c4b-9b5b-3d0d7c3f2a10',
    name,
    byteSize: 12,
  }));
  const attachImage = vi.fn(async () => ({ kind: 'stored', storagePath: 'images/board.png' }));
  window.electron = {
    actions: { attachFile, attachImage, discardAttachment: vi.fn(async () => undefined) },
  } as unknown as Window['electron'];
  const { container } = renderImportBar(onImport);
  fireEvent.drop(container.querySelector('fieldset')!, {
    dataTransfer: { files: [new File(['x'], 'board.png', { type: 'image/png' })] },
  });
  expect(await screen.findByRole('img')).toBeVisible();
  // An image alone imports.
  await userEvent.click(draftButton());
  expect(onImport).toHaveBeenLastCalledWith('', {
    files: [],
    images: [{ kind: 'image', storage_path: 'images/board.png' }],
  });
  expect(screen.queryByRole('img')).toBeNull();

  fireEvent.paste(importField(), {
    clipboardData: { files: [new File(['x'], 'board.png', { type: 'image/png' })] },
  });
  await screen.findByRole('img');
  await userEvent.click(screen.getByRole('button', { name: '添付' }));
  await userEvent.upload(
    container.querySelector<HTMLInputElement>('input[type="file"]')!,
    new File(['# 議事録'], 'notes.md', { type: 'text/markdown' })
  );
  expect(await screen.findByText('notes.md')).toBeVisible();
  await userEvent.click(draftButton());
  expect(onImport).toHaveBeenLastCalledWith('', {
    files: [
      { attachment_id: '6f1c7a64-5d1e-4c4b-9b5b-3d0d7c3f2a10', name: 'notes.md', byte_size: 12 },
    ],
    images: [{ kind: 'image', storage_path: 'images/board.png' }],
  });
  window.electron = undefined;
});

it('adds no file while an import is starting, and keeps lines typed meanwhile', async () => {
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
  const { container } = renderImportBar(pendingImport);
  await userEvent.type(importField(), 'a{Shift>}{Enter}{/Shift}b{Enter}');
  expect(pendingImport).toHaveBeenCalledOnce();
  const notes = new File(['# 議事録'], 'notes.md', { type: 'text/markdown' });
  expect(screen.getByRole('button', { name: '添付' })).toBeDisabled();
  fireEvent.drop(container.querySelector('fieldset')!, { dataTransfer: { files: [notes] } });
  fireEvent.paste(importField(), { clipboardData: { files: [notes] } });
  await userEvent.upload(container.querySelector<HTMLInputElement>('input[type="file"]')!, notes);
  expect(attachFile).not.toHaveBeenCalled();
  fireEvent.change(importField(), { target: { value: 'c\nd' } });

  await act(async () => answer(true));
  expect(screen.queryByText('notes.md')).toBeNull();
  // The newer lines are the next import's.
  expect(importField()).toHaveValue('c\nd');
  await userEvent.type(importField(), '{Enter}');
  expect(pendingImport).toHaveBeenLastCalledWith('c\nd', { files: [], images: [] });
  window.electron = undefined;
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
  expect(screen.getByRole('region', { name: 'その他 Wakakusa' })).toBeInTheDocument();
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
