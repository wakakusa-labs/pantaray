import { test, expect, type Locator, type Page } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import type { TodoCreateRequest } from '../../electron/src/todos/todoContracts';

// The Option+Space panel from the production entry, with deterministic IPC inputs: no Electron,
// no backend. Main's window follows the height the panel asks for (`data-overlay-height`).
let vite: ViteDevServer;
let baseUrl: string;
test.beforeAll(async () => {
  vite = await createServer({ server: { host: '127.0.0.1', port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls!.local[0];
});
test.afterAll(async () => vite?.close());
test.use({ viewport: { width: 400, height: 800 }, locale: 'ja-JP', timezoneId: 'Asia/Tokyo' });

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-11-16T10:00:00+09:00'));
  await page.addInitScript(() => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    const noop = () => {};
    const record = (key: string, value: unknown) =>
      (document.documentElement.dataset[key] = JSON.stringify(value));
    Object.defineProperty(window, 'electron', {
      value: {
        ipcRenderer: { on: () => noop },
        workspaceSettings: {
          get: async () => ({
            read_access_scope: 'workspace',
            organizations: [
              { organization_id: 'xer', display_name: 'Xer', icon_data_url: null },
              {
                organization_id: 'wk',
                display_name: 'Wakakusa Holdings International',
                icon_data_url: null,
              },
            ],
            // prettier-ignore
            projects: [
              { project_id: 'pantaray', display_name: 'Pantaray', sort_order: 0, organization_ids: ['xer'] },
              { project_id: 'sales', display_name: '営業資料', sort_order: 1, organization_ids: ['xer'] },
              { project_id: 'site', display_name: 'pantaray.app のランディングページ改修', sort_order: 2, organization_ids: ['wk'] },
            ],
            folders: [],
          }),
        },
        todos: {
          create: async (request: TodoCreateRequest) => {
            record('created', request);
            // prettier-ignore
            return { kind: 'ok', result: {
              todo_id: 'todo-1', project_id: request.project_id, organization_id: request.organization_id,
              text: request.text, author: 'user', origin: { kind: 'written' }, starts_on: request.starts_on ?? '2026-11-16',
              due: request.due, deletion_request: null, conversation: { kind: 'none' },
              state: { kind: 'not_started', reason: request.starts_on === undefined ? 'starting' : 'scheduled' },
            } };
          },
        },
        actions: {
          attachFile: async ({ bytes, name }: { bytes: ArrayBuffer; name: string }) => ({
            attachmentId: '0f8fad5b-d9cb-469f-a165-70867728950e',
            name,
            byteSize: bytes.byteLength,
          }),
          attachImage: async () => ({
            kind: 'stored',
            storagePath: 'user-1/2026-11-16/3f2b9a8e-1c4d-4e5f-8a6b-7c8d9e0f1a2b.png',
          }),
          discardAttachment: async (request: unknown) => record('discarded', request),
        },
        agentOverlay: {
          onSnapshot: () => noop,
          resize: (height: number) =>
            (document.documentElement.dataset.overlayHeight = `${height}`),
          hide: () => record('hidden', true),
          openTask: async (request: unknown) => record('opened', request),
        },
        orchestration: { onEvent: () => noop, onStatus: () => noop },
      },
    });
  });
  await page.goto(`${baseUrl}notification.html?mode=standalone`);
});

const recorded = async (page: Page, key: string) =>
  JSON.parse((await page.locator('html').getAttribute(`data-${key}`))!);
const windowHeight = async (page: Page) =>
  Number(await page.locator('html').getAttribute('data-overlay-height'));

/** The window grows to hold what opened under the panel, with no sideways scroll. */
async function expectWindowHolds(page: Page, popover: Locator, closed: number) {
  await expect(popover).toBeVisible();
  // Polled: the calendar lays out its days once it is open.
  const form = (await page.locator('form.quick-todo').boundingBox())!;
  await expect
    .poll(async () => {
      const box = (await popover.boundingBox())!;
      const height = await windowHeight(page);
      return box.y > form.y && height >= box.y + box.height && height > closed;
    })
    .toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(400);
}

test('the pickers work from the keyboard, and a TODO starting today opens in the main window', async ({
  page,
}) => {
  const field = page.getByRole('textbox', { name: 'メッセージ' });
  await expect(field).toBeFocused();
  await expect.poll(() => windowHeight(page)).toBeGreaterThan(0);
  const closed = await windowHeight(page);

  const organization = page.getByRole('button', { name: '組織: 組織なし' });
  await organization.focus();
  await page.keyboard.press('Enter');
  const organizations = page.getByRole('menu', { name: '組織' });
  await expect(organizations.getByRole('menuitemradio', { name: '組織なし' })).toBeFocused();
  await expectWindowHolds(page, organizations, closed);
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  await expect(organizations).toBeHidden();
  await expect(page.getByRole('button', { name: '組織: Xer' })).toBeFocused();
  await expect.poll(() => windowHeight(page)).toBe(closed);

  const project = page.getByRole('button', { name: 'プロジェクト: その他' });
  await project.click();
  const projects = page.getByRole('menu', { name: 'プロジェクト' });
  await expect(projects.getByRole('menuitemradio')).toHaveText(['Pantaray', '営業資料', 'その他']);
  await expectWindowHolds(page, projects, closed);
  await page.keyboard.press('Escape');
  await expect(projects).toBeHidden();
  await expect(project).toBeFocused();
  await expect.poll(() => windowHeight(page)).toBe(closed);
  await project.click();
  await expect(projects.getByRole('menuitemradio', { name: 'その他' })).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(projects.getByRole('menuitemradio', { name: 'Pantaray' })).toBeFocused();
  await page.keyboard.press('Space');
  await expect(page.getByRole('button', { name: 'プロジェクト: Pantaray' })).toBeFocused();

  await field.fill('来週の定例の資料を、前回の議事録から叩き台まで作っておいて');
  await field.press('Enter');
  expect(await recorded(page, 'created')).toMatchObject({
    project_id: 'pantaray',
    organization_id: null,
    text: '来週の定例の資料を、前回の議事録から叩き台まで作っておいて',
    due: null,
  });
  expect((await recorded(page, 'created')).starts_on).toBeUndefined();
  expect(await recorded(page, 'opened')).toEqual({ kind: 'todo', id: 'todo-1' });
});

test('dates are picked on the calendar under the panel, and a later TODO just closes it', async ({
  page,
}) => {
  await expect.poll(() => windowHeight(page)).toBeGreaterThan(0);
  const closed = await windowHeight(page);
  await page.getByRole('button', { name: '今日から、期限なし' }).click();
  const picker = page.getByRole('dialog', { name: '開始日と期限' });
  await expectWindowHolds(page, picker, closed);
  await picker.getByRole('button', { name: '2026年11月18日(水)' }).click();
  await picker.getByRole('button', { name: '2026年11月20日(金)' }).click();
  await expect(picker).toBeHidden();
  const dates = page.getByRole('button', { name: '水 11/18から、期限 金 11/20' });
  await expect(dates).toBeFocused();
  await expect(dates).toHaveText('11/18 → 11/20');
  // The window gives the calendar's room back: it is the panel's height again, whose row of
  // choices the dates shown may have wrapped.
  await expect
    .poll(async () => windowHeight(page))
    .toBeLessThan(
      Math.ceil((await page.locator('[data-overlay-panel]').boundingBox())!.height) + 40
    );

  await page.getByRole('textbox', { name: 'メッセージ' }).fill('見積書を出す');
  await page.getByRole('button', { name: '送信' }).click();
  expect(await recorded(page, 'created')).toMatchObject({
    project_id: null,
    organization_id: null,
    starts_on: '2026-11-18',
    due: { date: '2026-11-20', repeat: 'none' },
  });
  expect(await recorded(page, 'hidden')).toBe(true);
  await expect(page.locator('html')).not.toHaveAttribute('data-opened');
});

test('long names are cut short, and the dates and send stay on the one row', async ({ page }) => {
  await page.getByRole('button', { name: /^組織/ }).click();
  await page.getByRole('menuitemradio', { name: 'Wakakusa Holdings International' }).click();
  await page.getByRole('button', { name: /^プロジェクト/ }).click();
  await page.getByRole('menuitemradio', { name: /ランディングページ/ }).click();
  await page.getByRole('button', { name: '今日から、期限なし' }).click();
  const picker = page.getByRole('dialog', { name: '開始日と期限' });
  await picker.getByRole('button', { name: '2026年11月18日(水)' }).click();
  await picker.getByRole('button', { name: '2026年11月20日(金)' }).click();
  const row = [
    page.getByRole('button', { name: /^組織/ }),
    page.getByRole('button', { name: /^プロジェクト/ }),
    page.getByRole('button', { name: '水 11/18から、期限 金 11/20' }),
    page.getByRole('button', { name: '送信' }),
  ];
  const boxes = await Promise.all(row.map(async (item) => (await item.boundingBox())!));
  for (const box of boxes)
    expect(Math.round(box.y + box.height / 2)).toBe(Math.round(boxes[0].y + boxes[0].height / 2));
  const frame = (await page.locator('form.quick-todo').boundingBox())!;
  expect(boxes[3].x + boxes[3].width).toBeLessThanOrEqual(frame.x + frame.width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(400);
});

test('files and images go with the TODO, and the row stays one line under their chips', async ({
  page,
}) => {
  await expect.poll(() => windowHeight(page)).toBeGreaterThan(0);
  const closed = await windowHeight(page);
  await page.getByRole('button', { name: /^組織/ }).click();
  await page.getByRole('menuitemradio', { name: 'Wakakusa Holdings International' }).click();
  await page.getByRole('button', { name: /^プロジェクト/ }).click();
  await page.getByRole('menuitemradio', { name: /ランディングページ/ }).click();
  await page.locator('input[type="file"]').setInputFiles([
    {
      name: '第3四半期の営業報告書_最終版.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.7'),
    },
    { name: 'shot.png', mimeType: 'image/png', buffer: Buffer.from([137, 80, 78, 71]) },
  ]);
  const chips = page.getByRole('list', { name: '添付 2 件' });
  await expect(chips).toBeVisible();
  // The window grows to hold the chips above the row.
  await expect.poll(() => windowHeight(page)).toBeGreaterThan(closed);

  const row = [
    page.getByRole('button', { name: 'ファイルを追加' }),
    page.getByRole('button', { name: /^組織/ }),
    page.getByRole('button', { name: /^プロジェクト/ }),
    page.getByRole('button', { name: '今日から、期限なし' }),
    page.getByRole('button', { name: '送信' }),
  ];
  const boxes = await Promise.all(row.map(async (item) => (await item.boundingBox())!));
  for (const box of boxes)
    expect(Math.round(box.y + box.height / 2)).toBe(Math.round(boxes[0].y + boxes[0].height / 2));
  const frame = (await page.locator('form.quick-todo').boundingBox())!;
  expect(boxes[4].x + boxes[4].width).toBeLessThanOrEqual(frame.x + frame.width);
  expect((await chips.boundingBox())!.y).toBeLessThan(boxes[0].y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(400);

  await page.getByRole('textbox', { name: 'メッセージ' }).fill('報告書の要点をまとめる');
  await page.getByRole('button', { name: '送信' }).click();
  expect(await recorded(page, 'created')).toMatchObject({
    files: [
      {
        attachment_id: '0f8fad5b-d9cb-469f-a165-70867728950e',
        name: '第3四半期の営業報告書_最終版.pdf',
        byte_size: 8,
      },
    ],
    images: [
      { kind: 'image', storage_path: 'user-1/2026-11-16/3f2b9a8e-1c4d-4e5f-8a6b-7c8d9e0f1a2b.png' },
    ],
  });
  // Sent, the files are the TODO's: nothing is discarded.
  await expect(page.locator('html')).not.toHaveAttribute('data-discarded');
});
