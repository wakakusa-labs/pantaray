import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { ActionLiveUpdate } from '../../../electron/src/actions/actionLiveCore';
import { createActionPage } from './actionTaskFixtures';
import { TaskWorkspace } from './TaskWorkspace';

type ActionFiles = NonNullable<NonNullable<Window['electron']>['actionFiles']>;

let listeners: Set<(update: ActionLiveUpdate) => void>;
const read = vi.fn<ActionFiles['read']>();
const openInApp = vi.fn<ActionFiles['openInApp']>();

const ANSWER = [
  'Rebuilt the quote: [quote_v3.html](pantaray-file:///work/quote/quote_v3.html)',
  'and the mail pantaray-file:///work/quote/mail.md , the summary pantaray-file:///work/quote/summary.pdf',
  'with pantaray-file:///work/quote/run.log , pantaray-file:///work/quote/chart.png and pantaray-file:///work/quote/rates.xlsx',
].join('\n');

function emitPage(page: ReturnType<typeof createActionPage>, pageVersion: number) {
  act(() =>
    listeners.forEach((listener) =>
      listener({
        kind: 'action_updated',
        snapshot: {
          actionId: 'act-1',
          page,
          pageVersion,
          transientToolSteps: [],
          approvalBlockers: [],
          lifecycle: null,
        },
      })
    )
  );
}

/** The first run's answer, then a follow-up run that finished later. */
function followUpPage() {
  const first = createActionPage('act-1', 'success', 'run-1');
  first.runs[0].final_output = ANSWER;
  const second = createActionPage('act-1', 'success', 'run-2');
  second.runs[0].started_at = '2026-10-10T00:01:00.000000Z';
  second.runs[0].completed_at = '2026-10-10T00:01:05.000000Z';
  // A page lists its runs newest first.
  return { ...second, runs: [...second.runs, ...first.runs] };
}

async function renderFinishedTask() {
  const rendered = render(
    <UiLanguageProvider initialLanguage="ja">
      <TaskWorkspace
        actionId="act-1"
        title="見積書を作り直す"
        onShowInChat={() => {}}
        onAddProject={() => {}}
      />
    </UiLanguageProvider>
  );
  await screen.findByRole('button', { name: /毎回確認/ });
  const page = createActionPage('act-1', 'success');
  page.runs[0].final_output = ANSWER;
  emitPage(page, 1);
  return rendered;
}

const chip = (name: string) => screen.getByRole('button', { name: new RegExp(name) });

beforeEach(() => {
  listeners = new Set();
  read.mockReset();
  openInApp.mockReset().mockResolvedValue({ kind: 'opened' });
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      actions: {
        openConversation: vi.fn(async () => {}),
        submitMessage: vi.fn(() => new Promise(() => {})),
        resumeAction: vi.fn(() => new Promise(() => {})),
        readConversationPage: vi.fn(),
        readToolOutputPage: vi.fn(),
        discardAttachment: vi.fn(),
        onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
          listeners.add(callback);
          return () => listeners.delete(callback);
        },
      },
      orchestration: { send: vi.fn() },
      actionFiles: { open: vi.fn(), read, openInApp },
      agentOverlay: {
        getActionApprovalMode: vi.fn(async (actionId: string) => ({
          action_id: actionId,
          approval_mode: 'prompt_each_time',
          source: 'user_default',
        })),
      },
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('TaskWorkspace', () => {
  it('opens a document beside the conversation and closes it, keeping the draft', async () => {
    read.mockResolvedValue({
      kind: 'text',
      text: '<!DOCTYPE html><h1>御見積書</h1><script>parent.alert(1)</script>',
      truncated: false,
    });
    const { container } = await renderFinishedTask();
    const message = screen.getByRole('textbox', { name: 'メッセージ' });
    fireEvent.change(message, { target: { value: '単価をもう一度確認して' } });
    expect(chip('quote_v3.html').getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(chip('quote_v3.html'));
    const preview = await screen.findByRole('region', { name: 'quote_v3.html' });
    expect(read).toHaveBeenCalledWith({ actionId: 'act-1', path: '/work/quote/quote_v3.html' });
    expect(chip('quote_v3.html').getAttribute('aria-pressed')).toBe('true');
    expect(container.querySelector('.action-task--split')).not.toBeNull();

    const frame = await waitFor(() => {
      const found = preview.querySelector('iframe');
      expect(found).not.toBeNull();
      return found as HTMLIFrameElement;
    });
    expect(frame.getAttribute('sandbox')).toBe('');
    const document = frame.getAttribute('srcdoc') ?? '';
    expect(document).toMatch(
      /^<!DOCTYPE html><meta http-equiv="Content-Security-Policy" content="default-src 'none';/
    );
    expect(document).toContain('<h1>御見積書</h1>');

    // The conversation stayed mounted: the same composer still holds the draft.
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toBe(message);
    expect((message as HTMLTextAreaElement).value).toBe('単価をもう一度確認して');

    fireEvent.click(screen.getByRole('button', { name: 'プレビューを閉じる' }));
    expect(screen.queryByRole('region', { name: 'quote_v3.html' })).toBeNull();
    expect(container.querySelector('.action-task--split')).toBeNull();
    expect(chip('quote_v3.html').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toBe(message);
    expect((message as HTMLTextAreaElement).value).toBe('単価をもう一度確認して');
  });

  it('renders a Markdown document and says when only its beginning is shown', async () => {
    read.mockResolvedValue({ kind: 'text', text: '# 送付メール\n\n本文', truncated: true });
    await renderFinishedTask();
    fireEvent.click(chip('mail.md'));
    expect(await screen.findByRole('heading', { level: 1, name: '送付メール' })).toBeTruthy();
    expect(screen.getByText('途中まで表示しています。')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'いつものアプリで開く' })).toHaveLength(2);

    // Its chip again closes the preview.
    fireEvent.click(chip('mail.md'));
    expect(screen.queryByRole('region', { name: 'mail.md' })).toBeNull();
  });

  it('shows other text as it is, an image as a picture, and a PDF in the PDF viewer', async () => {
    read.mockResolvedValueOnce({ kind: 'text', text: 'step 1 ok\nstep 2 ok', truncated: false });
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:chart');
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    await renderFinishedTask();

    fireEvent.click(chip('run.log'));
    expect((await screen.findByText(/step 2 ok/)).tagName).toBe('PRE');

    read.mockResolvedValueOnce({ kind: 'image', bytes: new Uint8Array([1]), mime: 'image/png' });
    fireEvent.click(chip('chart.png'));
    expect((await screen.findByRole('img', { name: 'chart.png' })).getAttribute('src')).toBe(
      'blob:chart'
    );

    fireEvent.click(chip('summary.pdf'));
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:chart');
    const frame = screen.getByTitle('summary.pdf');
    expect(frame.getAttribute('src')).toMatch(
      /^pantaray-action-file:\/\/local\/\?action=act-1&path=%2Fwork%2Fquote%2Fsummary\.pdf&revision=run-1/
    );
    // A sandboxed frame turns the PDF viewer off.
    expect(frame.hasAttribute('sandbox')).toBe(false);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('offers the default app for a file it cannot show, and opens it there', async () => {
    read.mockResolvedValue({ kind: 'unavailable', reason: 'binary' });
    await renderFinishedTask();
    fireEvent.click(chip('run.log'));
    const preview = await screen.findByRole('region', { name: 'run.log' });
    expect(await screen.findByText('このファイルはここでは表示できません。')).toBeTruthy();

    // A spreadsheet is never read here; it goes straight to the default app.
    fireEvent.click(chip('rates.xlsx'));
    expect(preview.isConnected).toBe(false);
    expect(read).toHaveBeenCalledTimes(1);
    expect(screen.getByText('このファイルはここでは表示できません。')).toBeTruthy();

    const [headerButton, fallbackButton] = screen.getAllByRole('button', {
      name: 'いつものアプリで開く',
    });
    fireEvent.click(fallbackButton);
    await waitFor(() =>
      expect(openInApp).toHaveBeenCalledWith({ actionId: 'act-1', path: '/work/quote/rates.xlsx' })
    );

    openInApp.mockResolvedValue({ kind: 'unavailable', reason: 'open_failed' });
    fireEvent.click(headerButton);
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'ファイルを開けませんでした。'
    );
  });

  it('reads the open file again when a later run finishes, as it may have rewritten it', async () => {
    read.mockResolvedValueOnce({ kind: 'text', text: '# 見積書 v1', truncated: false });
    await renderFinishedTask();
    fireEvent.click(chip('mail.md'));
    expect(await screen.findByRole('heading', { name: '見積書 v1' })).toBeTruthy();

    read.mockResolvedValueOnce({ kind: 'text', text: '# 見積書 v2', truncated: false });
    emitPage(followUpPage(), 2);
    expect(await screen.findByRole('heading', { name: '見積書 v2' })).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(2);
    expect(read).toHaveBeenLastCalledWith({ actionId: 'act-1', path: '/work/quote/mail.md' });

    // The PDF frame's URL changes with the run, so it loads the file again.
    fireEvent.click(chip('summary.pdf'));
    expect(screen.getByTitle('summary.pdf').getAttribute('src')).toContain('revision=run-2');
  });
});
