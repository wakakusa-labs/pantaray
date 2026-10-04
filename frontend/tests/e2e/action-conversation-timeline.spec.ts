import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import {
  parseActionConversationPage,
  type ActionConversationPage,
} from '../../electron/src/actions/actionContracts';
import type { ActionLiveUpdate } from '../../electron/src/actions/actionLiveCore';
import type { OverlaySnapshotPayload } from '../../src/components/agent-overlay/model/overlayTypes';
import type { AcceptActionRequest } from '../../electron/src/orchestration/eventContracts';

// Production renderer with deterministic IPC pages, isolated from live accounts and storage.
let vite: ViteDevServer;
let baseUrl: string;
test.beforeAll(async () => {
  vite = await createServer({ server: { host: '127.0.0.1', port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls!.local[0];
});
test.afterAll(async () => vite.close());
test.use({ viewport: { width: 520, height: 800 }, deviceScaleFactor: 2 });

function conversation(settled: boolean, includeLastTool = true): ActionConversationPage {
  const assistant = (step: number, content: string) => ({
    step_kind: 'assistant',
    step_id: `step-${step}`,
    step_number: step,
    content,
  });
  const tool = (step: number, subject: string) => ({
    step_kind: 'tool',
    step_id: `step-${step}`,
    step_number: step,
    label: 'read',
    subject,
    status: 'success',
    outcome: 'completed',
    output_available: true,
    output_preview: null,
    images: [],
  });
  const user = (step: number, sequence: number, content: string) => ({
    step_kind: 'user',
    approved_suggestion: null,
    step_id: `step-${step}`,
    step_number: step,
    content,
    message_id: `message-${sequence}`,
    accepted_sequence: sequence,
    images: [],
    project_refs: [],
    status: 'adopted',
  });
  return parseActionConversationPage({
    action: {
      action_id: 'action-1',
      suggestion_id: null,
      status: settled ? 'success' : 'processing',
      latest_run_id: 'run-1',
      approved_suggestion: null,
      resumable: false,
    },
    runs: [
      {
        run_id: 'run-1',
        status: settled ? 'success' : 'running',
        started_at: '2026-09-14T00:00:00.000000Z',
        completed_at: settled ? '2026-09-14T00:01:00.000000Z' : null,
        completion_event_id: settled ? 'completion-1' : null,
        final_output: settled ? '必要な変更をまとめました。' : null,
        error: null,
        entries: [
          user(1, 1, '設定ファイルを確認して'),
          assistant(2, 'まず設定の読み込み順を確認します。'),
          tool(3, 'notes-a.md'),
          assistant(4, '[関連資料](https://example.com)も確認します。'),
          tool(5, 'notes-b.md'),
          user(6, 2, '変更の影響も確認して'),
          assistant(7, '影響する箇所を確認しています。'),
          ...(includeLastTool ? [tool(8, 'notes-c.md')] : []),
        ].reverse(),
      },
    ],
    unadopted_messages: [],
    next_cursor: null,
  });
}

async function publish(page: Page, version: number, conversationPage: ActionConversationPage) {
  const update: ActionLiveUpdate = {
    kind: 'action_updated',
    snapshot: {
      actionId: 'action-1',
      page: conversationPage,
      pageVersion: version,
      transientToolSteps: [],
      approvalBlockers: [],
      lifecycle: null,
    },
  };
  await page.evaluate(
    (detail) => window.dispatchEvent(new CustomEvent('test:conversation', { detail })),
    update
  );
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
  await page.locator('[data-overlay-panel]').screenshot({ path: info.outputPath(`${name}.png`) });
}

async function installBridge(page: Page, language: 'ja' | 'en') {
  if (language === 'en') {
    await page.setViewportSize({ width: 360, height: 640 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
  }
  await page.addInitScript((locale) => {
    localStorage.setItem('pantaray_ui_language', locale);
    const noop = () => {};
    Object.defineProperty(window, 'electron', {
      value: {
        ipcRenderer: { on: () => noop, send: noop },
        agentOverlay: {
          onSnapshot: (callback: (payload: OverlaySnapshotPayload) => void) => {
            const listener = (event: Event) =>
              callback((event as CustomEvent<OverlaySnapshotPayload>).detail);
            window.addEventListener('test:snapshot', listener);
            document.documentElement.dataset.snapshotReady = 'true';
            return () => window.removeEventListener('test:snapshot', listener);
          },
          resize: noop,
          getActionApprovalMode: async () => ({ approval_mode: 'prompt_each_time' }),
        },
        orchestration: {
          onEvent: () => noop,
          onStatus: () => noop,
          acceptAction: async (request: AcceptActionRequest) => {
            document.documentElement.dataset.accepted = JSON.stringify(request);
          },
        },
        approval: {
          getWorkspaceEditCommandPreference: async () => ({ approval_mode: 'prompt_each_time' }),
        },
        actions: {
          onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
            const listener = (event: Event) =>
              callback((event as CustomEvent<ActionLiveUpdate>).detail);
            window.addEventListener('test:conversation', listener);
            document.documentElement.dataset.conversationReady = 'true';
            return () => window.removeEventListener('test:conversation', listener);
          },
          readToolOutputPage: async () => ({
            content: 'line one\nline two\nline three\nline four\nline five\nline six',
            unavailable_reason: null,
            next_cursor: null,
            truncated: false,
          }),
        },
      },
    });
  }, language);
}

for (const language of ['ja', 'en'] as const) {
  test(`${language}: chronological work, final folding, keyboard focus and narrow layout`, async ({
    page,
  }, info) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await installBridge(page, language);
    await page.goto(`${baseUrl}notification.html?mode=standalone&actionId=action-1`);
    await expect(page.locator('html')).toHaveAttribute('data-conversation-ready', 'true');
    await publish(page, 1, conversation(false, false));
    const work = page.locator('.action-conversation__work > button');
    await expect(work).toHaveCount(2);
    await work.nth(0).click();
    await work.nth(1).click();
    await publish(page, 2, conversation(false));
    await expect(work).toHaveCount(3);
    for (let index = 0; index < 3; index += 1) {
      await expect(work.nth(index)).toHaveAccessibleName(
        new RegExp(`${language === 'ja' ? '区間' : 'Section'} ${index + 1}$`)
      );
    }
    await expect(work.nth(0)).toHaveAttribute('aria-expanded', 'true');
    await expect(work.nth(1)).toHaveAttribute('aria-expanded', 'true');
    await work.nth(2).click();
    // Document order reflects execution, including the intervening user message.
    const run = page.locator('.action-conversation__run');
    await expect(run).toContainText(
      /設定ファイルを確認して[\s\S]*読み込み順[\s\S]*notes-a.md[\s\S]*関連資料[\s\S]*notes-b.md[\s\S]*変更の影響[\s\S]*影響する箇所[\s\S]*notes-c.md/
    );
    await capture(page, info, 'running');
    if (language === 'ja') {
      const outputButton = page.getByRole('button', { name: /notes-b.md/ });
      await outputButton.click();
      await expect(page.getByText('line one', { exact: false })).toBeVisible();
      await outputButton.focus();
    } else {
      await page.getByRole('link', { name: '関連資料' }).focus();
    }
    await publish(page, 3, conversation(true));
    await expect(work).toHaveCount(2);
    await expect(work.nth(0)).toBeFocused();
    await expect(work.nth(0)).toHaveAttribute('aria-expanded', 'false');
    await expect(work.nth(1)).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByText('設定ファイルを確認して', { exact: true })).toBeVisible();
    await expect(page.getByText('変更の影響も確認して', { exact: true })).toBeVisible();
    await expect(page.getByText('必要な変更をまとめました。', { exact: true })).toBeVisible();
    await expect(page.getByText('影響する箇所を確認しています。', { exact: true })).toHaveCount(0);
    await capture(page, info, 'final-folded');
    await page.keyboard.press('Enter');
    await expect(page.getByRole('link', { name: '関連資料' })).toBeVisible();
    await expect(work.nth(1)).toHaveAttribute('aria-expanded', 'false');
    await work.nth(1).focus();
    await page.keyboard.press('Space');
    await expect(page.getByText('影響する箇所を確認しています。', { exact: true })).toBeVisible();
    expect(
      await page
        .locator('.action-conversation')
        .evaluate((element) => element.scrollWidth <= element.clientWidth)
    ).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
    await capture(page, info, 'reopened');
    expect(errors).toEqual([]);
  });
}

async function backgroundStyle(page: Page) {
  return page.locator('[data-overlay-panel]').evaluate((element) => {
    const style = getComputedStyle(element, '::before');
    return [style.backgroundImage, style.filter, style.backgroundSize];
  });
}

const PROPOSAL = '[設定ファイルの変更](https://example.com)を確認しましょうか。';

function approvedConversation(settled: boolean, comment: string | null) {
  const page = conversation(settled);
  page.action.suggestion_id = 'suggestion-1';
  page.action.approved_suggestion = { suggestion_id: 'suggestion-1', content: PROPOSAL };
  page.runs[0].entries = page.runs[0].entries.map((entry) =>
    entry.step_kind === 'user' && entry.accepted_sequence === 1
      ? {
          ...entry,
          content: comment,
          approved_suggestion: { suggestion_id: 'suggestion-1', content: PROPOSAL },
        }
      : entry
  );
  return parseActionConversationPage(page);
}

for (const language of ['ja', 'en'] as const) {
  test(`${language}: accepted proposal stays unique through canonical refresh and final folding`, async ({
    page,
  }, info) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await installBridge(page, language);
    await page.goto(`${baseUrl}notification.html`);
    await expect(page.locator('html')).toHaveAttribute('data-snapshot-ready', 'true');
    const snapshot: OverlaySnapshotPayload = {
      snapshot: {
        suggestionId: 'suggestion-1',
        commandId: 'command-1',
        interactionContract: 'action_offer',
        suggestionText: PROPOSAL,
        reactionState: null,
        reactionTimestamp: null,
        actionPhase: 'idle',
        actionStatus: null,
        actionErrorCode: null,
        actionFailureStage: null,
        actionFailureMessagePublic: null,
        processId: null,
        actionId: null,
        updatedAt: '2026-09-14T00:00:00Z',
        lastSequence: 1,
        isLive: true,
      },
    };
    await page.evaluate(
      (detail) => window.dispatchEvent(new CustomEvent('test:snapshot', { detail })),
      snapshot
    );
    const initialBackground = await backgroundStyle(page);
    const badge = page.getByText(language === 'ja' ? '承認済み' : 'Approved', { exact: true });
    await expect(badge).toHaveCount(0);
    const instructionLabel =
      language === 'ja' ? '追加の指示（任意）' : 'Additional instructions (optional)';
    await page.getByRole('button', { name: instructionLabel }).click();
    const comment = language === 'ja' ? '変更のリスクも教えて' : 'Include the risks.';
    await page.getByRole('textbox', { name: instructionLabel }).fill(comment);
    await page
      .getByRole('button', { name: language === 'ja' ? '承認' : 'Accept', exact: true })
      .click();
    await expect(page.locator('html')).toHaveAttribute('data-accepted', /suggestion-1/);
    expect(JSON.parse((await page.locator('html').getAttribute('data-accepted'))!)).toMatchObject({
      suggestionId: 'suggestion-1',
      supplement: comment,
    });
    Object.assign(snapshot.snapshot, {
      reactionState: 'accepted',
      reactionTimestamp: '2026-09-14T00:00:01Z',
      actionPhase: 'processing',
      actionStatus: 'processing',
      actionId: 'action-1',
      processId: 'run-1',
      lastSequence: 2,
    });
    await page.evaluate(
      (detail) => window.dispatchEvent(new CustomEvent('test:snapshot', { detail })),
      snapshot
    );
    const link = page.getByRole('link', { name: '設定ファイルの変更' });
    await link.focus();
    await expect(badge).toHaveCount(1);
    // The run has not produced a page or a tool yet; the panel must not look idle.
    const thinking = page.getByText(language === 'ja' ? '考えています' : 'Thinking', {
      exact: true,
    });
    await expect(thinking).toBeVisible();
    await capture(page, info, 'approved-thinking');
    await publish(page, 1, approvedConversation(false, comment));
    await expect(link).toBeFocused();
    await expect(link).toHaveCount(1);
    const bubbles = page.locator('.action-conversation__user');
    await expect(bubbles.first()).toHaveText(comment);
    expect(await backgroundStyle(page)).toEqual(initialBackground);
    await capture(page, info, 'approved-running');
    await publish(page, 2, approvedConversation(true, comment));
    await expect(link).toBeFocused();
    await expect(badge).toHaveCount(1);
    await expect(bubbles.first()).toHaveText(comment);
    await expect(bubbles.last()).toHaveText('変更の影響も確認して');
    await expect(page.getByText('まず設定の読み込み順を確認します。', { exact: true })).toHaveCount(
      0
    );
    await expect(page.getByText('必要な変更をまとめました。', { exact: true })).toBeVisible();
    await expect(thinking).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
    expect(await backgroundStyle(page)).toEqual(initialBackground);
    await capture(page, info, 'approved-final');

    // Reopening history without the notification snapshot still shows its approval once.
    await page.goto(`${baseUrl}notification.html?mode=standalone&actionId=action-1`);
    await expect(page.locator('html')).toHaveAttribute('data-conversation-ready', 'true');
    await publish(page, 1, approvedConversation(true, null));
    await expect(link).toHaveCount(1);
    await expect(badge).toHaveCount(1);
    const proposalBox = (await link.boundingBox())!;
    const conversationBox = (await page.locator('.action-conversation').boundingBox())!;
    expect(proposalBox.y + proposalBox.height).toBeLessThan(conversationBox.y);
    const badgeBox = (await badge.boundingBox())!;
    expect(
      Math.abs(badgeBox.x + badgeBox.width - conversationBox.x - conversationBox.width)
    ).toBeLessThan(2);
    await expect(bubbles).toHaveCount(1);
    await expect(bubbles).toHaveText('変更の影響も確認して');
    await capture(page, info, 'approved-history-no-comment');
    expect(errors).toEqual([]);
  });
}

for (const language of ['ja', 'en'] as const) {
  test(`${language}: reopen restores a read position after delayed history and defaults to latest`, async ({
    page,
  }, info) => {
    await installBridge(page, language);
    const pages = Array.from({ length: 5 }, (_, index) => {
      const result = conversation(true);
      result.runs[0].run_id = `run-${index}`;
      result.runs[0].completion_event_id = `complete-${index}`;
      result.runs[0].entries = [];
      result.runs[0].final_output = Array.from(
        { length: 24 },
        (_, line) => `Page ${index}, line ${line}`
      ).join('\n\n');
      result.action.latest_run_id = 'run-0';
      result.next_cursor = index < 4 ? `cursor-${index + 1}` : null;
      return result;
    });
    await page.addInitScript((history) => {
      window.electron!.actions!.readConversationPage = async ({ cursor, actionId }) => {
        const result = structuredClone(history[cursor === null ? 0 : Number(cursor.split('-')[1])]);
        result.action.action_id = actionId;
        if (cursor === null) return result;
        document.documentElement.dataset.pendingCursor = cursor;
        // Release history explicitly to exercise the initial-render race deterministically.
        await new Promise<void>((resolve, reject) =>
          window.addEventListener(
            'test:release-history',
            (event) => {
              if ((event as CustomEvent<boolean>).detail) reject(new Error('History unavailable'));
              else resolve();
            },
            { once: true }
          )
        );
        return result;
      };
    }, pages);
    const open = async (target: Page, actionId = 'action-1') => {
      await target.goto(`${baseUrl}notification.html?mode=standalone&actionId=${actionId}`);
      await expect(target.locator('html')).toHaveAttribute('data-conversation-ready', 'true');
      const head = structuredClone(pages[0]);
      head.action.action_id = actionId;
      await target.evaluate(
        (detail) => window.dispatchEvent(new CustomEvent('test:conversation', { detail })),
        {
          kind: 'action_updated',
          snapshot: {
            actionId,
            page: head,
            pageVersion: 1,
            transientToolSteps: [],
            approvalBlockers: [],
            lifecycle: null,
          },
        }
      );
    };
    const release = async (index: number) => {
      await expect(page.locator('html')).toHaveAttribute('data-pending-cursor', `cursor-${index}`);
      await page.evaluate(() => window.dispatchEvent(new Event('test:release-history')));
    };
    const scroll = page.locator('[data-overlay-scroll]');
    const bottomGap = () =>
      scroll.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    await open(page);
    await expect(page.getByText('Page 0, line 23', { exact: true })).toBeAttached();
    await expect
      .poll(() => scroll.evaluate((element) => element.scrollHeight - element.clientHeight))
      .toBeGreaterThan(10);
    await expect.poll(bottomGap).toBeLessThan(2);
    await release(1);
    await release(2);
    await expect.poll(bottomGap).toBeLessThan(2);
    await scroll.evaluate((element) => {
      element.scrollTop = 0;
    });
    const older = page.getByRole('button', {
      name: language === 'ja' ? '以前のメッセージを読み込む' : 'Load older messages',
    });
    await older.click();
    await release(3);
    await expect(page.getByText('Page 3, line 0', { exact: true })).toBeAttached();
    await older.click();
    await release(4);
    await expect(page.getByText('Page 4, line 0', { exact: true })).toBeAttached();
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBe(0);
    await expect
      .poll(() =>
        page.evaluate(() =>
          JSON.parse(localStorage.getItem('pantaray.conversation-scroll:action-1')!)
        )
      )
      .toMatchObject({ top: 0, atBottom: false, pageCount: 5 });
    await scroll.evaluate((element) => {
      element.scrollTop = 300;
    });
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBe(300);
    await capture(page, info, 'saved-reading-position');
    // Reload creates a new renderer and loses all React state, like the destroyed native window.
    await open(page);
    await expect(page.locator('html')).toHaveAttribute('data-pending-cursor', 'cursor-1');
    await page.evaluate(() =>
      window.dispatchEvent(new CustomEvent('test:release-history', { detail: true }))
    );
    const retry = page.getByRole('button', {
      name:
        language === 'ja'
          ? '以前のメッセージを読み込めませんでした。再度お試しください。'
          : 'Failed to load older messages. Try again.',
    });
    await expect(retry).toBeVisible();
    // Partial history must not overwrite the last known reading position.
    expect(
      await page.evaluate(() =>
        JSON.parse(localStorage.getItem('pantaray.conversation-scroll:action-1')!)
      )
    ).toMatchObject({ top: 300, atBottom: false, pageCount: 5 });
    await retry.click();
    await release(1);
    await release(2);
    await release(3);
    await release(4);
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBe(300);
    await capture(page, info, 'restored-reading-position');
    await publish(page, 2, pages[0]);
    for (const index of [1, 2, 3, 4]) await release(index);
    await expect(page.getByText('Page 4, line 0', { exact: true })).toBeAttached();
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBe(300);

    await open(page, 'action-2');
    await expect(page.getByText('Page 0, line 23', { exact: true })).toBeAttached();
    await expect.poll(bottomGap).toBeLessThan(2);
    await expect(page.locator('html')).toHaveAttribute('data-pending-cursor', 'cursor-1');
    await page.evaluate(() =>
      window.dispatchEvent(new CustomEvent('test:release-history', { detail: true }))
    );
    await expect(retry).toBeAttached();
    await expect.poll(bottomGap).toBeLessThan(2);
    await scroll.evaluate((element) => {
      element.scrollTop = 20;
    });
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            JSON.parse(localStorage.getItem('pantaray.conversation-scroll:action-2') ?? 'null')?.top
        )
      )
      .toBe(20);
    await open(page, 'action-2');
    await release(1);
    await release(2);
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBe(20);
  });
}

for (const language of ['ja', 'en'] as const) {
  test(`${language}: transient activity keeps a restored reading position in a running conversation`, async ({
    page,
  }) => {
    await installBridge(page, language);
    const active = conversation(false);
    const request = active.runs[0].entries.find((entry) => entry.step_kind === 'user');
    if (request?.step_kind !== 'user') throw new Error('Fixture requires a user message');
    request.content = Array.from({ length: 60 }, (_, index) => `確認する項目 ${index}`).join('\n');
    const open = async () => {
      await page.goto(`${baseUrl}notification.html?mode=standalone&actionId=action-1`);
      await expect(page.locator('html')).toHaveAttribute('data-conversation-ready', 'true');
      await publish(page, 1, active);
      await expect(page.getByText(request.content!, { exact: true })).toBeAttached();
    };
    const scroll = page.locator('[data-overlay-scroll]');
    const bottomGap = () =>
      scroll.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    await open();
    await expect.poll(bottomGap).toBeLessThan(2);
    await scroll.evaluate((element) => {
      element.scrollTop = 100;
    });
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            JSON.parse(localStorage.getItem('pantaray.conversation-scroll:action-1') ?? 'null')?.top
        )
      )
      .toBe(100);
    await open();
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBe(100);
    await expect.poll(bottomGap).toBeGreaterThan(100);
    const update: ActionLiveUpdate = {
      kind: 'action_updated',
      snapshot: {
        actionId: 'action-1',
        page: active,
        pageVersion: 1,
        transientToolSteps: [
          {
            processId: 'run-1',
            runId: 'run-1',
            entry: {
              step_kind: 'tool',
              step_id: 'live-step',
              step_number: 9,
              label: 'read',
              status: 'processing',
              outcome: 'completed',
              subject: 'live-notes.md',
              output_preview: null,
              output_available: false,
              images: [],
            },
          },
        ],
        approvalBlockers: [],
        lifecycle: { processId: 'run-1', status: 'processing' },
      },
    };
    await page.evaluate(
      (detail) => window.dispatchEvent(new CustomEvent('test:conversation', { detail })),
      update
    );
    // The previous behavior jumped to the bottom within a frame of this update.
    await page.waitForTimeout(1_000);
    expect(await scroll.evaluate((element) => element.scrollTop)).toBe(100);
  });
}
