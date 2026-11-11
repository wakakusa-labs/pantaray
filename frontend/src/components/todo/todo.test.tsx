import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { expectIconButtonTooltips } from '@/tests/iconTooltip';

import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { DeletedTodoRow } from './DeletedTodoRow';
import { OrganizationRail } from './OrganizationRail';
import { TodoAddRow } from './TodoAddRow';
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
  carried: false,
  due: null,
  state: { kind: 'not_started', reason: 'starting' },
  deletion_request: null,
  conversation: { kind: 'none' },
  ...overrides,
});

function renderRow(overrides: Partial<TodoView> = {}, selected = false, pending = false) {
  const handlers = { onToggleCheck: vi.fn(), onSelect: vi.fn(), onDelete: vi.fn() };
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
  expect(body()).toHaveTextContent(/^見積書を送る完了$/);
});

it('says where the TODO stands in a word, Pantaray’s deletion request first', () => {
  renderRow({ carried: true, state: { kind: 'waiting', reason: 'approval', note: '確認を' } });
  expect(body()).toHaveTextContent(/^見積書を送る返事待ち$/);

  renderRow({
    state: { kind: 'working', note: '下書き中' },
    deletion_request: { request_id: 'r1', reason: '重複です' },
  });
  expect(body()).toHaveTextContent(/^見積書を送る消してよい？$/);
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

it('adds to the project and due day chosen, offering the week’s days from today', async () => {
  const onSubmit = vi.fn(async () => true);
  render(
    ja(
      <TodoAddRow
        page={{
          today: TODAY,
          current_week_start: '2026-11-02',
          week: { start: '2026-11-02', end: '2026-11-08' },
          items: [],
        }}
        projects={[
          { projectId: 'p1', projectName: '見積もり', organization: null },
          { projectId: null, projectName: null, organization: null },
        ]}
        onSubmit={onSubmit}
      />
    )
  );
  const due = screen.getByRole('combobox', { name: '期限' });
  expect(
    within(due)
      .getAllByRole('option')
      .map((option) => option.textContent)
  ).toEqual(['期限なし', '土 11/7', '日 11/8']);
  await userEvent.type(screen.getByRole('textbox', { name: 'TODO を書く' }), '見積書を送る{Enter}');
  expect(onSubmit).toHaveBeenLastCalledWith('p1', '見積書を送る', null);

  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'プロジェクト' }), '');
  await userEvent.selectOptions(due, '2026-11-08');
  await userEvent.type(screen.getByRole('textbox', { name: 'TODO を書く' }), '請求書{Enter}');
  expect(onSubmit).toHaveBeenLastCalledWith(null, '請求書', { date: '2026-11-08', repeat: 'none' });
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
