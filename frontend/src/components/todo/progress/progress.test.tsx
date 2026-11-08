import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type {
  GoalProgress,
  GoalView,
  ProgressPace,
  ProgressPeriodView,
  ProgressRecordView,
  TodoProgressPage,
} from '../../../../electron/src/todos/todoContracts';
import { GoalForm } from './GoalForm';
import { ProgressDayPane, type RecordActions } from './ProgressDayPane';
import { ProjectGoalDialog } from './ProjectGoalDialog';
import { pageStartOf, shiftPage, totalsOf, weekHolding } from './progressPages';
import { percentValue } from './progressText';
import { ProgressView } from './ProgressView';

afterEach(cleanup);

const ja = (ui: ReactNode) => <UiLanguageProvider initialLanguage="ja">{ui}</UiLanguageProvider>;

const GOAL: GoalView = {
  project_id: 'p-sales',
  text: '売上',
  target: 300_000_000,
  unit: '円',
  deadline: '2027-08-31',
  // Noon UTC is 10/1 on the calendar of any machine the tests run on.
  set_at: '2026-10-01T12:00:00Z',
};

const record = (
  record_id: string,
  day: string,
  note: string,
  quantity: number,
  unit: string,
  evidence: ProgressRecordView['evidence'] = { kind: 'user' },
  project_id: string | null = 'p-sales'
): ProgressRecordView => ({
  record_id,
  todo_id: `todo-${record_id}`,
  project_id,
  quantity,
  unit,
  note,
  evidence,
  recorded_at: `${day}T12:00:00Z`,
});

const RECORDS = [
  record('r1', '2026-11-05', '丸和商事から受注', 10_000_000, '円'),
  record('r2', '2026-11-05', '北星から受注', 15_000_000, '円', {
    kind: 'action_steps',
    action_id: 'action-1',
    step_ids: ['s1'],
  }),
  record('r3', '2026-10-30', '東和から受注', 5_000_000, '円'),
  record('r4', '2026-11-03', '展示会で名刺交換', 40, '枚'),
  record('r5', '2026-11-05', '面接', 2, '人', { kind: 'user' }, 'p-hiring'),
];

const checked = (todo_id: string, day: string, text: string) => ({
  todo_id,
  project_id: 'p-sales',
  text,
  checked_at: `${day}T12:00:00Z`,
});

const CHECKED = [
  checked('t1', '2026-11-03', '見積書を送る'),
  checked('t2', '2026-11-04', '名刺を入れる'),
];

const DAY_MS = 86_400_000;

/** November 2026's calendar, 10/26 to 12/6, a row a day as the backend lays it out. */
function dayRows(records: readonly ProgressRecordView[], done = CHECKED): ProgressPeriodView[] {
  const start = Date.parse('2026-10-26T00:00:00Z');
  return Array.from({ length: 42 }, (_, index) => {
    const day = new Date(start + index * DAY_MS).toISOString().slice(0, 10);
    return {
      start: day,
      end: day,
      record_ids: records
        .filter((item) => item.recorded_at.startsWith(day))
        .map((item) => item.record_id),
      checked: done.filter((todo) => todo.checked_at.startsWith(day)),
    };
  });
}

const PROJECTED: ProgressPace = {
  kind: 'projected',
  window_start: '2026-10-11',
  record_ids: ['r1', 'r2', 'r3'],
  per_day: 1_071_428.57,
  projected_on: '2027-06-03',
};

const measured = (overrides: Partial<Extract<GoalProgress, { kind: 'measured' }>> = {}) =>
  ({
    kind: 'measured',
    goal: GOAL,
    total: 30_000_000,
    share: 0.1,
    record_ids: ['r1', 'r2', 'r3'],
    other_unit_record_ids: ['r4'],
    expected_share: 0.11,
    deadline_passed: false,
    pace: PROJECTED,
    ...overrides,
  }) satisfies GoalProgress;

const page = (overrides: Partial<TodoProgressPage> = {}): TodoProgressPage => ({
  today: '2026-11-07',
  period: 'day',
  page_start: '2026-11-01',
  rows: dayRows(RECORDS),
  goals: [measured()],
  records: RECORDS,
  ...overrides,
});

const NAMES = new Map([
  ['p-sales', '販路開拓'],
  ['p-hiring', '採用'],
]);
const projectName = (projectId: string) => NAMES.get(projectId) ?? projectId;

function renderView(data: TodoProgressPage, selectedDay: string | null = null) {
  const handlers = { onSelectDay: vi.fn(), onSelectMonth: vi.fn(), onSetGoal: vi.fn() };
  cleanup();
  render(
    ja(
      <ProgressView page={data} projectName={projectName} selectedDay={selectedDay} {...handlers} />
    )
  );
  return handlers;
}

const lead = () => document.querySelector('.todo-progress__lead');
const goalRow = (name = '販路開拓の目標') => screen.getByRole('region', { name });
const dayCell = (name: RegExp) => screen.getByRole('button', { name });

it('leads with the week holding today on the current page, and the one goal with its pace', () => {
  renderView(page());
  // 11/2–11/8: r4, then r1 and r2 added up, for 販路開拓, and r5 for 採用; 10/30's r3 is last
  // week's.
  expect(lead()).toHaveTextContent(
    '今週は、販路開拓が 40 枚と 25,000,000 円、採用が 2 人進みました。済んだ TODO は 2 件です。' +
      '売上 300,000,000 円の目標に対して、いまは 10% です。' +
      'このペースなら 2027 年 6 月のはじめに届きます。'
  );

  const row = goalRow();
  expect(row).toHaveTextContent('売上 300,000,000 円');
  expect(row).toHaveTextContent('販路開拓');
  expect(row).toHaveTextContent('2027/8/31 まで');
  expect(row).toHaveTextContent('30,000,000 円');
  expect(row).toHaveTextContent('期限から逆算した今の位置 11%');
  expect(row).toHaveTextContent('10%');
  expect(row).toHaveTextContent('単位が「枚」の記録 1 件は、合計に入れていません。');
  // The 40 枚 is nowhere in the total.
  expect(row).not.toHaveTextContent('30,000,040');
  expect(row.querySelector('.todo-progress-goal__late')).toBeNull();
});

/** The amounts a calendar cell's lines show, without the project naming them. */
const amountsIn = (cell: Element) =>
  [...cell.querySelectorAll('.todo-progress-cell__line:not(.todo-progress-cell__line--muted)')].map(
    (line) => line.textContent?.match(/[\d,.]+ \S+$/)?.[0]
  );

it('leads with the very totals the week column and the month cell show', () => {
  renderView(page({ goals: [] }));
  const week = document.querySelector('.todo-progress-week--now');
  expect(amountsIn(week!)).toEqual(['40 枚', '25,000,000 円']);
  for (const amount of amountsIn(week!)) expect(lead()).toHaveTextContent(amount!);
  expect(week).toHaveTextContent('✓ 2 済み');
  expect(lead()).toHaveTextContent('済んだ TODO は 2 件です。');

  renderView(
    page({
      period: 'month',
      page_start: '2026-01-01',
      rows: Array.from({ length: 12 }, (_, index) => {
        const start = `2026-${String(index + 1).padStart(2, '0')}-01`;
        const end = index === 10 ? '2026-11-30' : start;
        return { start, end, record_ids: index === 10 ? ['r1', 'r5'] : [], checked: [] };
      }),
      goals: [],
    })
  );
  const november = screen.getByRole('button', { name: /^2026年11月/ });
  expect(amountsIn(november)).toEqual(['10,000,000 円', '2 人']);
  expect(lead()).toHaveTextContent(/^今月は、販路開拓が 10,000,000 円、採用が 2 人進みました。$/);
});

it('marks a pace that misses its deadline, in the goal and in the lead', () => {
  renderView(page({ goals: [measured({ goal: { ...GOAL, deadline: '2027-03-31' } })] }));
  expect(lead()).toHaveTextContent(
    'このペースなら 2027 年 6 月のはじめに届きます。期限の 2027/3/31 には間に合いません。'
  );
  expect(goalRow().querySelector('.todo-progress-goal__late')).toHaveTextContent(
    'このペースなら 2027 年 6 月のはじめに届きます。期限の 2027/3/31 には間に合いません。'
  );

  renderView(page({ goals: [measured({ deadline_passed: true })] }));
  expect(goalRow().querySelector('.todo-progress-goal__late')).toHaveTextContent(
    '期限の 2027/8/31 を過ぎています。'
  );
});

it('leads with the whole month or year on any other page, never with the weeks spilling in', () => {
  // October's page seen from November: 10/30's r3, while 11/1's row spills in from November.
  const october = record('r6', '2026-11-01', '11/1 の受注', 1_000, '円');
  renderView(
    page({
      today: '2026-12-02',
      page_start: '2026-10-01',
      rows: dayRows([RECORDS[2], october])
        .slice(0, 35)
        .map((row) => row),
      records: [RECORDS[2], october],
      goals: [],
    })
  );
  expect(lead()).toHaveTextContent(/^10月は、販路開拓が 5,000,000 円進みました。$/);

  renderView(
    page({
      today: '2027-02-01',
      period: 'month',
      page_start: '2026-01-01',
      rows: Array.from({ length: 12 }, (_, index) => {
        const start = `2026-${String(index + 1).padStart(2, '0')}-01`;
        return {
          start,
          end: start,
          record_ids: index === 10 ? ['r1', 'r5'] : [],
          checked: index === 10 ? CHECKED : [],
        };
      }),
      goals: [],
    })
  );
  expect(lead()).toHaveTextContent(
    /^2026年は、販路開拓が 10,000,000 円、採用が 2 人進みました。済んだ TODO は 2 件です。$/
  );
});

it('shows no number for a goal without records, and offers a goal to a project without one', () => {
  renderView(page({ goals: [{ kind: 'no_records', goal: GOAL, other_unit_record_ids: [] }] }));
  expect(goalRow()).toHaveTextContent('まだ記録がありません。');
  expect(goalRow()).not.toHaveTextContent('%');
  expect(lead()).toHaveTextContent('売上 300,000,000 円の目標には、まだ記録がありません。');

  renderView(page({ goals: [{ kind: 'no_records', goal: GOAL, other_unit_record_ids: ['r4'] }] }));
  expect(goalRow()).toHaveTextContent('まだ「円」の記録がありません。');
  expect(goalRow()).toHaveTextContent('単位が「枚」の記録 1 件は、合計に入れていません。');

  const handlers = renderView(page({ goals: [{ kind: 'no_goal', project_id: 'p-sales' }] }));
  expect(goalRow()).toHaveTextContent('まだ目標がありません。');
  expect(goalRow()).not.toHaveTextContent(/\d/);
  expect(lead()).not.toHaveTextContent('目標');
  fireEvent.click(within(goalRow()).getByRole('button', { name: '目標を設定' }));
  expect(handlers.onSetGoal).toHaveBeenCalledWith('p-sales');
});

it('tells each pace without a number it does not have', () => {
  renderView(
    page({
      goals: [
        measured({
          pace: { kind: 'too_few_records', window_start: '2026-10-11', record_ids: ['r1'] },
        }),
      ],
    })
  );
  expect(goalRow()).toHaveTextContent('記録がまだ少ないので、届く時期は出していません。');
  expect(lead()).not.toHaveTextContent('このペースなら');

  renderView(
    page({
      goals: [
        measured({
          total: 360_000_000,
          share: 1.2,
          pace: { kind: 'reached' },
          deadline_passed: true,
        }),
      ],
    })
  );
  expect(goalRow()).toHaveTextContent('120%');
  expect(goalRow()).toHaveTextContent('目標に届きました。');
  // A reached goal is not late, whatever the date.
  expect(goalRow()).not.toHaveTextContent('過ぎています');
  expect(goalRow().querySelector('.todo-progress-goal__late')).toBeNull();
});

it('gives each day up to today its records by project and unit, and its checked TODOs', async () => {
  const handlers = renderView(page(), '2026-11-05');
  const thursday = dayCell(/^11月5日（木）/);
  expect(thursday).toHaveAccessibleName('11月5日（木）、記録 3 件、済んだ TODO 0 件');
  expect(thursday).toHaveAttribute('aria-pressed', 'true');
  // r1 and r2 add up; 採用's 人 stays its own line.
  expect(thursday).toHaveTextContent('販路開拓 25,000,000 円');
  expect(thursday).toHaveTextContent('採用 2 人');
  expect(dayCell(/^11月3日（火）/)).toHaveTextContent('販路開拓 40 枚✓ 1');
  expect(dayCell(/^11月7日（土）/)).toHaveAttribute('aria-current', 'date');
  // The weeks spilling in from the months either side show their days too.
  expect(dayCell(/^10月30日（金）/)).toHaveTextContent('販路開拓 5,000,000 円');
  // A day ahead holds nothing and opens nothing.
  expect(screen.queryByRole('button', { name: /^11月8日/ })).toBeNull();
  expect(screen.getByRole('button', { name: /^11月1日/ })).toBeInTheDocument();

  await userEvent.click(dayCell(/^11月3日（火）/));
  expect(handlers.onSelectDay).toHaveBeenCalledWith('2026-11-03');
});

it('tells how many lines a busy day leaves out, and totals each begun week from its days', () => {
  const busy = [
    record('b1', '2026-11-04', 'a', 1, '件'),
    record('b2', '2026-11-04', 'b', 2, '円'),
    record('b3', '2026-11-04', 'c', 3, '人'),
    record('b4', '2026-11-04', 'd', 4, '回'),
  ];
  const records = [...RECORDS, ...busy];
  renderView(page({ rows: dayRows(records), records }));
  expect(dayCell(/^11月4日（水）/)).toHaveTextContent('販路開拓 1 件販路開拓 2 円ほか 2✓ 1');

  const weeks = document.querySelectorAll('.todo-progress-week');
  expect(weeks).toHaveLength(6);
  // 11/2–11/8: six lines from its days' records, and both TODOs checked in it.
  expect(weeks[1]).toHaveTextContent('販路開拓 40 枚販路開拓 1 件ほか 4✓ 2 済み');
  expect(weeks[0]).toHaveTextContent(/^販路開拓 5,000,000 円$/);
  // Weeks not begun have no totals.
  expect(weeks[2]).toHaveTextContent(/^$/);
});

it('lays out the year as its months, each opening its calendar', async () => {
  const handlers = renderView(
    page({
      period: 'month',
      page_start: '2026-01-01',
      rows: Array.from({ length: 12 }, (_, index) => {
        const start = `2026-${String(index + 1).padStart(2, '0')}-01`;
        return {
          start,
          end: start,
          record_ids: index === 10 ? ['r1', 'r2', 'r5'] : [],
          checked: index === 10 ? CHECKED : [],
        };
      }),
    })
  );
  const months = within(screen.getByRole('group', { name: '2026年' })).getAllByRole('button');
  expect(months).toHaveLength(12);
  expect(months[10]).toHaveAccessibleName('2026年11月、記録 3 件、済んだ TODO 2 件');
  expect(months[10]).toHaveTextContent('11月販路開拓 25,000,000 円採用 2 人✓ 2');
  await userEvent.click(months[11]);
  expect(handlers.onSelectMonth).toHaveBeenCalledWith('2026-12-01');
});

function renderDay(row: ProgressPeriodView, pendingRecordIds: ReadonlySet<string> = new Set()) {
  const handlers: Pick<RecordActions, 'onOpenEvidence' | 'onDeleteRecord'> = {
    onOpenEvidence: vi.fn(),
    onDeleteRecord: vi.fn(),
  };
  cleanup();
  render(
    ja(
      <ProgressDayPane
        row={row}
        page={page()}
        projectName={projectName}
        pendingRecordIds={pendingRecordIds}
        recordFailure={null}
        {...handlers}
      />
    )
  );
  return handlers;
}

it('opens a day onto its records, the conversation behind an AI record, and the TODOs it checked', async () => {
  const [thursday] = page().rows.filter((row) => row.start === '2026-11-05');
  const handlers = renderDay(thursday, new Set(['r1']));
  const pane = screen.getByRole('region', { name: '11月5日（木）' });
  expect(pane).toHaveTextContent('記録 3 件済んだ TODO 0 件');
  const [byUser, byAi, other] = within(pane).getAllByRole('listitem');
  expect(byUser).toHaveTextContent('丸和商事から受注10,000,000 円販路開拓');
  expect(within(byUser).queryByRole('button', { name: /会話を開く/ })).toBeNull();
  expect(within(byUser).getByRole('button', { name: '削除: 丸和商事から受注' })).toBeDisabled();
  expect(other).toHaveTextContent('採用');

  await userEvent.click(
    within(byAi).getByRole('button', { name: 'この記録の会話を開く: 北星から受注' })
  );
  expect(handlers.onOpenEvidence).toHaveBeenCalledWith('action-1');
  await userEvent.click(within(byAi).getByRole('button', { name: '削除: 北星から受注' }));
  expect(handlers.onDeleteRecord).toHaveBeenCalledWith('r2');

  const [tuesday] = page().rows.filter((row) => row.start === '2026-11-03');
  renderDay(tuesday);
  expect(screen.getByRole('heading', { name: '済んだ TODO' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: '11月3日（火）' })).toHaveTextContent('見積書を送る');
});

it('steps pages across years and finds the week holding a day', () => {
  expect(pageStartOf('month', '2026-11-30')).toBe('2026-11-01');
  expect(pageStartOf('year', '2026-11-30')).toBe('2026-01-01');
  expect(shiftPage('month', '2026-12-01', 1)).toBe('2027-01-01');
  expect(shiftPage('month', '2026-01-01', -1)).toBe('2025-12-01');
  expect(shiftPage('year', '2026-01-01', -1)).toBe('2025-01-01');
  // Sunday 11/1 belongs to the week of Monday 10/26.
  expect(weekHolding(page().rows, '2026-11-01').map((row) => row.start)).toEqual([
    '2026-10-26',
    '2026-10-27',
    '2026-10-28',
    '2026-10-29',
    '2026-10-30',
    '2026-10-31',
    '2026-11-01',
  ]);
  const doubled = { start: 'x', end: 'x', record_ids: ['r1'], checked: [] };
  expect(totalsOf([doubled, doubled]).record_ids).toEqual(['r1']);
});

it('never writes a positive share or quantity as 0, and never rounds a share up to 100%', () => {
  renderView(
    page({
      goals: [
        measured({
          total: 1_000,
          share: 1_000 / 300_000_000,
          expected_share: 0.996,
          pace: { kind: 'too_few_records', window_start: '2026-10-11', record_ids: ['r1'] },
        }),
      ],
    })
  );
  expect(goalRow()).toHaveTextContent('0.0003%');
  expect(goalRow()).toHaveTextContent('期限から逆算した今の位置 99%');
  expect(lead()).toHaveTextContent('いまは 0.0003% です。');

  const tiny = record('r9', '2026-11-06', '打ち合わせ', 0.001, '時間');
  renderView(page({ rows: dayRows([tiny]), records: [tiny], goals: [] }));
  // One project's records only: the line need not name it.
  expect(dayCell(/^11月6日（金）/)).toHaveTextContent(/^6\s*0\.001 時間$/);
});

it('rounds a share down and keeps a small step visible', () => {
  expect(percentValue(0.29)).toBe(29);
  expect(percentValue(0.996)).toBe(99);
  expect(percentValue(1.2)).toBe(120);
  expect(percentValue(0.0003)).toBe(0.03);
  expect(percentValue(0)).toBe(0);
});

function renderForm(goal: GoalView | null, failure: string | null = null) {
  const handlers = {
    onSave: vi.fn(async () => true),
    onClear: vi.fn(async () => true),
    onClose: vi.fn(),
  };
  cleanup();
  render(ja(<GoalForm projectName="販路開拓" goal={goal} failure={failure} {...handlers} />));
  return handlers;
}

it('tells what to fix in the goal form before sending anything', async () => {
  const handlers = renderForm(null);
  const dialog = screen.getByRole('dialog', { name: '販路開拓の目標を決める' });
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(handlers.onSave).not.toHaveBeenCalled();
  expect(within(dialog).getByLabelText('目標')).toHaveAccessibleDescription(
    '目標を書いてください。'
  );
  expect(within(dialog).getByLabelText('目標')).toHaveFocus();
  expect(within(dialog).getByLabelText('量')).toHaveAccessibleDescription(
    '量は 0 より大きい数で書いてください。'
  );
  expect(within(dialog).getByLabelText('単位')).toHaveAccessibleDescription(
    '単位を書いてください。'
  );
  expect(within(dialog).getByLabelText('期限')).toHaveAccessibleDescription(
    '期限を選んでください。'
  );

  await userEvent.type(within(dialog).getByLabelText('目標'), ' 売上 ');
  await userEvent.type(within(dialog).getByLabelText('量'), '0');
  await userEvent.type(within(dialog).getByLabelText('単位'), '円');
  fireEvent.change(within(dialog).getByLabelText('期限'), { target: { value: '2027-08-31' } });
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(handlers.onSave).not.toHaveBeenCalled();
  expect(within(dialog).getByLabelText('量')).toHaveAccessibleDescription(
    '量は 0 より大きい数で書いてください。'
  );
  expect(within(dialog).getByLabelText('目標')).not.toHaveAttribute('aria-invalid');

  await userEvent.clear(within(dialog).getByLabelText('量'));
  await userEvent.type(within(dialog).getByLabelText('量'), '300000000');
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(handlers.onSave).toHaveBeenCalledWith({
    text: '売上',
    target: 300_000_000,
    unit: '円',
    deadline: '2027-08-31',
  });
  expect(handlers.onClose).toHaveBeenCalledOnce();
});

it('edits the goal it was opened with, removes it, and stays open on a refused write', async () => {
  const handlers = renderForm(GOAL, '期限は、目標を決めた日か、それより後の日にしてください。');
  const dialog = screen.getByRole('dialog', { name: '販路開拓の目標を直す' });
  expect(within(dialog).getByLabelText('量')).toHaveValue(300_000_000);
  expect(within(dialog).getByLabelText('期限')).toHaveValue('2027-08-31');
  expect(within(dialog).getByRole('alert')).toHaveTextContent('目標を決めた日か');

  handlers.onSave.mockResolvedValueOnce(false);
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(handlers.onSave).toHaveBeenCalledWith({
    text: '売上',
    target: 300_000_000,
    unit: '円',
    deadline: '2027-08-31',
  });
  expect(handlers.onClose).not.toHaveBeenCalled();

  await userEvent.click(within(dialog).getByRole('button', { name: '目標を外す' }));
  expect(handlers.onClear).toHaveBeenCalledOnce();
  expect(handlers.onClose).toHaveBeenCalledOnce();

  renderForm(null);
  expect(screen.queryByRole('button', { name: '目標を外す' })).toBeNull();
});

function renderProjectGoal(read: () => Promise<TodoProgressPage>) {
  const bridge = {
    progress: vi.fn(read),
    setGoal: vi.fn(async () => ({ kind: 'ok', result: GOAL })),
    clearGoal: vi.fn(async () => ({ kind: 'ok', result: null })),
    onChanged: () => () => undefined,
  };
  window.electron = { todos: bridge } as unknown as Window['electron'];
  const onClose = vi.fn();
  render(ja(<ProjectGoalDialog projectId="p-sales" projectName="販路開拓" onClose={onClose} />));
  return { bridge, onClose };
}

it('opens a project’s goal form on the goal it has now, and writes that project’s goal', async () => {
  const { bridge, onClose } = renderProjectGoal(async () =>
    page({ goals: [{ kind: 'no_records', goal: GOAL, other_unit_record_ids: [] }] })
  );
  const dialog = await screen.findByRole('dialog', { name: '販路開拓の目標を直す' });
  expect(bridge.progress).toHaveBeenCalledWith(
    expect.objectContaining({ scope: 'all', project: 'project', project_id: 'p-sales' })
  );
  expect(within(dialog).getByLabelText('目標')).toHaveValue('売上');
  expect(within(dialog).getByLabelText('量')).toHaveValue(300_000_000);
  expect(within(dialog).getByLabelText('単位')).toHaveValue('円');
  expect(within(dialog).getByLabelText('期限')).toHaveValue('2027-08-31');

  await userEvent.clear(within(dialog).getByLabelText('量'));
  await userEvent.type(within(dialog).getByLabelText('量'), '400000000');
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(bridge.setGoal).toHaveBeenCalledWith({
    project_id: 'p-sales',
    text: '売上',
    target: 400_000_000,
    unit: '円',
    deadline: '2027-08-31',
  });
  // Closed once the goal saved was read again.
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  expect(bridge.progress).toHaveBeenCalledTimes(2);
});

it('sets a new goal for a project without one', async () => {
  renderProjectGoal(async () => page({ goals: [{ kind: 'no_goal', project_id: 'p-sales' }] }));
  const dialog = await screen.findByRole('dialog', { name: '販路開拓の目標を決める' });
  expect(within(dialog).getByLabelText('目標')).toHaveValue('');
  expect(within(dialog).queryByRole('alert')).toBeNull();
});

it('writes nothing until the goal it has now is read: a failed read offers to read again', async () => {
  let fail = true;
  const { bridge, onClose } = renderProjectGoal(async () => {
    if (fail) throw new Error('backend unavailable');
    return page({ goals: [{ kind: 'no_records', goal: GOAL, other_unit_record_ids: [] }] });
  });
  const failed = await screen.findByRole('dialog', { name: '販路開拓の目標' });
  expect(within(failed).getByRole('alert')).toHaveTextContent(
    'このプロジェクトの今の目標を読み込めませんでした。'
  );
  // No field and no save: what is saved would replace a goal not yet seen.
  expect(within(failed).queryByRole('textbox')).toBeNull();
  expect(within(failed).queryByRole('button', { name: '保存' })).toBeNull();

  fail = false;
  await userEvent.click(within(failed).getByRole('button', { name: 'もう一度読む' }));
  const form = await screen.findByRole('dialog', { name: '販路開拓の目標を直す' });
  expect(within(form).getByLabelText('目標')).toHaveValue('売上');
  expect(within(form).getByLabelText('量')).toHaveValue(300_000_000);
  expect(bridge.setGoal).not.toHaveBeenCalled();

  await userEvent.click(within(form).getByRole('button', { name: 'キャンセル' }));
  expect(onClose).toHaveBeenCalledOnce();
});

it('never lets a write answered after its form closed close the form opened since', async () => {
  let answerFirst: (saved: boolean) => void = () => undefined;
  const onSave = vi
    .fn<(goal: unknown) => Promise<boolean>>()
    .mockImplementationOnce(() => new Promise((resolve) => (answerFirst = resolve)));
  const onClose = vi.fn();
  function Opener() {
    const [open, setOpen] = useState(true);
    return open ? (
      <GoalForm
        projectName="販路開拓"
        goal={GOAL}
        failure={null}
        onSave={onSave}
        onClear={async () => true}
        onClose={() => {
          onClose();
          setOpen(false);
        }}
      />
    ) : (
      <button type="button" onClick={() => setOpen(true)}>
        開く
      </button>
    );
  }
  render(ja(<Opener />));
  await userEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(onSave).toHaveBeenCalledOnce();
  await userEvent.click(screen.getByRole('button', { name: 'キャンセル' }));
  expect(onClose).toHaveBeenCalledOnce();

  await userEvent.click(screen.getByRole('button', { name: '開く' }));
  const reopened = screen.getByRole('dialog', { name: '販路開拓の目標を直す' });
  await userEvent.clear(within(reopened).getByLabelText('単位'));
  await userEvent.type(within(reopened).getByLabelText('単位'), 'ドル');
  await act(async () => answerFirst(true));

  expect(onClose).toHaveBeenCalledOnce();
  expect(screen.getByRole('dialog', { name: '販路開拓の目標を直す' })).toBeInTheDocument();
  expect(within(reopened).getByLabelText('単位')).toHaveValue('ドル');
});
