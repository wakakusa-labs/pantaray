import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type {
  GoalProgress,
  GoalView,
  ProgressPace,
  ProgressRecordView,
  TodoProgressPage,
} from '../../../../electron/src/todos/todoContracts';
import { GoalForm } from './GoalForm';
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
  note: string,
  quantity: number,
  unit: string,
  evidence: ProgressRecordView['evidence'] = { kind: 'user' }
): ProgressRecordView => ({
  record_id,
  todo_id: `todo-${record_id}`,
  project_id: 'p-sales',
  quantity,
  unit,
  note,
  evidence,
  recorded_at: '2026-11-05T12:00:00Z',
});

const RECORDS = [
  record('r1', '丸和商事から受注', 10_000_000, '円'),
  record('r2', '北星から受注', 15_000_000, '円', {
    kind: 'action_steps',
    action_id: 'action-1',
    step_ids: ['s1'],
  }),
  record('r3', '東和から受注', 5_000_000, '円'),
  record('r4', '展示会で名刺交換', 40, '枚'),
];

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
  period: 'week',
  rows: [
    {
      start: '2026-11-02',
      end: '2026-11-08',
      record_ids: ['r1', 'r2'],
      checked: [
        {
          todo_id: 't1',
          project_id: 'p-sales',
          text: '見積書を送る',
          checked_at: '2026-11-03T12:00:00Z',
        },
        {
          todo_id: 't2',
          project_id: null,
          text: '名刺を入れる',
          checked_at: '2026-11-04T12:00:00Z',
        },
      ],
    },
    { start: '2026-10-26', end: '2026-11-01', record_ids: ['r3', 'r4'], checked: [] },
  ],
  goals: [measured()],
  records: RECORDS,
  ...overrides,
});

const NAMES = new Map([
  ['p-sales', '販路開拓'],
  ['p-hiring', '採用'],
]);

function renderView(data: TodoProgressPage, pendingRecordIds: ReadonlySet<string> = new Set()) {
  const handlers = {
    onPeriodChange: vi.fn(),
    onOpenEvidence: vi.fn(),
    onDeleteRecord: vi.fn(),
  };
  cleanup();
  render(
    ja(
      <ProgressView
        page={data}
        projectName={(projectId) => NAMES.get(projectId) ?? projectId}
        pendingRecordIds={pendingRecordIds}
        recordFailure={null}
        {...handlers}
      />
    )
  );
  return handlers;
}

const lead = () => document.querySelector('.todo-progress__lead');
const goalBlock = () => screen.getByRole('region', { name: '販路開拓の目標' });
const disclosure = (name: RegExp) =>
  within(goalBlock()).getByText(name).closest('details') as HTMLDetailsElement;
const notesIn = (details: HTMLElement) =>
  within(details)
    .getAllByRole('listitem')
    .map((item) => item.querySelector('.todo-progress-record__note')?.textContent);

it('builds the lead from the newest period and the one goal, with only the numbers the page has', () => {
  renderView(page());
  expect(lead()).toHaveTextContent(
    '今週は、販路開拓が 2 件進みました。済んだ TODO は 2 件です。' +
      '売上 300,000,000 円の目標に対して、いまは 10% です。' +
      'このペースなら 2027 年 6 月のはじめに届きます。'
  );

  const block = goalBlock();
  expect(block).toHaveTextContent('2027/8/31 までに 売上 300,000,000 円。10/1 に決めました。');
  expect(block).toHaveTextContent('いま 10%（30,000,000 円）');
  expect(block).toHaveTextContent('期限から逆算した今の位置 11%');
  expect(block).toHaveTextContent('目標 300,000,000 円');
  expect(block).toHaveTextContent('10% は、10/1 からの記録 3 件、あわせて 30,000,000 円です。');
  expect(block).toHaveTextContent(
    '10/11 からの記録 3 件で、1 日あたり 1,071,429 円ずつ進んでいます。' +
      'このペースなら 2027 年 6 月のはじめに届きます。'
  );
});

it('lists exactly the records behind the number, and names other-unit records without summing them', () => {
  renderView(page());
  expect(notesIn(disclosure(/^この数字のもとになった記録（3 件）/))).toEqual([
    '丸和商事から受注',
    '北星から受注',
    '東和から受注',
  ]);
  expect(goalBlock()).toHaveTextContent('単位が「枚」の記録 1 件は、合計に入れていません。');
  expect(notesIn(disclosure(/^合計に入れていない記録（1 件）/))).toEqual(['展示会で名刺交換']);
  // The 40 枚 is nowhere in the total.
  expect(goalBlock()).not.toHaveTextContent('30,000,040');
});

it('says who wrote each record, opens the conversation behind an AI record, and deletes one', async () => {
  const handlers = renderView(page(), new Set(['r3']));
  const counted = disclosure(/^この数字のもとになった記録/);
  const [byUser, byAi, pending] = within(counted).getAllByRole('listitem');
  expect(byUser).toHaveTextContent('あなたが記録');
  expect(within(byUser).queryByRole('button', { name: /^会話を開く/ })).toBeNull();
  expect(byAi).toHaveTextContent('Pantaray が会話の中の結果から記録');
  expect(byUser).toHaveTextContent('10,000,000 円');
  expect(byUser).toHaveTextContent('11/5');

  await userEvent.click(within(byAi).getByRole('button', { name: '会話を開く: 北星から受注' }));
  expect(handlers.onOpenEvidence).toHaveBeenCalledWith('action-1');
  await userEvent.click(within(byUser).getByRole('button', { name: '削除: 丸和商事から受注' }));
  expect(handlers.onDeleteRecord).toHaveBeenCalledWith('r1');
  expect(within(pending).getByRole('button', { name: '削除: 東和から受注' })).toBeDisabled();
});

it('tells each pace without a number it does not have', () => {
  const tooFew: ProgressPace = {
    kind: 'too_few_records',
    window_start: '2026-10-11',
    record_ids: ['r1'],
  };
  renderView(page({ goals: [measured({ pace: tooFew })] }));
  expect(goalBlock()).toHaveTextContent('記録がまだ少ないので、届く時期は出していません。');
  expect(goalBlock()).not.toHaveTextContent('1 日あたり');
  expect(lead()).not.toHaveTextContent('このペースなら');

  renderView(
    page({
      goals: [
        measured({
          pace: {
            kind: 'beyond_horizon',
            window_start: '2026-10-11',
            record_ids: ['r1', 'r2', 'r3'],
            per_day: 0.5,
          },
        }),
      ],
    })
  );
  expect(goalBlock()).toHaveTextContent(
    '10/11 からの記録 3 件で、1 日あたり 0.5 円ずつ進んでいます。このペースでは 100 年以内に届きません。'
  );
  expect(goalBlock()).not.toHaveTextContent('届きます');

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
  expect(goalBlock()).toHaveTextContent('いま 120%（360,000,000 円）');
  expect(goalBlock()).toHaveTextContent('目標に届きました。');
  // A reached goal is not late, whatever the date.
  expect(goalBlock()).not.toHaveTextContent('過ぎています');

  renderView(page({ goals: [measured({ deadline_passed: true })] }));
  expect(goalBlock()).toHaveTextContent('期限の 2027/8/31 を過ぎています。');
});

it('shows no number for a goal without records, or a project without a goal', () => {
  renderView(page({ goals: [{ kind: 'no_records', goal: GOAL, other_unit_record_ids: [] }] }));
  expect(goalBlock()).toHaveTextContent('まだ記録がありません。');
  expect(goalBlock()).not.toHaveTextContent('%');
  expect(goalBlock().querySelector('details')).toBeNull();
  expect(lead()).toHaveTextContent('売上 300,000,000 円の目標には、まだ記録がありません。');

  renderView(page({ goals: [{ kind: 'no_records', goal: GOAL, other_unit_record_ids: ['r4'] }] }));
  expect(goalBlock()).toHaveTextContent('まだ「円」の記録がありません。');
  expect(goalBlock()).toHaveTextContent('単位が「枚」の記録 1 件は、合計に入れていません。');
  expect(goalBlock()).not.toHaveTextContent('あわせて');

  renderView(page({ goals: [{ kind: 'no_goal', project_id: 'p-sales' }] }));
  expect(goalBlock()).toHaveTextContent('まだ目標がありません。');
  expect(goalBlock()).not.toHaveTextContent(/\d/);
  expect(lead()).not.toHaveTextContent('目標');

  renderView(page({ goals: [] }));
  expect(screen.getByRole('heading', { name: '目標' }).parentElement).toHaveTextContent(
    'まだ目標がありません。'
  );
});

it('leaves the goals out of the lead when several speak, and says when the period has no records', () => {
  const hiring = measured({
    goal: { ...GOAL, project_id: 'p-hiring', text: '採用', target: 5, unit: '人' },
  });
  renderView(
    page({
      goals: [measured(), hiring],
      rows: [{ start: '2026-11-02', end: '2026-11-08', record_ids: [], checked: [] }],
    })
  );
  expect(lead()).toHaveTextContent(/^今週は、まだ記録がありません。$/);
  expect(screen.getByRole('region', { name: '採用の目標' })).toBeInTheDocument();
});

it('marks the period in view as pressed and asks for another', async () => {
  const handlers = renderView(page());
  const group = screen.getByRole('group', { name: '区切り' });
  expect(within(group).getByRole('button', { name: '週ごと' })).toHaveAttribute(
    'aria-pressed',
    'true'
  );
  expect(within(group).getByRole('button', { name: '日ごと' })).toHaveAttribute(
    'aria-pressed',
    'false'
  );
  await userEvent.click(within(group).getByRole('button', { name: '月ごと' }));
  expect(handlers.onPeriodChange).toHaveBeenCalledWith('month');
});

it('gives each period its records by project, its checked count and the checked TODOs as written', () => {
  renderView(page());
  const rows = screen.getByRole('region', { name: '週ごと' }).querySelectorAll(':scope > ul > li');
  expect(rows[0]).toHaveTextContent('11/2 の週');
  expect(rows[0]).toHaveTextContent('販路開拓 +2');
  expect(rows[0]).toHaveTextContent('済み 2');
  expect(rows[0]).toHaveTextContent('見積書を送る、名刺を入れる');
  // The bar is against the busiest row: both have 2 records.
  expect(rows[1].querySelector('i')).toHaveStyle({ width: '100%' });
  expect(rows[1]).toHaveTextContent('済み 0');

  renderView(
    page({
      period: 'month',
      rows: [
        { start: '2026-11-01', end: '2026-11-30', record_ids: ['r1'], checked: [] },
        { start: '2026-10-01', end: '2026-10-31', record_ids: [], checked: [] },
      ],
    })
  );
  const months = screen
    .getByRole('region', { name: '月ごと' })
    .querySelectorAll(':scope > ul > li');
  expect(months[0]).toHaveTextContent('11 月（11/7 まで）');
  expect(lead()).toHaveTextContent(/^今月は、販路開拓が 1 件進みました。/);
  expect(months[1]).toHaveTextContent('10 月');
  expect(months[1]).toHaveTextContent('記録なし');
  expect(months[1].querySelector('i')).toHaveStyle({ width: '0%' });

  renderView(
    page({
      period: 'day',
      rows: [{ start: '2026-11-07', end: '2026-11-07', record_ids: [], checked: [] }],
    })
  );
  expect(screen.getByRole('region', { name: '日ごと' })).toHaveTextContent('11/7（土）');
  expect(lead()).toHaveTextContent(/^今日（11\/7）は、まだ記録がありません。/);
});

it("opens a period's records and deletes one, with no goal to count them toward", async () => {
  const handlers = renderView(page({ goals: [] }));
  const [current] = screen
    .getByRole('region', { name: '週ごと' })
    .querySelectorAll<HTMLElement>(':scope > ul > li');
  const records = within(current)
    .getByText('この期間の記録（2 件）')
    .closest('details') as HTMLElement;
  expect(notesIn(records)).toEqual(['丸和商事から受注', '北星から受注']);
  await userEvent.click(within(records).getByRole('button', { name: '会話を開く: 北星から受注' }));
  expect(handlers.onOpenEvidence).toHaveBeenCalledWith('action-1');
  await userEvent.click(within(records).getByRole('button', { name: '削除: 北星から受注' }));
  expect(handlers.onDeleteRecord).toHaveBeenCalledWith('r2');
});

it('never writes a positive share or quantity as 0, and never rounds a share up to 100%', () => {
  const small = { ...record('r1', '一部入金', 1_000, '円'), recorded_at: '2026-11-05T12:00:00Z' };
  const tiny = record('r2', '打ち合わせ', 0.001, '時間');
  renderView(
    page({
      records: [small, tiny],
      rows: [{ start: '2026-11-02', end: '2026-11-08', record_ids: ['r1', 'r2'], checked: [] }],
      goals: [
        measured({
          total: 1_000,
          share: 1_000 / 300_000_000,
          record_ids: ['r1'],
          other_unit_record_ids: ['r2'],
          expected_share: 0.996,
          pace: { kind: 'too_few_records', window_start: '2026-10-11', record_ids: ['r1'] },
        }),
      ],
    })
  );
  expect(goalBlock()).toHaveTextContent('いま 0.0003%（1,000 円）');
  expect(goalBlock()).toHaveTextContent('期限から逆算した今の位置 99%');
  expect(lead()).toHaveTextContent('いまは 0.0003% です。');
  expect(notesIn(disclosure(/^合計に入れていない記録/))).toEqual(['打ち合わせ']);
  expect(disclosure(/^合計に入れていない記録/)).toHaveTextContent('0.001 時間');

  renderView(
    page({
      goals: [
        measured({
          pace: {
            kind: 'beyond_horizon',
            window_start: '2026-10-11',
            record_ids: ['r1', 'r2', 'r3'],
            per_day: 0.0004,
          },
        }),
      ],
    })
  );
  expect(goalBlock()).toHaveTextContent('1 日あたり 0.0004 円ずつ');
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
