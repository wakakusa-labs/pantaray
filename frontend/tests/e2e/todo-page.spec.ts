import { test, expect, type Locator, type Page, type TestInfo } from '@playwright/test';
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
  TodoCreateRequest,
  TodoDueRequest,
  TodoStartRequest,
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
    starts_on: '2026-11-02',
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
    starts_on: '2026-10-26',
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
      const notStarted = (startsOn: string): TodoView['state'] => ({
        kind: 'not_started',
        reason: startsOn > TODAY ? 'scheduled' : 'starting',
      });
      // Goals the form writes stay for the page's later reads, as the backend's do.
      const goals = new Map(initialGoals.map((goal) => [goal.project_id, goal]));
      const inFilter = (projectId: string | null, query: TodoProgressQuery) =>
        query.project === 'any' ||
        (query.project === 'no_project' ? projectId === null : projectId === query.project_id);
      // The page holding `on` (today without one), as the backend lays it out: the Monday-start
      // weeks of the month a row a day, or the year's twelve months.
      const pageStart = (query: TodoProgressQuery) => {
        const on = query.on ?? TODAY;
        return query.period === 'month' ? `${on.slice(0, 4)}-01-01` : `${on.slice(0, 7)}-01`;
      };
      const rowBounds = (query: TodoProgressQuery) => {
        const first = pageStart(query);
        const year = Number(first.slice(0, 4));
        const monthEnd = (month: number) =>
          new Date(Date.UTC(year, month + 1, 0)).toISOString().slice(0, 10);
        if (query.period === 'month')
          return Array.from({ length: 12 }, (_, i) => ({
            start: `${year}-${String(i + 1).padStart(2, '0')}-01`,
            end: monthEnd(i),
          }));
        const weekday = (date: string) => (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7;
        const last = monthEnd(Number(first.slice(5, 7)) - 1);
        const gridStart = shift(first, -weekday(first));
        const days = Math.round(
          (Date.parse(shift(last, 6 - weekday(last))) - Date.parse(gridStart)) / 86_400_000
        );
        return Array.from({ length: days + 1 }, (_, i) => ({
          start: shift(gridStart, i),
          end: shift(gridStart, i),
        }));
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
        const rows: ProgressPeriodView[] = rowBounds(query).map(({ start, end }) => {
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
          page_start: pageStart(query),
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
              // 見積もり's folders, shown under the header while it is in view.
              folders: ['/Users/me/Developer/clients/xer/estimates', '/Users/me/Documents/xer'].map(
                (path, index) => ({
                  folder_id: `folder-${index}`,
                  display_name: path.split('/').pop(),
                  real_path: path,
                  canonical_real_path: path,
                  organization_ids: [],
                  project_ids: ['estimate'],
                })
              ),
            }),
            createOrganization: async (input: {
              displayName: string;
              iconDataUrl: string | null;
            }) => {
              const created = {
                organization_id: 'labs',
                display_name: input.displayName,
                icon_data_url: input.iconDataUrl,
              };
              organizations.push(created);
              return created;
            },
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
            // Writes change the TODOs the next read returns, as the backend's do.
            create: async (request: TodoCreateRequest) => {
              const added: TodoView = {
                todo_id: `t-added-${items.length}`,
                project_id: request.project_id,
                text: request.text,
                author: 'user',
                origin: 'written',
                starts_on: request.starts_on ?? TODAY,
                carried: false,
                due: request.due,
                state: { kind: 'not_started', reason: 'starting' },
                deletion_request: null,
                conversation: { kind: 'none' },
              };
              added.state = notStarted(added.starts_on);
              items.push(added);
              return { kind: 'ok', result: added };
            },
            setDue: async ({ todo_id, due }: TodoDueRequest) => {
              const item = items.find((candidate) => candidate.todo_id === todo_id)!;
              item.due = due;
              return { kind: 'ok', result: item };
            },
            setStart: async ({ todo_id, starts_on }: TodoStartRequest) => {
              const item = items.find((candidate) => candidate.todo_id === todo_id)!;
              item.starts_on = starts_on;
              item.state = notStarted(starts_on);
              return { kind: 'ok', result: item };
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
            // The request's conversation is still going, so the backend keeps the TODO.
            answerDeletion: async () => ({ kind: 'refused', code: 'TODO_BUSY' }),
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

const sidebarOf = (page: Page) => page.getByRole('complementary', { name: 'TODO', exact: true });
const railOf = (page: Page) => page.getByRole('group', { name: '組織' });
const groups = (page: Page) => page.locator('section.todo-group');
const rowOf = (scope: Page | Locator, text: string) =>
  scope.getByRole('listitem').filter({ hasText: text });
const hasNoSideScroll = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

test('the home lists every organization’s week by project, with the rail and the sidebar', async ({
  page,
}, info) => {
  await openHome(page);
  await expect(page.getByRole('heading', { level: 1, name: '今週' })).toBeVisible();
  await expect(page.locator('.todo-screen__header')).toContainText('11月2日 – 8日');
  await expect(page.locator('.todo-screen__summary')).toHaveText('2 / 7 完了');

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
    /^見積もりXer1 \/ 2$/,
    /^採用Xer0 \/ 1$/,
    /^プロダクトWakakusa0 \/ 2$/,
    /^家のこと組織なし0 \/ 1$/,
    /^プロジェクトなし組織なし1 \/ 1$/,
  ]);
  const estimate = page.getByRole('region', { name: '見積もり Xer' });
  await expect(
    estimate.getByRole('checkbox', { name: '完了: 名刺 40 枚を入れておく' })
  ).toBeChecked();
  // Each row says where it stands in a word, and its due day.
  const resubmit = rowOf(estimate, '見積書を再提出する');
  await expect(resubmit.locator('.todo-row__state')).toHaveText('途中まで');
  await expect(
    resubmit.getByRole('button', { name: '期限 金 11/6: 見積書を再提出する' })
  ).toHaveText('金 11/6');
  await expect(rowOf(page, '古いブランチを消す').locator('.todo-row__state')).toHaveText(
    '消してよい？'
  );
  // One add bar under the list, into the first project in view until another is chosen.
  await expect(page.getByRole('textbox', { name: 'TODO を追加' })).toHaveCount(1);
  await expect(page.getByRole('combobox', { name: 'プロジェクト' })).toHaveValue('estimate');
  await expect(page.getByRole('button', { name: '期限なし', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '開始 今日' })).toBeVisible();
  // Every row is one line.
  const heights = await page
    .locator('.todo-row')
    .evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height));
  expect(new Set(heights)).toEqual(new Set([40]));

  // The sidebar: all TODOs (current), the projects with their open counts, and deleted; the
  // search, the chat and the other conversations are off.
  const sidebar = sidebarOf(page);
  await expect(sidebar.getByRole('button', { name: 'すべての TODO' })).toHaveAttribute(
    'aria-current',
    'true'
  );
  await expect(
    sidebar.getByRole('button', { name: 'プロダクト', exact: true })
  ).toHaveAccessibleDescription('未完了 2 件');
  await expect(sidebar.getByRole('button', { name: '削除した TODO' })).toBeVisible();
  await expect(sidebar.getByRole('searchbox')).toHaveCount(0);
  await expect(sidebar.getByRole('button', { name: 'チャット', exact: true })).toHaveCount(0);
  await expect(sidebar.getByRole('heading', { level: 2, name: 'そのほかの会話' })).toHaveCount(0);
  await expect(sidebar.getByRole('button', { name: /^競合 3 社の料金ページを調べる/ })).toHaveCount(
    0
  );
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

  // Pantaray's request to delete waits under the conversation for the user's 消す or 残す.
  await page.getByRole('button', { name: /^古いブランチを消す/ }).click();
  const request = page.getByRole('group', {
    name: 'Pantaray がこの TODO を消したいと言っています',
  });
  await expect(request).toContainText('どれも main に取り込み済みで');
  await expect(request.getByRole('button', { name: '消す' })).toBeVisible();
  await expect(request.getByRole('button', { name: '残す' })).toBeVisible();

  await page.getByRole('button', { name: '会話を閉じる' }).click();
  await expect(page).toHaveURL(/#\/history$/);
  await expect(page.getByRole('heading', { level: 1, name: '今週' })).toBeFocused();
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
  await expect(groups(page).locator('h3')).toHaveText(['見積もり']);
  // Its folders on one line under the header, each path cut in its middle.
  const folders = page.getByRole('list', { name: 'フォルダ' });
  await expect(folders.getByRole('listitem')).toHaveCount(2);
  await expect(
    folders.getByRole('button', { name: '/Users/me/Developer/clients/xer/estimates' })
  ).toBeVisible();
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'week-project-folders');

  // An ended week shows what was done in it, takes no new TODOs, and offers 今週へ.
  await page.getByRole('button', { name: '前の週' }).click();
  await expect(page.getByRole('heading', { level: 1, name: '10月26日 – 11月1日' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'TODO を追加' })).toHaveCount(0);
  const thisWeek = page.getByRole('button', { name: '今週へ' });
  await capture(page, info, 'week-past');
  await thisWeek.focus();
  await page.keyboard.press('Enter');
  await expect(thisWeek).toHaveCount(0);
  await expect(page.getByRole('heading', { level: 1, name: '今週' })).toBeFocused();
  await expect(page.locator('.todo-screen__header')).toContainText('11月2日 – 8日');
});

test('a project’s TODO opens beside its list, with the add bar under it offering no project', async ({
  page,
}, info) => {
  await openHome(page, '?project=project:hiring&todo=t-questions');
  const pane = page.getByRole('region', { name: '来週の面接の質問リストを作る' });
  await expect(pane.getByRole('button', { name: '承認' })).toBeEnabled();
  await expect(page.getByRole('textbox', { name: 'TODO を追加' })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'プロジェクト' })).toHaveCount(0);
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'project-conversation');
});

test('a row’s due day and start change from a small picker, and the bar under the list adds', async ({
  page,
}, info) => {
  await openHome(page);
  const estimate = page.getByRole('region', { name: '見積もり Xer' });
  const due = estimate.getByRole('button', { name: '期限 金 11/6: 見積書を再提出する' });
  await due.click();
  const picker = page.getByRole('dialog', { name: '期限' });
  await expect(picker.getByRole('button', { name: '今日' })).toBeFocused();
  const [trigger, panel] = await Promise.all([due.boundingBox(), picker.boundingBox()]);
  expect(panel!.y).toBeGreaterThan(trigger!.y + trigger!.height);
  expect(panel!.x + panel!.width).toBeLessThanOrEqual(trigger!.x + trigger!.width + 1);
  await capture(page, info, 'due-picker');
  await page.keyboard.press('Escape');
  await expect(picker).toBeHidden();
  await expect(due).toBeFocused();

  // 明日 is Thursday 11/5; focus returns to the row's due day, and 期限をなくす takes it away.
  await due.click();
  await picker.getByRole('button', { name: '明日' }).click();
  const moved = estimate.getByRole('button', { name: '期限 木 11/5: 見積書を再提出する' });
  await expect(moved).toBeFocused();
  await moved.click();
  await picker.getByRole('button', { name: '期限をなくす' }).click();
  await expect(
    estimate.getByRole('button', { name: '期限なし: 見積書を再提出する' })
  ).toBeFocused();

  // The bar adds to the project, due and start chosen; a start ahead shows on the row.
  await page.getByRole('combobox', { name: 'プロジェクト' }).selectOption({ label: '採用' });
  await page.getByRole('button', { name: '開始 今日' }).click();
  await page.getByRole('dialog', { name: '開始日' }).getByLabel('日付').fill('2026-11-18');
  await page.getByRole('button', { name: '期限なし', exact: true }).click();
  await page.getByRole('dialog', { name: '期限' }).getByRole('button', { name: '来週' }).click();
  const field = page.getByRole('textbox', { name: 'TODO を追加' });
  await field.fill('面接官の日程を押さえる');
  await expect(page.getByRole('button', { name: '開始 水 11/18' })).toBeVisible();
  await expect(page.getByRole('button', { name: '期限 月 11/9' })).toBeVisible();
  await capture(page, info, 'add-bar');
  await field.press('Enter');
  await expect(field).toHaveValue('');
  const added = rowOf(page.getByRole('region', { name: '採用 Xer' }), '面接官の日程を押さえる');
  await expect(
    added.getByRole('button', { name: '期限 月 11/9: 面接官の日程を押さえる' })
  ).toBeVisible();

  // The start of a TODO not started yet changes from its row.
  await added.getByRole('button', { name: '開始日 水 11/18から: 面接官の日程を押さえる' }).click();
  await page.getByRole('dialog', { name: '開始日' }).getByRole('button', { name: '今日' }).click();
  await expect(
    added.getByRole('button', { name: '開始日 まもなく: 面接官の日程を押さえる' })
  ).toBeFocused();
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
  await expect(page.getByRole('heading', { level: 1, name: '今週' })).toBeVisible();
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

test('a narrow window tells a failed answer to Pantaray’s deletion request in the conversation', async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 960, height: 820 });
  await openHome(page);
  await page.getByRole('button', { name: /^古いブランチを消す/ }).click();
  const request = page.getByRole('group', {
    name: 'Pantaray がこの TODO を消したいと言っています',
  });
  await request.getByRole('button', { name: '消す' }).click();
  // The list is hidden at this width; the failure is told under the request, and only there.
  const alerts = page.getByRole('alert');
  await expect(alerts).toHaveCount(1);
  await expect(alerts).toBeVisible();
  await expect(alerts).toHaveText(
    'この TODO の会話が進んでいるあいだは消せません。終わってから、または止めてから消してください。'
  );
  await capture(page, info, 'narrow-deletion-failure');
});

test('進み具合 lays out the month, keeps its page in the address, and opens a day’s records', async ({
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
  const heading = page.getByRole('heading', { level: 1, name: '進み具合' });
  await expect(heading).toBeVisible();
  await expect(page.locator('.todo-screen__range')).toHaveText('2026年11月');
  await expect(page.getByRole('button', { name: '今月へ' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '月', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true'
  );

  const goal = page.getByRole('region', { name: '見積もりの目標' });
  await expect(goal).toContainText('受注 10 件');
  await expect(goal).toContainText('12/25 まで');
  await expect(goal).toContainText('40%');
  await expect(goal).toContainText('このペースなら 2026 年 12 月のなかばに届きます。');
  await expect(page.locator('.todo-progress__lead')).toHaveText(
    '今週は、見積もりが 1 件進みました。完了した TODO は 1 件です。受注 10 件の目標に対して、いまは 40% です。このペースなら 2026 年 12 月のなかばに届きます。'
  );
  // A day ahead opens nothing.
  await expect(page.getByRole('button', { name: /^11月5日/ })).toHaveCount(0);
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'progress-month');

  // A day opens beside the calendar, in the conversation's place, and the sidebar folds away.
  await page.getByRole('button', { name: '11月3日（火）、記録 1 件、完了した TODO 1 件' }).click();
  await expect(page).toHaveURL(/view=progress&day=2026-11-03$/);
  const day = page.getByRole('region', { name: '11月3日（火）' });
  await expect(day).toContainText('記録 1 件');
  await expect(day).toContainText('丸和商事から受注');
  await expect(day).toContainText('名刺 40 枚を入れておく');
  await expect(sidebarOf(page)).toHaveCount(0);
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'progress-day');

  // The record's conversation opens as any conversation does, and back returns to the day.
  await day.getByRole('button', { name: 'この記録の会話を開く: 丸和商事から受注' }).click();
  await expect(page).toHaveURL(/#\/history\?item=action:action-resubmit$/);
  await page.goBack();
  await expect(page).toHaveURL(/view=progress&day=2026-11-03$/);
  await expect(day).toBeVisible();
  await page.getByRole('button', { name: 'この日を閉じる' }).click();
  await expect(day).toHaveCount(0);
  await expect(heading).toBeFocused();
  await expect(sidebar).toBeVisible();

  // The month is the address's: a reload keeps it, and 今月へ returns to today's.
  await page.getByRole('button', { name: '前の月' }).click();
  await expect(page).toHaveURL(/view=progress&on=2026-10-01$/);
  await expect(page.locator('.todo-progress__lead')).toContainText(
    '10月は、見積もりが 3 件進みました。'
  );
  await page.reload();
  await expect(page.locator('.todo-screen__range')).toHaveText('2026年10月');
  await page.getByRole('button', { name: '今月へ' }).click();
  await expect(page).toHaveURL(/#\/history\?project=project:estimate&view=progress$/);
  await expect(heading).toBeFocused();

  // The year lays out its months, and a month opens as its calendar.
  await page.getByRole('button', { name: '年', exact: true }).click();
  await expect(page).toHaveURL(/view=progress&span=year$/);
  await expect(page.locator('.todo-screen__range')).toHaveText('2026年');
  const months = page.getByRole('group', { name: '2026年' }).getByRole('button');
  await expect(months).toHaveCount(12);
  await capture(page, info, 'progress-year');
  await page.getByRole('button', { name: '2026年10月、記録 2 件、完了した TODO 0 件' }).click();
  await expect(page).toHaveURL(/view=progress&on=2026-10-01$/);
  await expect(page.locator('.todo-screen__range')).toHaveText('2026年10月');

  // Every organization's progress, with each goal there is.
  await sidebar.getByRole('button', { name: 'すべての TODO' }).click();
  await sidebar.getByRole('button', { name: '進み具合' }).click();
  await expect(page).toHaveURL(/#\/history\?view=progress$/);
  await expect(page.locator('.todo-progress__lead')).toContainText(
    '今週は、プロダクトが 1 本、見積もりが 1 件進みました。完了した TODO は 2 件です。'
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
  await expect(goal).toContainText('面接 5 回');
  await expect(goal).toContainText('12/25 まで');
  await expect(goal).toContainText('まだ記録がありません。');
  await expect(page.getByRole('heading', { level: 1, name: '進み具合' })).toBeFocused();
});

test('beside a conversation the sidebar folds away and the rows keep their one line', async ({
  page,
}, info) => {
  await openHome(page);
  await page.getByRole('button', { name: /^来週の面接の質問リストを作る/ }).click();
  await expect(
    page.getByRole('region', { name: '来週の面接の質問リストを作る' }).getByRole('button', {
      name: '承認',
    })
  ).toBeVisible();
  await expect(sidebarOf(page)).toHaveCount(0);
  const heights = await page
    .locator('.todo-row')
    .evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height));
  expect(new Set(heights)).toEqual(new Set([40]));
  expect(await hasNoSideScroll(page)).toBe(true);
  await capture(page, info, 'beside-conversation');

  await page.getByRole('button', { name: '会話を閉じる' }).click();
  await expect(sidebarOf(page)).toBeVisible();
});

test('the rail’s ＋ adds an organization with its image shrunk to a small icon, and shows it', async ({
  page,
}, info) => {
  await openHome(page);
  const rail = railOf(page);
  await page.getByRole('button', { name: '組織を追加' }).click();
  const dialog = page.getByRole('dialog', { name: '組織を追加' });
  await expect(dialog.getByRole('textbox', { name: '組織の名前' })).toBeFocused();
  await dialog.getByRole('textbox', { name: '組織の名前' }).fill('Labs');
  // A 400×300 photo-like PNG, drawn by the browser.
  const picked = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 400;
    canvas.height = 300;
    const context = canvas.getContext('2d')!;
    for (let x = 0; x < 400; x += 4)
      for (let y = 0; y < 300; y += 4) {
        context.fillStyle = `rgb(${(x * 7) % 256}, ${(y * 5) % 256}, ${(x * y) % 256})`;
        context.fillRect(x, y, 4, 4);
      }
    return canvas.toDataURL('image/png').split(',')[1];
  });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'logo.png',
    mimeType: 'image/png',
    buffer: Buffer.from(picked, 'base64'),
  });
  await expect(dialog.getByRole('button', { name: '画像を選ぶ' }).locator('img')).toBeVisible();
  await capture(page, info, 'organization-add');

  await dialog.getByRole('button', { name: '追加' }).click();
  await expect(page).toHaveURL(/#\/history\?scope=organization:labs$/);
  await expect(page.getByRole('button', { name: '組織を追加' })).toBeFocused();
  const labs = rail.getByRole('button', { name: 'Labs' });
  await expect(labs).toHaveAttribute('aria-pressed', 'true');
  // What was sent: a 128px square WebP within the backend's 64 KiB.
  const icon = await labs.locator('img').evaluate(async (image: HTMLImageElement) => {
    await image.decode();
    const base64 = image.src.split(',')[1];
    return {
      type: image.src.slice(0, image.src.indexOf(';')),
      width: image.naturalWidth,
      height: image.naturalHeight,
      bytes: atob(base64).length,
    };
  });
  expect(icon).toMatchObject({ type: 'data:image/webp', width: 128, height: 128 });
  expect(icon.bytes).toBeLessThanOrEqual(64 * 1024);
  await expect(sidebarOf(page).getByRole('heading', { level: 2, name: 'Labs' })).toBeVisible();
  await capture(page, info, 'organization-rail');

  // A project in view; its row has no folders under it.
  await rail.getByRole('button', { name: 'Xer' }).click();
  await sidebarOf(page).getByRole('button', { name: '見積もり', exact: true }).click();
  await expect(page.locator('.history-project__details')).toHaveCount(0);
  await capture(page, info, 'organization-project');
});
