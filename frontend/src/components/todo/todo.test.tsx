import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { expectIconButtonTooltips } from '@/tests/iconTooltip';

import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { DeletedTodoRow } from './DeletedTodoRow';
import { OrganizationRail } from './OrganizationRail';
import { TodoProjectGroup } from './TodoProjectGroup';
import { TodoRow } from './TodoRow';
import { WeekSwitcher } from './WeekSwitcher';

afterEach(cleanup);

const TODAY = '2026-11-07';
const ja = (ui: ReactNode) => <UiLanguageProvider initialLanguage="ja">{ui}</UiLanguageProvider>;

function renderRow(overrides: Partial<TodoView> = {}, selected = false, pending = false) {
  const todo: TodoView = {
    todo_id: 'todo-1',
    project_id: 'project-1',
    text: '見積書を送る',
    author: 'user',
    origin: 'written',
    week_start: '2026-11-02',
    carried: false,
    due: null,
    state: { kind: 'not_started', reason: 'starting' },
    deletion_request: null,
    conversation: { kind: 'none' },
    ...overrides,
  };
  const handlers = {
    onToggleCheck: vi.fn(),
    onSelect: vi.fn(),
    onDelete: vi.fn(),
    onAnswerDeletion: vi.fn(),
  };
  const row = (
    <ul>
      <TodoRow todo={todo} today={TODAY} selected={selected} pending={pending} {...handlers} />
    </ul>
  );
  cleanup();
  render(ja(row));
  return handlers;
}

const body = () => screen.getByRole('button', { name: /^見積書を送る/ });

it('names the check by what pressing it does and keeps it apart from opening the TODO', async () => {
  const handlers = renderRow();
  const check = screen.getByRole('button', { name: '完了にする: 見積書を送る' });
  expect(check).toHaveAttribute('aria-pressed', 'false');
  await userEvent.click(check);
  await userEvent.click(body());
  expect(handlers.onToggleCheck).toHaveBeenCalledOnce();
  expect(handlers.onSelect).toHaveBeenCalledOnce();
  expectIconButtonTooltips(screen.getByRole('listitem'));

  renderRow({ state: { kind: 'done', by: 'ai', at: '2026-11-06T12:00:00Z' } });
  expect(screen.getByRole('button', { name: '未完了に戻す: 見積書を送る' })).toHaveAttribute(
    'aria-pressed',
    'true'
  );
  expect(body()).toHaveTextContent('Pantaray が完了（11/6）');
});

it('says where the TODO stands, joining a note only when there is one', () => {
  renderRow({ carried: true, state: { kind: 'partial', note: null } });
  expect(body()).toHaveTextContent('先週から持ち越し');
  expect(body()).toHaveTextContent('途中まで');
  expect(body()).not.toHaveTextContent(/null|。/);

  renderRow({ author: 'ai', state: { kind: 'working', note: '下書き中' } });
  expect(body()).toHaveTextContent('Pantaray が追加');
  expect(body()).toHaveTextContent('Pantaray が作業中。下書き中');
});

it('draws a due date within two days in the warm tone, and never a done one', () => {
  renderRow({ due: { date: '2026-11-09', repeat: 'weekly' } });
  expect(screen.getByText('11/9 まで')).toHaveClass('todo-due--soon');
  expect(screen.getByRole('img', { name: '毎週くり返す' })).toBeInTheDocument();

  renderRow({ due: { date: '2026-11-10', repeat: 'none' } });
  expect(screen.getByText('11/10 まで')).not.toHaveClass('todo-due--soon');

  renderRow({
    due: { date: '2026-11-05', repeat: 'none' },
    state: { kind: 'done', by: 'user', at: '2026-11-04T12:00:00Z' },
  });
  expect(screen.getByText('11/5 まで')).not.toHaveClass('todo-due--soon');
});

it('answers Pantaray’s deletion request with the button pressed, and waits while pending', async () => {
  const handlers = renderRow({ deletion_request: { request_id: 'r1', reason: '重複です' } });
  const prompt = screen.getByRole('group', {
    name: 'Pantaray がこの TODO を消したいと言っています',
  });
  expect(prompt).toHaveTextContent('重複です');
  await userEvent.click(within(prompt).getByRole('button', { name: '残す' }));
  expect(handlers.onAnswerDeletion).toHaveBeenLastCalledWith('keep');
  await userEvent.click(within(prompt).getByRole('button', { name: '消す' }));
  expect(handlers.onAnswerDeletion).toHaveBeenLastCalledWith('approve');
  expect(handlers.onDelete).not.toHaveBeenCalled();

  renderRow({ deletion_request: { request_id: 'r1', reason: '重複です' } }, true, true);
  expect(body()).toHaveAttribute('aria-current', 'true');
  for (const name of ['完了にする: 見積書を送る', '削除: 見積書を送る', '消す', '残す']) {
    expect(screen.getByRole('button', { name })).toBeDisabled();
  }
});

it('counts a group’s done TODOs and heads it with its organization when one is given', () => {
  const group = (done: number, total: number, organizationName: string | null = null) =>
    ja(
      <TodoProjectGroup
        projectName={organizationName ? null : '販路開拓'}
        organizationName={organizationName}
        doneCount={done}
        totalCount={total}
        addField={null}
      >
        {null}
      </TodoProjectGroup>
    );
  const { rerender } = render(group(2, 5));
  expect(screen.getByRole('region', { name: '販路開拓' })).toHaveTextContent('2 / 5 済み');
  rerender(group(5, 5));
  expect(screen.getByRole('region')).toHaveTextContent('すべて済み');
  rerender(group(0, 0));
  expect(screen.getByRole('region')).not.toHaveTextContent(/済み/);
  rerender(group(0, 1, 'Wakakusa'));
  expect(screen.getByRole('region', { name: 'Wakakusa プロジェクトなし' })).toBeInTheDocument();
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
          { organization_id: 'org-1', display_name: 'wakakusa' },
          { organization_id: 'org-2', display_name: 'llm-jp' },
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

it('offers 「今週へ」 only away from this week', async () => {
  const handlers = { onPrevious: vi.fn(), onNext: vi.fn(), onCurrent: vi.fn() };
  const week = { start: '2026-11-09', end: '2026-11-15' };
  const { rerender } = render(ja(<WeekSwitcher week={week} isCurrent={false} {...handlers} />));
  expect(screen.getByText(/11月9日.*〜11月15日/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '今週へ' }));
  await userEvent.click(screen.getByRole('button', { name: '前の週' }));
  expect(handlers.onCurrent).toHaveBeenCalledOnce();
  expect(handlers.onPrevious).toHaveBeenCalledOnce();

  rerender(ja(<WeekSwitcher week={week} isCurrent {...handlers} />));
  expect(screen.queryByRole('button', { name: '今週へ' })).not.toBeInTheDocument();
});
