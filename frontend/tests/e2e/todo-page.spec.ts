import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import type { OverlaySnapshot } from '../../src/components/agent-overlay/model/overlayTypes';
import type {
  DeletedTodoView,
  GoalProgress,
  GoalView,
  ProgressPeriodView,
  ProgressRecordView,
  TodoGoalRequest,
  TodoProgressPage,
  TodoProgressQuery,
  TodoProjectRefRequest,
  TodoScopeQuery,
  TodoView,
  TodoWeekPage,
  TodoWeekQuery,
} from '../../electron/src/todos/todoContracts';
import { waitForAnimationsToSettle } from './animations';
import { installTaskDrafts } from './taskDrafts';

// The main window's home page (the TODO list) from the production entry, with deterministic IPC
// inputs: no Electron, no backend.
let vite: ViteDevServer;
let baseUrl: string;
test.beforeAll(async () => {
  vite = await createServer({ server: { host: '127.0.0.1', port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls!.local[0];
});
test.afterAll(async () => vite.close());
test.use({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });

const ORGANIZATIONS = [
  { organization_id: 'xer', display_name: 'Xer' },
  { organization_id: 'wakakusa', display_name: 'Wakakusa' },
];
const PROJECTS = [
  { project_id: 'estimate', display_name: '見積もり', sort_order: 0, organization_ids: ['xer'] },
  { project_id: 'hiring', display_name: '採用', sort_order: 1, organization_ids: ['xer'] },
  {
    project_id: 'product',
    display_name: 'プロダクト',
    sort_order: 2,
    organization_ids: ['wakakusa'],
  },
  { project_id: 'home', display_name: '家のこと', sort_order: 3, organization_ids: [] },
];

function todo(todoId: string, projectId: string | null, text: string, extra: Partial<TodoView>) {
  return {
    todo_id: todoId,
    project_id: projectId,
    text,
    author: 'user',
    origin: 'written',
    week_start: '2026-11-02',
    carried: false,
    due: null,
    state: { kind: 'working', note: null },
    deletion_request: null,
    conversation: { kind: 'none' },
    ...extra,
  } satisfies TodoView;
}

const ITEMS: TodoView[] = [
  todo('t-resubmit', 'estimate', '見積書を再提出する', {
    author: 'ai',
    origin: 'suggestion',
    carried: true,
    week_start: '2026-10-26',
    due: { date: '2026-11-06', repeat: 'none' },
    state: { kind: 'partial', note: '数量と納期は直しました' },
    conversation: { kind: 'action', action_id: 'action-resubmit' },
  }),
  todo('t-cards', 'estimate', '名刺 40 枚を入れておく', {
    state: { kind: 'done', by: 'user', at: '2026-11-03T02:00:00.000Z' },
  }),
  todo('t-questions', 'hiring', '来週の面接の質問リストを作る', {
    author: 'ai',
    origin: 'suggestion',
    state: { kind: 'waiting', reason: 'approval', note: null },
    conversation: { kind: 'suggestion', suggestion_id: 'suggestion-questions' },
  }),
  todo('t-notes', 'product', 'リリースノートの下書きを書く', {
    due: { date: '2026-11-05', repeat: 'none' },
    state: { kind: 'working', note: '変更点を集めています' },
    conversation: { kind: 'action', action_id: 'action-notes' },
  }),
  todo('t-branches', 'product', '古いブランチを消す', {
    state: { kind: 'partial', note: null },
    deletion_request: {
      request_id: 'request-branches',
      reason: 'どれも main に取り込み済みで、残す理由が見当たりません。',
    },
    conversation: { kind: 'action', action_id: 'action-branches' },
  }),
  todo('t-garbage', 'home', '粗大ごみの申し込み', {
    due: { date: '2026-11-07', repeat: 'weekly' },
    origin: 'recurrence',
    state: { kind: 'waiting', reason: 'reply', note: '回収日を選んでください' },
    conversation: { kind: 'action', action_id: 'action-garbage' },
  }),
  todo('t-tax', null, 'ふるさと納税の上限を調べる', {
    author: 'ai',
    state: { kind: 'done', by: 'ai', at: '2026-11-04T01:00:00.000Z' },
    conversation: { kind: 'action', action_id: 'action-tax' },
  }),
];

const DELETED: DeletedTodoView[] = [
  {
    todo_id: 'd-old-estimate',
    project_id: 'estimate',
    text: '旧版の見積書を整理する',
    deleted_by: 'user',
    deleted_at: '2026-10-29T03:00:00.000Z',
    purge_on: '2026-11-28',
  },
  {
    todo_id: 'd-slack',
    project_id: null,
    text: 'Slack の通知設定を見直す',
    deleted_by: 'ai_approved_by_user',
    deleted_at: '2026-10-07T03:00:00.000Z',
    purge_on: '2026-11-05',
  },
];

/** 見積もり's goal; the other projects start without one. */
const GOALS: GoalView[] = [
  {
    project_id: 'estimate',
    text: '受注',
    target: 10,
    unit: '件',
    deadline: '2026-12-25',
    set_at: '2026-10-01T03:00:00.000Z',
  },
];

const progressRecord = (
  recordId: string,
  todoId: string,
  projectId: string | null,
  quantity: number,
  unit: string,
  note: string,
  recordedAt: string,
  evidence: ProgressRecordView['evidence']
): ProgressRecordView => ({
  record_id: recordId,
  todo_id: todoId,
  project_id: projectId,
  quantity,
  unit,
  note,
  evidence,
  recorded_at: recordedAt,
});

const RECORDS: ProgressRecordView[] = [
  progressRecord(
    'r-maruwa',
    't-resubmit',
    'estimate',
    1,
    '件',
    '丸和商事から受注',
    '2026-11-03T03:00:00.000Z',
    { kind: 'action_steps', action_id: 'action-resubmit', step_ids: ['step-1'] }
  ),
  progressRecord(
    'r-hokusei',
    't-cards',
    'estimate',
    2,
    '件',
    '北星と東和から受注',
    '2026-10-28T03:00:00.000Z',
    { kind: 'user' }
  ),
  progressRecord(
    'r-yamato',
    't-resubmit',
    'estimate',
    1,
    '件',
    '大和製作所から受注',
    '2026-10-14T03:00:00.000Z',
    { kind: 'action_steps', action_id: 'action-resubmit', step_ids: ['step-2'] }
  ),
  progressRecord(
    'r-notes',
    't-notes',
    'product',
    1,
    '本',
    '10 月分のリリースノートを公開',
    '2026-11-02T03:00:00.000Z',
    { kind: 'action_steps', action_id: 'action-notes', step_ids: ['step-3'] }
  ),
];

const SUGGESTION: OverlaySnapshot = {
  suggestionId: 'suggestion-questions',
  commandId: 'command-1',
  interactionContract: 'action_offer',
  suggestionText:
    '来週の面接に向けて、職務経歴書と募集要項から質問リストの下書きを作っておきましょうか。',
  reactionState: null,
  reactionTimestamp: null,
  actionPhase: 'idle',
  actionStatus: null,
  actionErrorCode: null,
  actionFailureStage: null,
  actionFailureMessagePublic: null,
  processId: null,
  actionId: null,
  updatedAt: '2026-11-04T00:00:00Z',
  lastSequence: 1,
  isLive: true,
};

async function openHome(page: Page, search = '') {
  await page.addInitScript(
    ({ organizations, projects, items, deleted, suggestion, initialGoals, records }) => {
      localStorage.setItem('pantaray_ui_language', 'ja');
      const noop = () => {};
      const CURRENT = '2026-11-02';
      const shift = (date: string, days: number) =>
        new Date(Date.parse(`${date}T00:00:00Z`) + days * 86_400_000).toISOString().slice(0, 10);
      // The backend's scope: a TODO belongs to its project's organization.
      const inScope = (projectId: string | null, query: TodoScopeQuery) => {
        if (query.scope === 'all') return true;
        const ids = projects.find((p) => p.project_id === projectId)?.organization_ids ?? [];
        return query.scope === 'no_organization'
          ? ids.length === 0
          : ids.includes(query.organization_id!);
      };
      const TODAY = '2026-11-04';
      // Goals the form writes stay for the page's later reads, as the backend's do.
      const goals = new Map(initialGoals.map((goal) => [goal.project_id, goal]));
      const inFilter = (projectId: string | null, query: TodoProgressQuery) =>
        query.project === 'any' ||
        (query.project === 'no_project' ? projectId === null : projectId === query.project_id);
      // The rows up to today, newest first: 7 days, 4 weeks or 2 months.
      const rowBounds = (period: TodoProgressQuery['period']) => {
        if (period === 'day')
          return Array.from({ length: 7 }, (_, i) => ({
            start: shift(TODAY, -i),
            end: shift(TODAY, -i),
          }));
        if (period === 'week')
          return Array.from({ length: 4 }, (_, i) => ({
            start: shift(CURRENT, -7 * i),
            end: shift(CURRENT, 6 - 7 * i),
          }));
        return [
          { start: '2026-11-01', end: '2026-11-30' },
          { start: '2026-10-01', end: '2026-10-31' },
        ];
      };
      const goalProgress = (projectId: string, shown: ProgressRecordView[]): GoalProgress => {
        const goal = goals.get(projectId);
        if (!goal) return { kind: 'no_goal', project_id: projectId };
        const counted = shown.filter(
          (record) => record.project_id === projectId && record.unit === goal.unit
        );
        if (counted.length === 0) return { kind: 'no_records', goal, other_unit_record_ids: [] };
        const total = counted.reduce((sum, record) => sum + record.quantity, 0);
        return {
          kind: 'measured',
          goal,
          total,
          share: total / goal.target,
          record_ids: counted.map((record) => record.record_id),
          other_unit_record_ids: [],
          expected_share: 0.4,
          deadline_passed: false,
          pace: {
            kind: 'projected',
            window_start: '2026-10-07',
            record_ids: counted.map((record) => record.record_id),
            per_day: 0.14,
            projected_on: '2026-12-15',
          },
        };
      };
      const progressPage = (query: TodoProgressQuery): TodoProgressPage => {
        const shown = records.filter(
          (record) => inScope(record.project_id, query) && inFilter(record.project_id, query)
        );
        const rows: ProgressPeriodView[] = rowBounds(query.period).map(({ start, end }) => {
          const within = (at: string) => at.slice(0, 10) >= start && at.slice(0, 10) <= end;
          return {
            start,
            end,
            record_ids: shown
              .filter((record) => within(record.recorded_at))
              .map((record) => record.record_id),
            checked: items.flatMap((item) =>
              item.state.kind === 'done' &&
              within(item.state.at) &&
              inScope(item.project_id, query) &&
              inFilter(item.project_id, query)
                ? [
                    {
                      todo_id: item.todo_id,
                      project_id: item.project_id,
                      text: item.text,
                      checked_at: item.state.at,
                    },
                  ]
                : []
            ),
          };
        });
        // One project's goal with or without one; otherwise each project in scope with one.
        const goalProjects =
          query.project === 'project'
            ? [query.project_id!]
            : query.project === 'any'
              ? projects
                  .filter((p) => goals.has(p.project_id) && inScope(p.project_id, query))
                  .map((p) => p.project_id)
              : [];
        return {
          today: TODAY,
          period: query.period,
          rows,
          goals: goalProjects.map((projectId) => goalProgress(projectId, shown)),
          records: shown,
        };
      };
      Object.defineProperty(window, 'electron', {
        value: {
          ipcRenderer: { on: () => noop, send: noop },
          process: { platform: 'darwin', env: { NODE_ENV: 'test' } },
          workspaceSettings: {
            get: async () => ({
              read_access_scope: 'workspace',
              organizations,
              projects,
              folders: [],
            }),
          },
          auth: {
            getState: async () => ({
              authStatus: 'authenticated',
              isLoggedIn: true,
              user: { id: 'user-1', email: 'e2e@example.com' },
              runtimeState: {
                status: 'ready',
                message: null,
                owner: { id: 'user-1', kind: 'account' },
              },
            }),
            onStateChanged: () => noop,
          },
          onboarding: { getState: async () => ({ status: 'completed' }) },
          aiConnection: { getState: () => new Promise(() => {}), onChanged: () => noop },
          shortcut: { getState: async () => ({ accelerator: 'Alt+Space', failure: null }) },
          approval: {
            getWorkspaceEditCommandPreference: async () => ({ approval_mode: 'prompt_each_time' }),
          },
          chat: {
            listItems: async () => ({ items: [], next_cursor: null }),
            onItemAppended: () => noop,
            onTurnState: () => noop,
            getTurnState: async () => null,
          },
          orchestration: { onStatus: () => noop, onEvent: () => noop },
          history: {
            fetch: async () => ({
              data: [
                {
                  kind: 'conversation',
                  action_id: 'action-research',
                  title: '競合 3 社の料金ページを調べる',
                  updated_at: new Date().toISOString(),
                  status: 'idle',
                  latest_completion_event_id: null,
                  status_line: null,
                  artifact_names: [],
                },
              ],
              nextCursor: null,
              unreadActionIds: [],
              error: null,
              errorCode: null,
            }),
            readItem: async () => null,
            onChanged: () => noop,
            openNewConversation: async () => undefined,
            deleteItem: async () => ({ ok: true }),
          },
          todos: {
            listWeek: async (query: TodoWeekQuery): Promise<TodoWeekPage> => {
              const start = query.week_start ?? CURRENT;
              // An ended week holds what was done in it; a week ahead holds nothing yet.
              const shown =
                start === CURRENT
                  ? items
                  : start < CURRENT
                    ? items.filter((item) => item.todo_id === 't-cards')
                    : [];
              return {
                today: '2026-11-04',
                current_week_start: CURRENT,
                week: { start, end: shift(start, 6) },
                items: shown.filter((item) => inScope(item.project_id, query)),
              };
            },
            listDeleted: async (query: TodoScopeQuery) =>
              deleted.filter((item) => inScope(item.project_id, query)),
            progress: async (query: TodoProgressQuery) => progressPage(query),
            setGoal: async ({ project_id, ...goal }: TodoGoalRequest) => {
              const saved = {
                ...goal,
                project_id,
                set_at: goals.get(project_id)?.set_at ?? '2026-11-04T03:00:00.000Z',
              };
              goals.set(project_id, saved);
              return { kind: 'ok', result: saved };
            },
            clearGoal: async ({ project_id }: TodoProjectRefRequest) => {
              goals.delete(project_id);
              return { kind: 'ok', result: null };
            },
            deleteProgressRecord: async () => undefined,
            onChanged: () => noop,
          },
          suggestions: {
            read: async ({ suggestionId }: { suggestionId: string }) =>
              suggestionId === suggestion.suggestionId ? suggestion : new Promise(() => {}),
            onSnapshot: () => noop,
          },
          agentOverlay: {
            getActionApprovalMode: async () => ({ approval_mode: 'prompt_each_time' }),
          },
          actions: {
            // The Action panes' reads stay pending: these tests look at what opens them.
            openConversation: () => new Promise(() => {}),
            onConversationUpdated: () => noop,
            attachFile: async () => ({ kind: 'rejected' }),
            attachImage: async () => ({ kind: 'rejected', reason: 'failed' }),
            discardAttachment: async () => undefined,
          },
        },
      });
    },
    {
      organizations: ORGANIZATIONS,
      projects: PROJECTS,
      items: ITEMS,
      deleted: DELETED,
      suggestion: SUGGESTION,
      initialGoals: GOALS,
      records: RECORDS,
    }
  );
  await installTaskDrafts(page);
  await page.goto(`${baseUrl}#/history${search}`);
}

async function capture(page: Page, info: TestInfo, name: string) {
  for (const scheme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await waitForAnimationsToSettle(page);
    await page.screenshot({ path: info.outputPath(`${name}-${scheme}.png`) });
  }
}

const sidebarOf = (page: Page) => page.getByRole('complementary', { name: 'TODO と会話' });
const railOf = (page: Page) => page.getByRole('group', { name: '組織' });
const groups = (page: Page) => page.locator('section.todo-group');
const hasNoSideScroll = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

test('the home lists every organization’s week by project, with the rail and the sidebar', async ({
  page,
}, info) => {
  await openHome(page);
  await expect(page.getByRole('heading', { level: 1, name: 'すべての TODO' })).toBeVisible();

  // The rail: the organizations in place of Home, then Settings.
  const rail = railOf(page);
  await expect(rail.getByRole('button')).toHaveText(['', 'X', 'W', 'なし']);
  await expect(rail.getByRole('button', { name: 'すべての組織' })).toHaveAttribute(
    'aria-pressed',
    'true'
  );
  await expect(rail.getByRole('button', { name: 'Xer' })).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByRole('navigation').getByRole('button', { name: '設定' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'ホーム' })).toHaveCount(0);

  // Groups by organization, then the projects in none, then the TODOs in no project.
  await expect(groups(page).locator('.todo-group__head')).toHaveText([
    /^Xer ›見積もり1 \/ 2 済み$/,
    /^Xer ›採用0 \/ 1 済み$/,
    /^Wakakusa ›プロダクト0 \/ 2 済み$/,
    /^組織なし ›家のこと0 \/ 1 済み$/,
    /^組織なし ›プロジェクトなしすべて済み$/,
  ]);
  const estimate = page.getByRole('region', { name: 'Xer 見積もり' });
  await expect(
    estimate.getByRole('checkbox', { name: '完了: 名刺 40 枚を入れておく' })
  ).toBeChecked();
  await expect(estimate.getByRole('button', { name: /^見積書を再提出する/ })).toContainText(
    '先週から持ち越し'
  );
  await expect(
    estimate.getByRole('textbox', { name: 'TODO を書く（Enter で追加）' })
  ).toBeVisible();

  // Pantaray's request to delete waits for the user's 消す or 残す.
  const request = page.getByRole('group', {
    name: 'Pantaray がこの TODO を消したいと言っています',
  });
  await expect(request).toContainText('どれも main に取り込み済みで');
  await expect(request.getByRole('button', { name: '消す' })).toBeVisible();
  await expect(request.getByRole('button', { name: '残す' })).toBeVisible();

  // The sidebar: all TODOs (current), the projects with their open counts, deleted, chat, others.
  const sidebar = sidebarOf(page);
  await expect(sidebar.getByRole('button', { name: 'すべての TODO' })).toHaveAttribute(
    'aria-current',
    'true'
  );
  await expect(
    sidebar.getByRole('button', { name: 'プロダクト', exact: true })
  ).toHaveAccessibleDescription('未完了 2 件');
  await expect(sidebar.getByRole('button', { name: 'プロダクト のフォルダ' })).toHaveAttribute(
    'aria-expanded',
    'false'
  );
  await expect(sidebar.getByRole('button', { name: '削除した TODO' })).toBeVisible();
  await expect(sidebar.getByRole('button', { name: 'チャット', exact: true })).toBeVisible();
  await expect(sidebar.getByRole('heading', { level: 2, name: 'そのほかの会話' })).toBeVisible();
  await expect(
    sidebar.getByRole('button', { name: /^競合 3 社の料金ページを調べる/ })
  ).toBeVisible();
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'week-all');

  // The keyboard reaches a row's check and shows where it is.
  await estimate.getByRole('checkbox', { name: '完了: 見積書を再提出する' }).focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  await expect(estimate.getByRole('checkbox', { name: '完了: 見積書を再提出する' })).toBeFocused();
  await capture(page, info, 'week-focus');
});

test('a selected TODO opens its conversation beside the list, with 承認 and no 見送る', async ({
  page,
}, info) => {
  await openHome(page);
  const row = page.getByRole('button', { name: /^来週の面接の質問リストを作る/ });
  await row.click();
  await expect(page).toHaveURL(/#\/history\?todo=t-questions$/);
  await expect(row).toHaveAttribute('aria-current', 'true');
  const pane = page.getByRole('region', { name: '来週の面接の質問リストを作る' });
  await expect(pane.getByText(/職務経歴書と募集要項から/)).toBeVisible();
  await expect(pane.getByRole('button', { name: '承認' })).toBeEnabled();
  await expect(pane.getByRole('button', { name: '見送る' })).toHaveCount(0);
  // A TODO's conversation is not in the dated list, so no History delete is offered for it.
  await expect(
    sidebarOf(page).getByRole('button', { name: /来週の面接の質問リストを作る/ })
  ).toHaveCount(0);
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'conversation');

  // An Action's TODO opens its task pane; one with no conversation yet says so.
  await page.getByRole('button', { name: /^リリースノートの下書きを書く/ }).click();
  await expect(page.getByRole('region', { name: 'リリースノートの下書きを書く' })).toBeVisible();

  await page.getByRole('button', { name: '会話を閉じる' }).click();
  await expect(page).toHaveURL(/#\/history$/);
  await expect(page.getByRole('heading', { level: 1, name: 'すべての TODO' })).toBeFocused();
});

test('the rail scopes the week to one organization, a project filters it, and 今週へ keeps focus', async ({
  page,
}, info) => {
  await openHome(page);
  await railOf(page).getByRole('button', { name: 'Xer' }).click();
  await expect(page).toHaveURL(/#\/history\?scope=organization:xer$/);
  await expect(railOf(page).getByRole('button', { name: 'Xer' })).toHaveAttribute(
    'aria-pressed',
    'true'
  );
  await expect(groups(page).locator('h3')).toHaveText(['見積もり', '採用']);
  await capture(page, info, 'week-organization');

  await sidebarOf(page).getByRole('button', { name: '見積もり', exact: true }).click();
  await expect(page).toHaveURL(/#\/history\?scope=organization:xer&project=project:estimate$/);
  await expect(page.getByRole('heading', { level: 1, name: '見積もり' })).toBeVisible();
  await expect(groups(page).locator('h3')).toHaveText(['見積もり']);

  // An ended week shows what was done in it, takes no new TODOs, and offers 今週へ.
  await page.getByRole('button', { name: '前の週' }).click();
  await expect(page.getByText('10月26日(月)〜11月1日(日)')).toBeVisible();
  await expect(page.getByRole('textbox', { name: /TODO を書く/ })).toHaveCount(0);
  const thisWeek = page.getByRole('button', { name: '今週へ' });
  await capture(page, info, 'week-past');
  await thisWeek.focus();
  await page.keyboard.press('Enter');
  await expect(thisWeek).toHaveCount(0);
  await expect(page.getByRole('heading', { level: 1, name: '見積もり' })).toBeFocused();
  await expect(page.getByText('11月2日(月)〜11月8日(日)')).toBeVisible();
});

test('the deleted TODOs list what can still be restored', async ({ page }, info) => {
  await openHome(page);
  await sidebarOf(page).getByRole('button', { name: '削除した TODO' }).click();
  await expect(page).toHaveURL(/#\/history\?view=deleted$/);
  await expect(page.getByRole('heading', { level: 1, name: '削除した TODO' })).toBeVisible();
  await expect(sidebarOf(page).getByRole('button', { name: '削除した TODO' })).toHaveAttribute(
    'aria-current',
    'true'
  );
  await expect(page.getByRole('button', { name: '戻す: 旧版の見積書を整理する' })).toBeVisible();
  await expect(page.getByText('あと 1 日で完全に消えます')).toBeVisible();
  await capture(page, info, 'deleted');
});

test('a narrow window stacks the conversation under the list header, with nothing scrolling sideways', async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 900, height: 820 });
  await openHome(page);
  await expect(page.getByRole('heading', { level: 1, name: 'すべての TODO' })).toBeVisible();
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'narrow-week');

  await page.getByRole('button', { name: /^来週の面接の質問リストを作る/ }).click();
  const pane = page.getByRole('region', { name: '来週の面接の質問リストを作る' });
  await expect(pane.getByRole('button', { name: '承認' })).toBeVisible();
  // The list makes room; its close button brings it back.
  await expect(page.getByRole('checkbox', { name: '完了: 名刺 40 枚を入れておく' })).toBeHidden();
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'narrow-conversation');
  await page.getByRole('button', { name: '会話を閉じる' }).click();
  await expect(page.getByRole('checkbox', { name: '完了: 名刺 40 枚を入れておく' })).toBeVisible();
});

test('進み具合 shows the progress in view, keeps its period in the address, and opens where a number came from', async ({
  page,
}, info) => {
  await openHome(page);
  const sidebar = sidebarOf(page);
  await sidebar.getByRole('button', { name: '見積もり', exact: true }).click();
  await sidebar.getByRole('button', { name: '進み具合' }).click();
  await expect(page).toHaveURL(/#\/history\?project=project:estimate&view=progress$/);
  await expect(sidebar.getByRole('button', { name: '進み具合' })).toHaveAttribute(
    'aria-current',
    'true'
  );
  // The progress is of the project in view, which stays marked.
  await expect(sidebar.getByRole('button', { name: '見積もり', exact: true })).toHaveAttribute(
    'aria-current',
    'true'
  );
  await expect(page.getByRole('heading', { level: 1, name: '見積もり' })).toBeVisible();

  const goal = page.getByRole('region', { name: '見積もりの目標' });
  await expect(goal).toContainText('12/25 までに 受注 10 件。10/1 に決めました。');
  await expect(goal).toContainText('いま 40%（4 件）');
  await expect(goal).toContainText('このペースなら 2026 年 12 月のなかばに届きます。');
  await expect(page.locator('.todo-progress__lead')).toHaveText(
    '今週は、見積もりが 1 件進みました。済んだ TODO は 1 件です。受注 10 件の目標に対して、いまは 40% です。このペースなら 2026 年 12 月のなかばに届きます。'
  );

  // A period's count opens onto its records, each with who wrote it.
  const rows = page.getByRole('region', { name: '週ごと' }).getByRole('listitem');
  const lastWeek = rows.filter({ hasText: '10/26 の週' }).first();
  await expect(lastWeek).toContainText('見積もり +1');
  await lastWeek.getByText('この期間の記録（1 件）').click();
  await expect(lastWeek.getByText('北星と東和から受注')).toBeVisible();
  await expect(lastWeek).toContainText('あなたが記録');
  const thisWeek = rows.filter({ hasText: '11/2 の週' }).first();
  await thisWeek.getByText('この期間の記録（1 件）').click();
  await expect(thisWeek).toContainText('Pantaray が会話の中の結果から記録');
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'progress-week');

  // The period is the address's: a reload keeps it.
  await page.getByRole('button', { name: '月ごと' }).click();
  await expect(page).toHaveURL(/#\/history\?project=project:estimate&view=progress&period=month$/);
  await expect(page.getByRole('region', { name: '月ごと' })).toContainText('11 月（11/4 まで）');
  await page.reload();
  await expect(page.getByRole('button', { name: '月ごと' })).toHaveAttribute(
    'aria-pressed',
    'true'
  );

  // The record's conversation opens as any conversation does, and back returns to the progress.
  const current = page.getByRole('region', { name: '月ごと' }).getByRole('listitem').first();
  await current.getByText('この期間の記録（1 件）').click();
  await current.getByRole('button', { name: '会話を開く: 丸和商事から受注' }).click();
  await expect(page).toHaveURL(/#\/history\?item=action:action-resubmit$/);
  await page.goBack();
  await expect(page).toHaveURL(/#\/history\?project=project:estimate&view=progress&period=month$/);
  await expect(page.getByRole('region', { name: '見積もりの目標' })).toBeVisible();

  // Every organization's progress, with each goal there is.
  await sidebar.getByRole('button', { name: 'すべての TODO' }).click();
  await sidebar.getByRole('button', { name: '進み具合' }).click();
  await expect(page).toHaveURL(/#\/history\?view=progress$/);
  await expect(page.locator('.todo-progress__lead')).toContainText(
    '今週は、見積もりが 1 件、プロダクトが 1 件進みました。'
  );
  await capture(page, info, 'progress-all');
});

test('a project’s goal is set from its menu or from its progress, opening on the goal it has', async ({
  page,
}, info) => {
  await openHome(page);
  const sidebar = sidebarOf(page);
  const menu = (name: string) => sidebar.getByRole('button', { name: `${name} のメニュー` });

  // A project with a goal opens on it.
  await menu('見積もり').click();
  await page.getByRole('menuitem', { name: '目標を設定' }).click();
  const editing = page.getByRole('dialog', { name: '見積もりの目標を直す' });
  await expect(editing.getByLabel('目標', { exact: true })).toHaveValue('受注');
  await expect(editing.getByLabel('量')).toHaveValue('10');
  await expect(editing.getByLabel('期限')).toHaveValue('2026-12-25');
  await capture(page, info, 'goal-form-edit');
  await editing.getByRole('button', { name: 'キャンセル' }).click();
  await expect(editing).toHaveCount(0);
  await expect(menu('見積もり')).toBeFocused();

  // One without a goal opens empty, and the goal saved is the one it opens on next.
  await menu('採用').click();
  await page.getByRole('menuitem', { name: '目標を設定' }).click();
  const setting = page.getByRole('dialog', { name: '採用の目標を決める' });
  await expect(setting.getByLabel('目標', { exact: true })).toHaveValue('');
  await setting.getByLabel('目標', { exact: true }).fill('採用');
  await setting.getByLabel('量').fill('3');
  await setting.getByLabel('単位').fill('人');
  await setting.getByLabel('期限').fill('2027-03-31');
  await capture(page, info, 'goal-form-set');
  await setting.getByRole('button', { name: '保存' }).click();
  await expect(setting).toHaveCount(0);
  await expect(menu('採用')).toBeFocused();
  await menu('採用').click();
  await page.getByRole('menuitem', { name: '目標を設定' }).click();
  const saved = page.getByRole('dialog', { name: '採用の目標を直す' });
  await expect(saved.getByLabel('単位')).toHaveValue('人');
  await saved.getByRole('button', { name: '目標を外す' }).click();
  await expect(saved).toHaveCount(0);

  // The progress of a project without a goal offers to set one.
  await sidebar.getByRole('button', { name: '採用', exact: true }).click();
  await sidebar.getByRole('button', { name: '進み具合' }).click();
  const goal = page.getByRole('region', { name: '採用の目標' });
  await expect(goal).toContainText('まだ目標がありません。');
  await goal.getByRole('button', { name: '目標を設定' }).click();
  const fromProgress = page.getByRole('dialog', { name: '採用の目標を決める' });
  await fromProgress.getByLabel('目標', { exact: true }).fill('面接');
  await fromProgress.getByLabel('量').fill('5');
  await fromProgress.getByLabel('単位').fill('回');
  await fromProgress.getByLabel('期限').fill('2026-12-25');
  await fromProgress.getByRole('button', { name: '保存' }).click();
  await expect(fromProgress).toHaveCount(0);
  // Its 「目標を設定」 is gone with the goal set, so focus goes to the page's heading.
  await expect(goal).toContainText('12/25 までに 面接 5 回。11/4 に決めました。');
  await expect(goal).toContainText('まだ記録がありません。');
  await expect(page.getByRole('heading', { level: 1, name: '採用' })).toBeFocused();
});

test('beside a conversation, a row’s meta line wraps as one line of text with its due and delete in line', async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1180, height: 820 });
  await openHome(page);
  await page.getByRole('button', { name: /^来週の面接の質問リストを作る/ }).click();
  await expect(
    page.getByRole('region', { name: '来週の面接の質問リストを作る' }).getByRole('button', {
      name: '承認',
    })
  ).toBeVisible();

  const layout = await page.locator('.todo-row').evaluateAll((rows) =>
    rows.map((row) => {
      const parts = [...row.querySelectorAll('.todo-row__meta > span')];
      // Each part after the first starts on the line the one before it ends on.
      const flows = parts.slice(1).every((part, i) => {
        const before = [...parts[i].getClientRects()].at(-1)!;
        const first = part.getClientRects()[0];
        return Math.abs(first.top - before.top) < 1;
      });
      const due = row.querySelector('.todo-due')?.getBoundingClientRect();
      const remove = row.querySelector('.todo-row__delete')!.getBoundingClientRect();
      const inLine =
        due === undefined ||
        Math.abs(due.top + due.height / 2 - (remove.top + remove.height / 2)) < 1;
      return { text: row.querySelector('.todo-row__text')?.textContent, flows, inLine };
    })
  );
  expect(layout.length).toBeGreaterThan(0);
  for (const row of layout) {
    expect(row.flows, `${row.text}: meta parts flow`).toBe(true);
    expect(row.inLine, `${row.text}: due and delete in line`).toBe(true);
  }
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'beside-conversation');
});
