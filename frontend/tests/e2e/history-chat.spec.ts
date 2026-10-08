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
    reply(9, 33, '納期を直しますね。', [
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

async function installBridge(
  page: Page,
  mode: 'chat' | 'list' | null,
  chatPage: ChatItemPage = CHAT
) {
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
      let sequence = 100;
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
            // Answers as the backend does: the appended item, with the request's content.
            sendMessage: async (request: {
              text: string;
              quote_item_id: string | null;
              images: unknown[];
              files: unknown[];
            }) => {
              sequence += 1;
              return {
                kind: 'sent',
                item: {
                  sequence,
                  item_id: `sent-${sequence}`,
                  created_at: new Date().toISOString(),
                  content: {
                    kind: 'user_message',
                    text: request.text.trim(),
                    quote_item_id: request.quote_item_id,
                    images: request.images,
                    files: request.files,
                  },
                },
              };
            },
            onItemAppended: () => noop,
            onTurnState: (callback: (state: { running: boolean }) => void) => {
              Object.defineProperty(window, 'e2eTurnState', {
                value: callback,
                configurable: true,
              });
              return noop;
            },
            getTurnState: async () => null,
            retryTurn: async ({ failure_item_id }: { failure_item_id: string }) => {
              Object.defineProperty(window, 'e2eRetried', {
                value: failure_item_id,
                configurable: true,
              });
              return { kind: 'started' };
            },
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
            onShowChat: (callback: (payload: { actionId: string }) => void) => {
              Object.defineProperty(window, 'e2eShowChat', { value: callback, configurable: true });
              return noop;
            },
          },
          actions: {
            attachFile: async ({ name, bytes }: { name: string; bytes: ArrayBuffer }) => ({
              attachmentId: '00000000-0000-4000-8000-000000000001',
              name,
              byteSize: bytes.byteLength,
            }),
            attachImage: async () => ({ kind: 'rejected', reason: 'failed' }),
            discardAttachment: async () => undefined,
            onConversationUpdated: () => noop,
          },
        },
      });
    },
    { chat: chatPage, history: HISTORY, storedMode: mode }
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
  await expect(chat.getByText('納期を直しますね。')).toBeInViewport();

  await estimate.nth(1).focus();
  await page.keyboard.press('Enter');
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { e2eOpened: string[] }).e2eOpened))
    .toEqual(['estimate']);
  await expect(estimate.nth(2)).toHaveAttribute('aria-current', 'true');

  for (const scheme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await waitForAnimationsToSettle(page);
    await page.screenshot({ path: info.outputPath(`chat-${scheme}.png`) });
  }
});

test('the chosen view is remembered, and the list stays as it was', async ({ page }, info) => {
  await installBridge(page, 'chat');
  await page.getByRole('button', { name: '作業', exact: true, pressed: false }).click();
  await expect(
    page.getByRole('button', { name: '作業', exact: true, pressed: true })
  ).toBeFocused();

  // Today's list: search, day heading, rows with a delete button and a muted status badge.
  await expect(page.getByRole('searchbox')).toBeVisible();
  await expect(page.getByRole('heading', { name: '今日' })).toBeVisible();
  const row = page.getByRole('button', { name: /見積書のたたき台を作る/ }).first();
  await expect(row.getByText('実行中')).toBeVisible();
  await expect(page.getByRole('button', { name: '削除 見積書のたたき台を作る' })).toBeVisible();
  await expect(page.getByRole('button', { name: '新しい作業' })).toBeVisible();
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('list.png') });

  await page.reload();
  await expect(
    page.getByRole('button', { name: '作業', exact: true, pressed: true })
  ).toBeVisible();
  await expect(page.getByRole('searchbox')).toBeVisible();
});

test('composer: quote a message, attach a document, send with Enter', async ({ page }, info) => {
  await installBridge(page, null);
  const chat = page.getByRole('list', { name: 'Pantaray とのチャット' });
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await expect(input).toBeVisible();
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-composer-empty.png') });

  // The quote button appears on hover and is reachable by keyboard.
  const reply = chat.getByRole('article', { name: 'Pantaray' }).last();
  await reply.hover();
  await reply.getByRole('button', { name: '引用して返信' }).click();
  await expect(input).toBeFocused();
  await expect(page.getByRole('button', { name: '引用をやめる' })).toBeVisible();
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-composer-quote.png') });

  await page
    .getByRole('form', { name: 'Pantaray へのメッセージ' })
    .locator('input[type="file"]')
    .setInputFiles({
      name: '見積書.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4\n%%EOF\n'),
    });
  await expect(page.getByRole('button', { name: '見積書.pdf を削除' })).toBeVisible();
  await input.fill('納期は 2 週間うしろで。単価はそのまま');
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-composer-attachment.png') });

  await input.press('Enter');
  const sent = chat.getByRole('article', { name: 'あなた' }).last();
  await expect(sent).toContainText('納期は 2 週間うしろで。単価はそのまま');
  await expect(sent).toContainText('納期を直しますね。');
  await expect(sent.getByRole('list', { name: '添付ファイル 1 件' })).toContainText('見積書.pdf');
  await expect(sent).toBeInViewport();
  await expect(input).toHaveValue('');
  await expect(page.getByRole('button', { name: '引用をやめる' })).toHaveCount(0);
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-after-send.png') });
});

test('the Overlay’s chat button opens the chat at that Action’s latest card', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 520 });
  await installBridge(page, 'list');
  await expect(page.getByRole('searchbox')).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as { e2eShowChat: (p: { actionId: string }) => void }).e2eShowChat({
      actionId: 'slides',
    })
  );
  const card = page.getByRole('button', { name: '登壇資料のスライドを下書きする を開く' });
  await expect(page.getByRole('button', { name: 'チャット', pressed: true })).toBeVisible();
  await expect(card).toBeFocused();
  await expect(card).toBeInViewport();
  await expect(card).toHaveAttribute('aria-current', 'true');
});

test('a running turn shows the typing bubble; a failed turn offers to try again', async ({
  page,
}, info) => {
  await installBridge(page, null, {
    items: [
      item(11, 41, { kind: 'turn_failure', reason: 'llm_connection' }),
      user(10, 41, '登壇資料の構成、もう一度見直して'),
      ...CHAT.items,
    ],
    next_cursor: null,
  });
  const lastMessage = page.getByRole('article', { name: 'あなた' }).last();
  const retry = lastMessage.getByRole('button', {
    name: 'AI に接続できず、返信できませんでした。もう一度送る',
  });
  await expect(retry).toBeVisible();
  await expect(retry).toHaveAttribute(
    'title',
    'AI に接続できず、返信できませんでした。もう一度送る'
  );
  // No notice text: the icon under the message is all there is.
  await expect(page.getByText('AI に接続できず、返信できませんでした。')).toHaveCount(0);
  await retry.focus();
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-turn-failure.png') });

  await page.keyboard.press('Enter');
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { e2eRetried?: string }).e2eRetried))
    .toBe('item-11');

  await page.evaluate(() =>
    (window as unknown as { e2eTurnState: (s: { running: boolean }) => void }).e2eTurnState({
      running: true,
    })
  );
  await expect(page.getByRole('status', { name: '入力中' })).toBeInViewport({ ratio: 1 });
  await expect(retry).toHaveCount(0);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-typing.png') });
});
