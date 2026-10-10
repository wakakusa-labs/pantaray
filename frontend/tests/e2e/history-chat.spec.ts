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
    project_refs: [],
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
  chatPage: ChatItemPage = CHAT,
  historyItems: ConversationHistoryListItem[] = HISTORY
) {
  await page.addInitScript(
    ({ chat, history }) => {
      localStorage.setItem('pantaray_ui_language', 'ja');
      const noop = () => {};
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
              project_refs: unknown[];
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
                    project_refs: request.project_refs,
                  },
                },
              };
            },
            // Every subscriber hears an item the test appends, as main's relay reaches them all.
            onItemAppended: (callback: (item: unknown) => void) => {
              const target = window as unknown as { e2eAppended?: Set<(item: unknown) => void> };
              target.e2eAppended ??= new Set();
              target.e2eAppended.add(callback);
              return () => target.e2eAppended?.delete(callback);
            },
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
            deleteItem: async () => ({ ok: true }),
            onShowChat: (callback: (payload: { actionId: string }) => void) => {
              Object.defineProperty(window, 'e2eShowChat', { value: callback, configurable: true });
              return noop;
            },
          },
          // The detail pane's reads stay pending: these tests look at what selects it.
          suggestions: { read: () => new Promise(() => {}), onSnapshot: () => noop },
          actions: {
            attachFile: async ({ name, bytes }: { name: string; bytes: ArrayBuffer }) => ({
              attachmentId: '00000000-0000-4000-8000-000000000001',
              name,
              byteSize: bytes.byteLength,
            }),
            attachImage: async () => ({ kind: 'rejected', reason: 'failed' }),
            discardAttachment: async () => undefined,
            openConversation: () => new Promise(() => {}),
            onConversationUpdated: () => noop,
          },
        },
      });
    },
    { chat: chatPage, history: historyItems }
  );
  await page.goto(`${baseUrl}#/history`);
}

const sidebarOf = (page: Page) => page.getByRole('complementary', { name: 'チャットと作業' });

test('the chat pane: bubbles, cards with latest-only status, no event bubbles', async ({
  page,
}, info) => {
  await installBridge(page);
  const chat = page.getByRole('list', { name: 'Pantaray とのチャット' });
  await expect(chat).toBeVisible();
  await expect(page.getByRole('heading', { level: 1, name: 'チャット' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'チャット', exact: true })).toHaveAttribute(
    'aria-current',
    'true'
  );

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

  for (const scheme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await waitForAnimationsToSettle(page);
    await page.screenshot({ path: info.outputPath(`chat-${scheme}.png`) });
  }

  // A card selects its work: the detail pane shows it, and its row is the current one.
  await estimate.nth(1).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/history\?item=action:estimate$/);
  await expect(
    page.getByRole('heading', { level: 1, name: '見積書のたたき台を作る' })
  ).toBeVisible();
  await expect(
    sidebarOf(page).getByRole('button', { name: /^見積書のたたき台を作る/ })
  ).toHaveAttribute('aria-current', 'true');
});

test('the sidebar holds New task, search, the chat row and the tasks by day', async ({
  page,
}, info) => {
  await installBridge(page);
  const sidebar = sidebarOf(page);
  await expect(sidebar.getByRole('button', { name: '新しい作業' })).toBeVisible();
  await expect(sidebar.getByRole('searchbox', { name: '作業を検索' })).toBeVisible();
  await expect(sidebar.getByRole('button', { name: 'チャット', exact: true })).toBeVisible();
  await expect(sidebar.getByRole('heading', { level: 2, name: '今日' })).toBeVisible();
  const row = sidebar.getByRole('button', { name: /^見積書のたたき台を作る/ });
  // A running task shows its title alone, with no badge and no dot, and is heard as running.
  await expect(row).toHaveAccessibleName('見積書のたたき台を作る 実行中');
  await expect(row.getByRole('img')).toHaveCount(0);
  // The delete button shows while its row is pointed at; a running task's stays disabled.
  const trash = sidebar.getByRole('button', { name: '削除 見積書のたたき台を作る' });
  await expect(trash).toHaveCSS('opacity', '0');
  await row.hover();
  await expect(trash).toHaveCSS('opacity', '0.35');
  // A clicked row keeps focus while its task is shown, which must not keep the button shown.
  await row.click();
  await page.mouse.move(900, 400);
  await expect(row).toBeFocused();
  await expect(trash).toHaveCSS('opacity', '0');
  // Keyboard focus shows it.
  await page.keyboard.press('Tab');
  await expect(
    sidebar.getByRole('button', { name: /^登壇資料のスライドを下書きする/ })
  ).toBeFocused();
  await expect(
    sidebar.getByRole('button', { name: '削除 登壇資料のスライドを下書きする' })
  ).toHaveCSS('opacity', '0.35');
  expect((await sidebar.boundingBox())!.width).toBeCloseTo(300, -1);
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('sidebar.png') });
});

test('composer: quote a message, attach a document, send with Enter', async ({ page }, info) => {
  await installBridge(page);
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
  await installBridge(page);
  await expect(page.getByRole('searchbox')).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as { e2eShowChat: (p: { actionId: string }) => void }).e2eShowChat({
      actionId: 'slides',
    })
  );
  const card = page.getByRole('button', { name: '登壇資料のスライドを下書きする を開く' });
  await expect(card).toBeFocused();
  await expect(card).toBeInViewport();
  await expect(card).toHaveAttribute('aria-current', 'true');
});

test('a running turn shows the typing bubble; a failed turn offers to try again', async ({
  page,
}, info) => {
  await installBridge(page, {
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
  // The time stays beside the bubble's bottom; the icon has its own row under the bubble.
  const bubble = await lastMessage.locator('.chat-bubble').boundingBox();
  const time = await lastMessage.locator('time').boundingBox();
  const icon = await retry.boundingBox();
  expect(Math.abs(bubble!.y + bubble!.height - (time!.y + time!.height))).toBeLessThan(8);
  expect(icon!.y).toBeGreaterThanOrEqual(bubble!.y + bubble!.height);
  expect(Math.abs(icon!.x + icon!.width - (bubble!.x + bubble!.width))).toBeLessThan(2);
  // No notice text: the icon under the message is all there is.
  await expect(page.getByText('AI に接続できず、返信できませんでした。')).toHaveCount(0);
  // A small glyph, with a hit area of at least 24 × 24.
  expect(icon!.width).toBeGreaterThanOrEqual(24);
  expect(icon!.height).toBeGreaterThanOrEqual(24);
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-turn-failure.png') });
  // Reached from the composer by keyboard, it shows the focus ring.
  // Backwards from the message field: the attach button, the bubble's quote button, then this.
  await page.getByRole('textbox', { name: 'メッセージ' }).focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Shift+Tab');
  await expect(retry).toBeFocused();
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('chat-turn-failure-focus.png') });

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

/** Drags the mouse across the element's text, first character to last, as a reader selects it. */
async function dragAcross(page: Page, selector: string) {
  const [start, end] = await page.locator(selector).evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const rects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0);
    const first = rects[0];
    const last = rects[rects.length - 1];
    return [
      [first.left + 1, first.top + first.height / 2],
      [last.right - 1, last.top + last.height / 2],
    ];
  });
  await page.mouse.move(start[0], start[1]);
  await page.mouse.down();
  await page.mouse.move(end[0], end[1], { steps: 8 });
  await page.mouse.up();
}

test('text in either side’s bubbles, with its quote and link, copies with the keyboard', async ({
  page,
  context,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await installBridge(page, {
    items: [
      reply(
        3,
        5,
        '直しました。[見積書](https://example.com/estimate) を見てください。',
        [],
        'item-2'
      ),
      user(2, 4, '納期を 2 週間うしろに'),
      reply(1, 0, 'おはようございます。'),
    ],
    next_cursor: null,
  });
  const chat = page.getByRole('list', { name: 'Pantaray とのチャット' });
  await expect(chat.getByRole('link', { name: '見積書' })).toBeVisible();
  const clipboard = () => page.evaluate(() => navigator.clipboard.readText());

  await dragAcross(page, '.chat-row--mine .chat-bubble__text');
  await page.keyboard.press('ControlOrMeta+c');
  await expect.poll(clipboard).toBe('納期を 2 週間うしろに');

  // Pantaray's reply: the quote it answers, then its text with the link's words.
  await dragAcross(page, '.chat-row:not(.chat-row--mine):has(.chat-quote) .chat-bubble');
  await page.keyboard.press('ControlOrMeta+c');
  await expect
    .poll(clipboard)
    .toMatch(/納期を 2 週間うしろに[\s\S]*直しました。見積書 を見てください。/);
});

const READ_POSITION_KEY = 'pantaray.chat-read:account:user-1';

/** Delivers a chat item the way main relays one that was appended while the window is open. */
async function appendLive(page: Page, chatItem: ChatItem) {
  await page.evaluate((value) => {
    const target = window as unknown as { e2eAppended?: Set<(item: unknown) => void> };
    for (const callback of target.e2eAppended ?? []) callback(value);
  }, chatItem);
}

test('unread Pantaray messages mark the rail and the chat row until the chat is read', async ({
  page,
}, info) => {
  // Read up to the user's first message before this window opened: four replies came after it.
  await page.addInitScript((key) => {
    if (sessionStorage.getItem('read-seeded')) return;
    localStorage.setItem(key, '2');
    sessionStorage.setItem('read-seeded', '1');
  }, READ_POSITION_KEY);
  await installBridge(page);
  const history = page.getByRole('button', { name: '履歴' });
  const chatRow = sidebarOf(page).getByRole('button', { name: 'チャット', exact: true });

  // The chat opens on its newest message, so everything is read.
  await expect(page.getByText('納期を直しますね。')).toBeInViewport();
  await expect
    .poll(() => page.evaluate((key) => localStorage.getItem(key), READ_POSITION_KEY))
    .toBe('9');
  await expect(history).not.toHaveAccessibleDescription(/未読/);
  await expect(history.locator('.app-rail-unread-dot')).toHaveCount(0);
  await expect(chatRow).toHaveText('チャット');

  // A reply that arrives while the newest message is in view is read as it lands.
  await appendLive(page, reply(10, 40, '下書きを直しました。'));
  await expect(
    page.getByRole('list', { name: 'Pantaray とのチャット' }).getByText('下書きを直しました。')
  ).toBeVisible();
  await expect(history).not.toHaveAccessibleDescription(/未読/);

  // A reader who scrolled up to older messages has not seen a reply that lands below.
  const chatList = page.getByRole('list', { name: 'Pantaray とのチャット' });
  await chatList.hover();
  await page.mouse.wheel(0, -400);
  await expect(page.getByText('じゃあスライドの下書きから始めますね。')).toBeInViewport();
  await appendLive(page, reply(11, 41, '表紙も作りました。'));
  await expect(history).toHaveAccessibleDescription('未読 1 件');
  await expect(chatRow).toHaveAccessibleDescription('未読 1 件');
  await expect(chatRow).toHaveText('チャット1');
  await expect(history.locator('.app-rail-unread-dot')).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), READ_POSITION_KEY)).toBe('10');

  // Pantaray's replies count and the user's own messages never do.
  await appendLive(page, user(12, 42, 'ありがとう'));
  await appendLive(page, reply(13, 43, 'ほかに直すところがあれば言ってください。'));
  await expect(history).toHaveAccessibleDescription('未読 2 件');
  await expect(chatRow).toHaveText('チャット2');
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('unread-rail-and-row.png') });
  await page.screenshot({
    path: info.outputPath('unread-rail-and-row-detail.png'),
    clip: { x: 0, y: 0, width: 620, height: 200 },
  });

  // The Chat row takes the reader back to the newest message, which reads it.
  await chatRow.click();
  await expect(chatList.getByText('ほかに直すところがあれば言ってください。')).toBeInViewport();
  await expect(history).not.toHaveAccessibleDescription(/未読/);
  await expect(chatRow).toHaveText('チャット');
  expect(await page.evaluate((key) => localStorage.getItem(key), READ_POSITION_KEY)).toBe('13');

  // A restart reads the stored position, so what was read stays read.
  await page.reload();
  await expect(chatRow).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), READ_POSITION_KEY)).toBe('13');
  await expect(history).not.toHaveAccessibleDescription(/未読/);
});

test('a first run marks the existing chat read instead of showing it all as unread', async ({
  page,
}) => {
  await installBridge(page);
  await expect
    .poll(() => page.evaluate((key) => localStorage.getItem(key), READ_POSITION_KEY))
    .toBe('9');
  await expect(page.getByRole('button', { name: '履歴' })).not.toHaveAccessibleDescription(/未読/);
});

test('a suggestion the user has not answered has a dot and opens in the detail pane', async ({
  page,
}, info) => {
  await installBridge(page, CHAT, [
    {
      kind: 'suggestion',
      suggestion_id: 'suggestion-1',
      title: '来週の登壇資料、構成案からスライドの下書きを作っておきましょうか？',
      updated_at: at(9, 40),
      status: 'approval_pending',
    },
    ...HISTORY,
  ]);
  const sidebar = sidebarOf(page);
  const row = sidebar.getByRole('button', { name: /^来週の登壇資料、構成案から/ });
  await expect(row.getByRole('img', { name: '返事待ちの提案' })).toBeVisible();
  await expect(row).toHaveText(
    '来週の登壇資料、構成案からスライドの下書きを作っておきましょうか？'
  );
  // A running Action needs nothing from the user, so it has no dot.
  await expect(
    sidebar.getByRole('button', { name: /^見積書のたたき台を作る/ }).getByRole('img')
  ).toHaveCount(0);
  await waitForAnimationsToSettle(page);
  await page.screenshot({ path: info.outputPath('tasks-suggestion.png') });
  await row.click();
  await expect(page).toHaveURL(/#\/history\?item=suggestion:suggestion-1$/);
  await expect(row).toHaveAttribute('aria-current', 'true');
  await expect(
    page.getByRole('heading', {
      level: 1,
      name: '来週の登壇資料、構成案からスライドの下書きを作っておきましょうか？',
    })
  ).toBeVisible();
});
