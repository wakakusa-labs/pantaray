import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import type { OverlaySnapshot } from '../../src/components/agent-overlay/model/overlayTypes';
import type {
  DeletedTodoView,
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
    ({ organizations, projects, items, deleted, suggestion }) => {
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
