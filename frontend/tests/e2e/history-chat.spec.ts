import { test, expect, type Page } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import type { ChatItem, ChatItemPage } from '../../electron/src/chat/chatContracts';
import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import { waitForAnimationsToSettle } from './animations';

// The main window's History page from the production entry, with deterministic IPC inputs.
let vite: ViteDevServer;
let baseUrl: string;
test.beforeAll(async () => {
  vite = await createServer({ server: { host: '127.0.0.1', port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls!.local[0];
});
test.afterAll(async () => vite.close());
test.use({ viewport: { width: 1200, height: 860 }, deviceScaleFactor: 2 });

const today = new Date();
const at = (hour: number, minute: number) =>
  new Date(today.getFullYear(), today.getMonth(), today.getDate(), hour, minute).toISOString();

function item(sequence: number, minute: number, content: ChatItem['content']): ChatItem {
  return { sequence, item_id: `item-${sequence}`, created_at: at(9, minute), content };
}
const user = (sequence: number, minute: number, text: string, quote: string | null = null) =>
  item(sequence, minute, {
    kind: 'user_message',
    text,
    quote_item_id: quote,
    images: [],
    files: [],
  });
const reply = (
  sequence: number,
  minute: number,
  text: string,
  cards: ChatItem['content'] extends infer C
    ? C extends { kind: 'assistant_message'; cards: infer K }
      ? K
      : never
    : never = [],
  quote: string | null = null
) => item(sequence, minute, { kind: 'assistant_message', text, quote_item_id: quote, cards });

const CHAT: ChatItemPage = {
  items: [
    reply(9, 33, '見積書のほうに伝えました。', [
      {
        kind: 'action',
        action_id: 'estimate',
        summary: '納期を 2 週間うしろにずらして書き直しています。',
      },
    ]),
    user(8, 33, '納期のところ、もう少し余裕持たせて', 'item-6'),
    item(7, 31, {
      kind: 'action_event',
      action_id: 'estimate',
      event: 'completed',
      final_answer_excerpt: '数量と納期を直しました。',
    }),
    reply(6, 31, '見積書のたたき台ができました。変えたのは 2 か所だけです。', [
      {
        kind: 'action',
        action_id: 'estimate',
        summary: '数量と納期の記載を直しました。単価は前回のままです。',
      },
    ]),
    reply(5, 3, 'じゃあスライドの下書きから始めますね。', [
      {
        kind: 'action',
        action_id: 'slides',
        summary: '構成案をもとに、1 枚ずつ下書きを作っています。',
      },
    ]),
    user(4, 3, 'うん、お願い'),
    reply(
      3,
      2,
      '構成案まで決まってます。スライドはまだ手つかずです。続き、やっておきましょうか？',
      [],
      'item-2'
    ),
    user(2, 2, 'そういえば来週の登壇資料ってどこまで進んでたっけ'),
    reply(
      1,
      0,
      'おはようございます。昨日の打ち合わせメモに、見積もりの再提出は金曜って書いてありました。たたき台、作っておきましょうか？',
      [
        {
          kind: 'action',
          action_id: 'estimate',
          summary: '前回の見積もりと打ち合わせメモから、変わった点を反映した版を作ります。',
        },
      ]
    ),
  ],
  next_cursor: null,
};

const HISTORY: ConversationHistoryListItem[] = [
  {
    kind: 'conversation',
    action_id: 'estimate',
    title: '見積書のたたき台を作る',
    updated_at: at(9, 33),
    status: 'running',
    latest_completion_event_id: null,
  },
  {
    kind: 'conversation',
    action_id: 'slides',
    title: '登壇資料のスライドを下書きする',
    updated_at: at(9, 3),
    status: 'running',
    latest_completion_event_id: null,
  },
];

async function installBridge(page: Page, mode: 'chat' | 'list' | null) {
  await page.addInitScript(
    ({ chat, history, storedMode }) => {
      localStorage.setItem('pantaray_ui_language', 'ja');
      if (storedMode !== null && !sessionStorage.getItem('seeded')) {
        localStorage.setItem('pantaray.history-view-mode', storedMode);
        sessionStorage.setItem('seeded', '1');
      }
      const noop = () => {};
      const opened: string[] = [];
      Object.defineProperty(window, 'e2eOpened', { value: opened });
      Object.defineProperty(window, 'electron', {
        value: {
          ipcRenderer: { on: () => noop, send: noop },
          process: { platform: 'darwin', env: { NODE_ENV: 'test' } },
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
          // The connection notice stays out of the way: its state never arrives.
          aiConnection: { getState: () => new Promise(() => {}), onChanged: () => noop },
          shortcut: { getState: async () => ({ accelerator: 'Alt+Space', failure: null }) },
          chat: {
            listItems: async () => chat,
            sendMessage: async () => {
              throw new Error('not in this test');
            },
            onItemAppended: () => noop,
          },
          orchestration: { onStatus: () => noop, onEvent: () => noop },
          history: {
            fetch: async () => ({
              data: history,
              nextCursor: null,
              unreadActionIds: [],
              error: null,
              errorCode: null,
            }),
            onChanged: () => noop,
            openNewConversation: async () => undefined,
            openConversation: async ({ actionId }: { actionId: string }) => {
              opened.push(actionId);
              return 'focused';
            },
            deleteItem: async () => ({ ok: true }),
          },
        },
      });
    },
    { chat: CHAT, history: HISTORY, storedMode: mode }
  );
  await page.goto(`${baseUrl}#/history`);
}

test('chat mode is the default: bubbles, cards with latest-only status, no event bubbles', async ({
  page,
}, info) => {
  await installBridge(page, null);
  const chat = page.getByRole('list', { name: 'Pantaray とのチャット' });
  await expect(chat).toBeVisible();
  await expect(page.getByRole('button', { name: 'チャット', pressed: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '新しい作業' })).toBeVisible();
  await expect(page.getByRole('searchbox')).toHaveCount(0);

  await expect(chat.getByRole('article', { name: 'あなた' })).toHaveCount(3);
  await expect(chat.getByRole('article', { name: 'Pantaray' })).toHaveCount(5);
  await expect(page.getByText('数量と納期を直しました。')).toHaveCount(0);

  const estimate = chat.getByRole('button', { name: '見積書のたたき台を作る を開く' });
  await expect(estimate).toHaveCount(3);
  await expect(estimate.nth(2).getByText('実行中')).toBeVisible();
  await expect(estimate.nth(0).getByText('実行中')).toHaveCount(0);
  await expect(estimate.nth(1).getByText('実行中')).toHaveCount(0);

  // The newest message is in view without scrolling.
  await expect(chat.getByText('見積書のほうに伝えました。')).toBeInViewport();

  await estimate.nth(1).focus();
  await page.keyboard.press('Enter');
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { e2eOpened: string[] }).e2eOpened))
    .toEqual(['estimate']);
  await expect(estimate.nth(2)).toHaveAttribute('aria-current', 'true');

  for (const scheme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await waitForAnimationsToSettle(page);
    await page.screenshot({ path: info.outputPath(`history-chat-${scheme}.png`) });
  }
});

test('the chosen view is remembered, and the list stays as it was', async ({ page }, info) => {
  await installBridge(page, 'chat');
  await page.getByRole('button', { name: '一覧', pressed: false }).click();
  await expect(page.getByRole('button', { name: '一覧', pressed: true })).toBeFocused();

  // Today's list: search, day heading, rows with a delete button and a muted status badge.
  await expect(page.getByRole('searchbox')).toBeVisible();
  await expect(page.getByRole('heading', { name: '今日' })).toBeVisible();
  const row = page.getByRole('button', { name: /見積書のたたき台を作る/ }).first();
  await expect(row.getByText('実行中')).toBeVisible();
  await expect(page.getByRole('button', { name: '削除 見積書のたたき台を作る' })).toBeVisible();
  await expect(page.getByRole('button', { name: '新しい作業' })).toBeVisible();
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('history-list.png') });

  await page.reload();
  await expect(page.getByRole('button', { name: '一覧', pressed: true })).toBeVisible();
  await expect(page.getByRole('searchbox')).toBeVisible();
});
