import { test, expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import type { OverlaySnapshotPayload } from '../../src/components/agent-overlay/model/overlayTypes';
import type { AcceptActionRequest } from '../../electron/src/orchestration/eventContracts';
import {
  parseActionConversationPage,
  type ActionMessageRequest,
} from '../../electron/src/actions/actionContracts';
import type { ActionLiveUpdate } from '../../electron/src/actions/actionLiveCore';

// Render the production entry with deterministic IPC inputs; no backend, account, or live store.
let vite: ViteDevServer;
let baseUrl: string;
test.beforeAll(async () => {
  vite = await createServer({ server: { host: '127.0.0.1', port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls!.local[0];
});
test.afterAll(async () => {
  await vite?.close();
});
test.use({ viewport: { width: 520, height: 800 }, locale: 'ja-JP', deviceScaleFactor: 2 });

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    const noop = () => {};
    Object.defineProperty(window, 'electron', {
      value: {
        ipcRenderer: { on: () => noop },
        approval: {
          getWorkspaceEditCommandPreference: async () => ({ approval_mode: 'prompt_each_time' }),
        },
        // Dummy projects; a test empties the workspace with data-no-projects on <html>.
        workspaceSettings: {
          get: async () => {
            // prettier-ignore
            const names = ['Aurora Web', 'Nimbus API', '北極星アプリ', 'Harbor Docs', 'Lumen Design', 'Orbit Mobile', 'Pixel Studio', 'Quartz Data', 'Sierra Infra', 'Tidal Ops'];
            const paths = ['/Users/demo/projects/aurora-web', '/Users/demo/projects/aurora-shared'];
            return {
              read_access_scope: 'workspace',
              organizations: [],
              projects: (document.documentElement.dataset.noProjects ? [] : names).map(
                (display_name, sort_order) => ({
                  project_id: `project-${sort_order}`,
                  display_name,
                  sort_order,
                  organization_ids: [],
                })
              ),
              // prettier-ignore
              folders: paths.map((real_path, index) => ({ folder_id: `folder-${index}`, display_name: `folder-${index}`, real_path, canonical_real_path: real_path, organization_ids: [], project_ids: ['project-0'] })),
            };
          },
        },
        agentOverlay: {
          getActionApprovalMode: async () => ({ approval_mode: 'prompt_each_time' }),
          openWorkspaceSettings: () => {
            document.documentElement.dataset.workspaceOpened = 'true';
          },
          onSnapshot: (callback: (payload: OverlaySnapshotPayload) => void) => {
            const listener = (event: Event) =>
              callback((event as CustomEvent<OverlaySnapshotPayload>).detail);
            window.addEventListener('test:snapshot', listener);
            document.documentElement.dataset.snapshotReady = 'true';
            return () => window.removeEventListener('test:snapshot', listener);
          },
          resize: (height: number) => {
            document.documentElement.dataset.overlayHeight = String(height);
          },
        },
        orchestration: {
          onEvent: () => noop,
          onStatus: () => noop,
          acceptAction: async (request: AcceptActionRequest) => {
            document.documentElement.dataset.accepted = JSON.stringify(request);
          },
        },
        actions: {
          onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
            const listener = (event: Event) =>
              callback((event as CustomEvent<ActionLiveUpdate>).detail);
            window.addEventListener('test:conversation', listener);
            document.documentElement.dataset.conversationReady = 'true';
            return () => window.removeEventListener('test:conversation', listener);
          },
          submitMessage: async (request: ActionMessageRequest) => {
            document.documentElement.dataset.submitted = JSON.stringify(request);
            return { kind: 'action_conflict' };
          },
          attachFile: async ({ bytes, name }: { bytes: ArrayBuffer; name: string }) => {
            const staged = Number(document.documentElement.dataset.staged ?? 0) + 1;
            document.documentElement.dataset.staged = String(staged);
            const attachmentId = `${staged}0000000-0000-4000-8000-000000000000`;
            return { attachmentId, name, byteSize: bytes.byteLength };
          },
          discardAttachment: async ({ attachmentId }: { attachmentId: string }) => {
            document.documentElement.dataset.discarded = attachmentId;
          },
        },
      },
    });
  });
});

async function showSuggestion(
  page: Page,
  kind: 'message_only' | 'action_offer',
  id = 'suggestion-1'
) {
  // prettier-ignore
  const payload: OverlaySnapshotPayload = { snapshot: {
    suggestionId: id, commandId: kind === 'action_offer' ? 'command-1' : null, interactionContract: kind,
    suggestionText: kind === 'message_only'
      ? '午後の打ち合わせ、前回のメモに気になる点が残っていました。\n\n決めたいことを先に整理しておくと、話を進めやすそうです。'
      : '午後の打ち合わせに向けて、前回のメモから未決事項をまとめておきましょうか。',
    reactionState: null, reactionTimestamp: null, actionPhase: 'idle', actionStatus: null,
    actionErrorCode: null, actionFailureStage: null, actionFailureMessagePublic: null,
    processId: null, actionId: null, updatedAt: '2026-09-11T00:00:00Z', lastSequence: 1, isLive: true,
  } };
  await expect(page.locator('html')).toHaveAttribute('data-snapshot-ready', 'true');
  await page.evaluate(
    (detail) => window.dispatchEvent(new CustomEvent('test:snapshot', { detail })),
    payload
  );
  await expect(page.getByText(payload.snapshot.suggestionText.split('\n')[0])).toBeVisible();
}

async function capture(page: Page, info: TestInfo, name: string) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      document
        .getAnimations()
        .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
        .map((animation) => animation.finished)
    );
  });
  const panel = page.locator('[data-overlay-panel]');
  await expect
    .poll(async () => Number(await page.locator('html').getAttribute('data-overlay-height')))
    .toBeGreaterThanOrEqual(Math.ceil((await panel.boundingBox())!.height));
  await panel.screenshot({ path: info.outputPath(`${name}.png`), omitBackground: true });
}

test('comment: quiet entry, keyboard reveal, resize and reply', async ({ page }, info) => {
  await page.goto(`${baseUrl}notification.html`);
  await showSuggestion(page, 'message_only');
  await expect(page.getByRole('textbox')).toHaveCount(0);
  const reply = page.getByRole('button', { name: 'この提案に返信' });
  await capture(page, info, 'comment-collapsed');
  const initialHeight = (await page.locator('[data-overlay-panel]').boundingBox())!.height;
  await reply.focus();
  await page.keyboard.press('Enter');
  const input = page.getByRole('textbox', { name: 'メッセージ', exact: true });
  await expect(input).toBeFocused();
  await capture(page, info, 'comment-expanded');
  expect((await page.locator('[data-overlay-panel]').boundingBox())!.height).toBeGreaterThan(
    initialHeight
  );
  await input.fill('決めたいことを3つに整理して');
  await input.press('Enter');
  const request = JSON.parse((await page.locator('html').getAttribute('data-submitted'))!);
  expect(request.target).toMatchObject({ kind: 'new', reply_to_suggestion_id: 'suggestion-1' });
  expect(request.message.content).toBe('決めたいことを3つに整理して');
});

test('offer: inline decisions, expanded instructions and fresh suggestion reset', async ({
  page,
}, info) => {
  await page.goto(`${baseUrl}notification.html`);
  await showSuggestion(page, 'action_offer');
  const open = page.getByRole('button', { name: '追加の指示（任意）' });
  const accept = page.getByRole('button', { name: '承認', exact: true });
  await expect(page.getByRole('textbox')).toHaveCount(0);
  const iconBox = (await open.boundingBox())!;
  const acceptBox = (await accept.boundingBox())!;
  expect(
    Math.abs(iconBox.y + iconBox.height / 2 - acceptBox.y - acceptBox.height / 2)
  ).toBeLessThan(2);
  expect(acceptBox.x).toBeGreaterThan(iconBox.x + iconBox.width);
  await capture(page, info, 'offer-collapsed');
  await open.click();
  const input = page.getByRole('textbox', { name: '追加の指示（任意）' });
  await expect(input).toBeFocused();
  await input.fill('未決事項だけを、箇条書きでまとめて');
  await expect(page.getByRole('button', { name: 'ファイルを追加' })).toBeVisible();
  await page.getByRole('button', { name: /ファイル編集・コマンド実行の権限/ }).click();
  await page.getByRole('menuitemradio', { name: /自動承認/ }).click();
  await capture(page, info, 'offer-expanded');
  const composerBox = (await page.locator('.overlay-composer').boundingBox())!;
  const expandedAcceptBox = (await accept.boundingBox())!;
  const dismissBox = (await page
    .getByRole('button', { name: '見送る', exact: true })
    .boundingBox())!;
  expect(expandedAcceptBox.y).toBeGreaterThan(composerBox.y + composerBox.height);
  expect(Math.abs(expandedAcceptBox.y - dismissBox.y)).toBeLessThan(2);
  expect(expandedAcceptBox.x).toBeGreaterThan(dismissBox.x + dismissBox.width);
  await showSuggestion(page, 'action_offer', 'suggestion-2');
  await expect(open).toBeVisible();
  await open.click();
  await expect(input).toHaveValue('');
  await expect(page.getByRole('button', { name: /権限: 毎回確認/ })).toBeVisible();
  await input.fill('未決事項だけを、箇条書きでまとめて');
  await accept.click();
  expect(JSON.parse((await page.locator('html').getAttribute('data-accepted'))!)).toMatchObject({
    suggestionId: 'suggestion-2',
    supplement: '未決事項だけを、箇条書きでまとめて',
    approvalMode: 'prompt_each_time',
    images: [],
  });
});

test('user-started Action opens the regular composer immediately', async ({ page }, info) => {
  await page.goto(`${baseUrl}notification.html?mode=standalone`);
  await expect(page.getByRole('textbox', { name: 'メッセージ', exact: true })).toBeFocused();
  await expect(page.getByRole('button', { name: 'この提案に返信' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'ファイルを追加' })).toBeVisible();
  await capture(page, info, 'standalone-unchanged');
});

test('narrow English layout and reduced motion keep the controls reachable', async ({
  page,
}, info) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 360, height: 640 });
  await page.addInitScript(() => localStorage.setItem('pantaray_ui_language', 'en'));
  await page.goto(`${baseUrl}notification.html`);
  await showSuggestion(page, 'action_offer');
  const open = page.getByRole('button', { name: 'Additional instructions (optional)' });
  const controlsId = await open.getAttribute('aria-controls');
  await open.focus();
  await page.keyboard.press('Space');
  await expect(
    page.getByRole('textbox', { name: 'Additional instructions (optional)' })
  ).toBeFocused();
  await expect(page.getByRole('button', { name: 'Accept', exact: true })).toBeInViewport();
  expect(
    await page.locator(`[id="${controlsId}"]`).evaluate((element) => element.getAnimations().length)
  ).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(360);
  await capture(page, info, 'offer-narrow-reduced-motion');
});

const recorded = async (page: Page, key: string) =>
  JSON.parse((await page.locator('html').getAttribute(`data-${key}`))!);

// The list stays outside the composer frame on either side.
async function expectPlacement(input: Locator, listbox: Locator, placement: 'above' | 'below') {
  const field = (await input.locator('xpath=ancestor::form').boundingBox())!;
  const list = (await listbox.boundingBox())!;
  if (placement === 'above') expect(list.y + list.height).toBeLessThan(field.y);
  else expect(list.y).toBeGreaterThan(field.y + field.height);
}

test('continuation: list floats above, filters, closes, and sends the reference', async ({
  page,
}, info) => {
  await page.goto(`${baseUrl}notification.html?mode=standalone&actionId=action-1`);
  await expect(page.locator('html')).toHaveAttribute('data-conversation-ready', 'true');
  const entry = (step: number, content: string) =>
    step % 2
      ? // prettier-ignore
        { step_kind: 'user', approved_suggestion: null, step_id: `step-${step}`, step_number: step, content, message_id: `message-${step}`, accepted_sequence: step, images: [], project_refs: [], status: 'adopted' }
      : { step_kind: 'assistant', step_id: `step-${step}`, step_number: step, content };
  // prettier-ignore
  const conversation = parseActionConversationPage({
    action: { action_id: 'action-1', suggestion_id: null, status: 'success', latest_run_id: 'run-1', approved_suggestion: null, resumable: false },
    runs: [{ run_id: 'run-1', status: 'success', started_at: '2026-09-14T00:00:00.000000Z', completed_at: '2026-09-14T00:01:00.000000Z', completion_event_id: 'completion-1', final_output: '変更点を3つにまとめました。', error: null,
      entries: [entry(1, '先週の議事録を整理して'), entry(2, '議事録を読み、決定事項と宿題に分けます。'), entry(3, '宿題は担当者ごとに'), entry(4, '担当者ごとに並べ替えます。'), entry(5, '期限が近い順にして'), entry(6, '期限の近い順に並べ、今週分に印を付けました。'), entry(7, '共有用の文面も作って')].reverse() }],
    unadopted_messages: [],
    next_cursor: null,
  });
  const update: ActionLiveUpdate = {
    kind: 'action_updated',
    // prettier-ignore
    snapshot: { actionId: 'action-1', page: conversation, pageVersion: 1, transientToolSteps: [], approvalBlockers: [], lifecycle: null },
  };
  await page.evaluate((detail) => {
    window.dispatchEvent(new CustomEvent('test:conversation', { detail }));
  }, update);
  const input = page.getByRole('textbox', { name: 'メッセージ', exact: true });
  await input.click();
  await page.keyboard.type('🚀 次は@');
  const listbox = page.getByRole('listbox', { name: 'プロジェクト' });
  await expect(listbox.getByRole('option')).toHaveCount(11);
  await expect(listbox.getByRole('option').last()).toHaveText('プロジェクトを追加');
  await expectPlacement(input, listbox, 'above');
  await expect(input).toHaveAttribute('aria-expanded', 'true');
  // Seven project rows scroll; "Add project" stays pinned under them and ↑ reaches it.
  const addProject = listbox.getByRole('option', { name: 'プロジェクトを追加' });
  const lastProject = listbox.getByRole('option', { name: 'Tidal Ops' });
  await expect(addProject).toBeInViewport();
  await expect(lastProject).not.toBeInViewport();
  await capture(page, info, 'mention-above-conversation-v2');
  await page.keyboard.press('ArrowUp');
  await expect(addProject).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('ArrowUp');
  await expect(lastProject).toBeInViewport();
  await expect(addProject).toBeInViewport();
  await page.keyboard.type('zz');
  await expect(listbox).toHaveCount(0);
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Backspace');
  await page.keyboard.type('AU');
  await expect(listbox.getByRole('option')).toHaveText(['Aurora Web', 'プロジェクトを追加']);
  await capture(page, info, 'mention-filtering');
  await page.keyboard.press('Escape');
  await expect(listbox).toHaveCount(0);
  await page.keyboard.type('r');
  await expect(listbox).toHaveCount(0);
  await input.fill('🚀 次は');
  await page.keyboard.type('＠au');
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue('🚀 次はAurora Web ');
  const mention = page.locator('.overlay-composer [aria-hidden="true"] span', {
    hasText: 'Aurora Web',
  });
  await expect(mention).toHaveCSS('color', 'rgb(168, 208, 255)');
  await capture(page, info, 'mention-confirmed-v2');
  await page.keyboard.type('を確認して');
  await page.keyboard.press('Enter');
  expect((await recorded(page, 'submitted')).message).toMatchObject({
    content: '🚀 次はAurora Web を確認して',
    project_refs: [
      {
        project_id: 'project-0',
        display_name: 'Aurora Web',
        paths: [expect.stringContaining('aurora-web'), expect.stringContaining('aurora-shared')],
        start: 4,
        end: 14,
      },
    ],
  });
});

test('new conversation: small window opens the list below; none registered offers Add', async ({
  page,
}, info) => {
  await page.goto(`${baseUrl}notification.html?mode=standalone`);
  const input = page.getByRole('textbox', { name: 'メッセージ', exact: true });
  await expect(input).toBeFocused();
  await page.evaluate(() => (document.documentElement.dataset.noProjects = 'true'));
  await page.keyboard.type('一行目');
  await page.keyboard.press('Shift+Enter');
  await page.keyboard.type('@');
  const listbox = page.getByRole('listbox', { name: 'プロジェクト' });
  await expect(listbox.getByRole('option')).toHaveText(['プロジェクトを追加']);
  await expectPlacement(input, listbox, 'below');
  await capture(page, info, 'mention-below-no-projects-v2');
  await listbox.getByRole('option').click();
  await expect(page.locator('html')).toHaveAttribute('data-workspace-opened', 'true');
  await expect(listbox).toHaveCount(0);
  await expect(input).toBeFocused();
});

test('suggestion reply and approval supplement carry picked projects', async ({ page }, info) => {
  await page.goto(`${baseUrl}notification.html`);
  await showSuggestion(page, 'message_only');
  await page.getByRole('button', { name: 'この提案に返信' }).click();
  await page.keyboard.type('@');
  const listbox = page.getByRole('listbox', { name: 'プロジェクト' });
  await expectPlacement(page.getByRole('textbox'), listbox, 'below');
  await capture(page, info, 'mention-below-small-window-v2');
  await page.keyboard.type('nim');
  await page.getByRole('option', { name: 'Nimbus API' }).click();
  await page.keyboard.press('Enter');
  expect((await recorded(page, 'submitted')).message.project_refs).toEqual([
    { project_id: 'project-1', display_name: 'Nimbus API', paths: [], start: 0, end: 10 },
  ]);

  await page.goto(`${baseUrl}notification.html`);
  await showSuggestion(page, 'action_offer');
  await page.getByRole('button', { name: '追加の指示（任意）' }).click();
  await page.keyboard.type('  @北極');
  await page.keyboard.press('Tab');
  await page.keyboard.type('だけ');
  await page.getByRole('button', { name: '承認', exact: true }).click();
  expect(await recorded(page, 'accepted')).toMatchObject({
    supplement: '北極星アプリ だけ',
    supplementProjectRefs: [{ project_id: 'project-2', start: 0, end: 6 }],
  });
});

for (const language of ['ja', 'en'] as const) {
  test(`documents: chip, keyboard removal, send, and history (${language})`, async ({
    page,
  }, info) => {
    const copy = {
      ja: {
        message: 'メッセージ',
        attach: 'ファイルを追加',
        you: 'あなた',
        files: '添付ファイル 1 件',
      },
      en: { message: 'Message', attach: 'Add files', you: 'You', files: '1 attached file' },
    }[language];
    const remove = (name: string) => (language === 'ja' ? `${name} を削除` : `Remove ${name}`);
    await page.addInitScript(
      (value) => localStorage.setItem('pantaray_ui_language', value),
      language
    );
    await page.goto(`${baseUrl}notification.html?mode=standalone&actionId=action-1`);
    await expect(page.locator('html')).toHaveAttribute('data-conversation-ready', 'true');
    // prettier-ignore
    const sent = { step_kind: 'user', approved_suggestion: null, step_id: 'step-1', step_number: 1, message_id: 'message-1', accepted_sequence: 1, content: '先月の見積書を確認して', images: [], project_refs: [], files: [{ name: '2026年8月 見積書（改訂版・最終）.pdf', byte_size: 1_258_291 }], status: 'adopted' };
    // prettier-ignore
    const conversation = parseActionConversationPage({
      action: { action_id: 'action-1', suggestion_id: null, status: 'success', latest_run_id: 'run-1', approved_suggestion: null, resumable: false },
      runs: [{ run_id: 'run-1', status: 'success', started_at: '2026-09-29T00:00:00.000000Z', completed_at: '2026-09-29T00:01:00.000000Z', completion_event_id: 'completion-1', final_output: '合計金額と支払条件を確認しました。', error: null, entries: [sent] }],
      unadopted_messages: [],
      next_cursor: null,
    });
    // prettier-ignore
    const update: ActionLiveUpdate = { kind: 'action_updated', snapshot: { actionId: 'action-1', page: conversation, pageVersion: 1, transientToolSteps: [], approvalBlockers: [], lifecycle: null } };
    await page.evaluate((detail) => {
      window.dispatchEvent(new CustomEvent('test:conversation', { detail }));
    }, update);

    const history = page.getByRole('article', { name: copy.you });
    await expect(history.getByRole('list', { name: copy.files })).toContainText('1.2 MB');
    await expect(history.getByRole('button')).toHaveCount(0);
    await capture(page, info, `documents-history-${language}`);

    await expect(page.getByRole('button', { name: copy.attach })).toBeVisible();
    await page.locator('.overlay-composer input[type="file"]').setInputFiles([
      { name: 'minutes.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7 minutes') },
      { name: 'analysis.ipynb', mimeType: '', buffer: Buffer.from('{"cells": []}') },
    ]);
    const composer = page.locator('.overlay-composer');
    await expect(composer.getByText('minutes.pdf')).toBeVisible();
    await expect(composer.getByText('16 B')).toBeVisible();
    await capture(page, info, `documents-composer-${language}`);

    await composer.getByRole('button', { name: remove('analysis.ipynb') }).focus();
    await page.keyboard.press('Enter');
    await expect(composer.getByText('analysis.ipynb')).toHaveCount(0);
    await expect(page.locator('html')).toHaveAttribute(
      'data-discarded',
      '20000000-0000-4000-8000-000000000000'
    );
    await expect(composer.getByRole('button', { name: remove('minutes.pdf') })).toBeFocused();

    const input = page.getByRole('textbox', { name: copy.message, exact: true });
    await input.fill('議事録を要約して');
    await input.press('Enter');
    expect((await recorded(page, 'submitted')).message.files).toEqual([
      { attachment_id: '10000000-0000-4000-8000-000000000000', name: 'minutes.pdf', byte_size: 16 },
    ]);
  });
}
