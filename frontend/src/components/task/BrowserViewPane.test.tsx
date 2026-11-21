import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BrowserViewUpdate,
  BrowserViewWatchResult,
} from '../../../electron/src/browser/pageViewer';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { BrowserViewPane } from './BrowserViewPane';

let listeners: Set<(update: BrowserViewUpdate) => void>;
const watch = vi.fn<(request: { actionId: string }) => Promise<BrowserViewWatchResult>>();
const unwatch = vi.fn<(request: { watchId: string }) => Promise<void>>();

function emit(update: BrowserViewUpdate) {
  act(() => listeners.forEach((listener) => listener(update)));
}

const frame = (watchId: string, jpeg: string): BrowserViewUpdate => ({
  kind: 'frame',
  watchId,
  jpeg,
  width: 1280,
  height: 800,
});

function renderPane() {
  return render(
    <UiLanguageProvider initialLanguage="ja">
      <BrowserViewPane actionId="act-1" onClose={() => {}} />
    </UiLanguageProvider>
  );
}

/** Renders the pane and waits until main's watch id has reached it. */
async function renderWatching() {
  const rendered = renderPane();
  await act(async () => {});
  return rendered;
}

const status = () => screen.getByRole('status');

beforeEach(() => {
  listeners = new Set();
  watch.mockReset().mockResolvedValue({ kind: 'watching', watchId: 'w-1' });
  unwatch.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      browserView: {
        watch,
        unwatch,
        onUpdate: (callback: (update: BrowserViewUpdate) => void) => {
          listeners.add(callback);
          return () => listeners.delete(callback);
        },
      },
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('BrowserViewPane', () => {
  it("watches the Action's page while shown and stops watching once closed", async () => {
    const { unmount } = await renderWatching();
    expect(watch).toHaveBeenCalledWith({ actionId: 'act-1' });
    expect(status().textContent).toBe('ページを読み込んでいます…');

    unmount();
    expect(unwatch).toHaveBeenCalledWith({ watchId: 'w-1' });
    // Nothing reaches the closed pane.
    expect(listeners.size).toBe(0);
  });

  it('stops a watch that main granted only after the pane had closed', async () => {
    let grant: (result: BrowserViewWatchResult) => void = () => {};
    watch.mockReturnValue(new Promise((resolve) => (grant = resolve)));
    const { unmount } = renderPane();
    unmount();
    expect(unwatch).not.toHaveBeenCalled();

    await act(async () => grant({ kind: 'watching', watchId: 'w-late' }));
    expect(unwatch).toHaveBeenCalledWith({ watchId: 'w-late' });
  });

  it('shows the latest frame of its own watch and ignores another watch', async () => {
    await renderWatching();
    emit(frame('w-old', 'T0xE'));
    expect(screen.queryByRole('img')).toBeNull();

    emit(frame('w-1', 'Rk9P'));
    emit(frame('w-1', 'QkFS'));
    const image = screen.getByRole('img', { name: 'ページの画面' });
    expect(image.getAttribute('src')).toBe('data:image/jpeg;base64,QkFS');
    // Live, the state says nothing: frames are not announced.
    expect(status().textContent).toBe('');

    emit({ kind: 'page', watchId: 'w-1', url: 'https://example.com/a', title: '見積フォーム' });
    expect(screen.getByRole('img', { name: '見積フォーム の画面' })).toBe(image);
    emit({ kind: 'page', watchId: 'w-old', url: 'https://other.example', title: '別のページ' });
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('見積フォーム');
  });

  it('keeps the last frame dimmed and says why once the page has closed', async () => {
    await renderWatching();
    emit(frame('w-1', 'Rk9P'));
    emit({ kind: 'ended', watchId: 'w-1', reason: 'work_ended' });
    const image = screen.getByRole('img');
    expect(image.getAttribute('src')).toBe('data:image/jpeg;base64,Rk9P');
    expect(image.classList.contains('browser-view__frame--ended')).toBe(true);
    expect(status().textContent).toBe('作業が終わったのでページは閉じました。');
  });

  it('says the page stopped responding when it crashed', async () => {
    await renderWatching();
    emit(frame('w-1', 'Rk9P'));
    emit({ kind: 'ended', watchId: 'w-1', reason: 'page_crashed' });
    expect(status().textContent).toBe('ページが応答しなくなったので閉じました。');
  });

  it('says no page is open when the Action has none, and when the watch failed', async () => {
    watch.mockResolvedValueOnce({ kind: 'not_live' });
    renderPane();
    await waitFor(() => expect(status().textContent).toBe('いま開いているページはありません。'));
    cleanup();

    vi.spyOn(console, 'error').mockImplementation(() => {});
    watch.mockRejectedValueOnce(new Error('ipc'));
    renderPane();
    await waitFor(() => expect(status().textContent).toBe('ページを表示できませんでした。'));
  });

  it("shows the page's title and URL as plain text, never as markup or a link", async () => {
    await renderWatching();
    const title = '<img src=x onerror=alert(1)><b>Sign in</b>';
    emit({ kind: 'page', watchId: 'w-1', url: 'javascript:alert(1)', title });
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(title);
    expect(screen.getByText('javascript:alert(1)').tagName).toBe('P');
    expect(screen.queryByRole('link')).toBeNull();
    expect(document.querySelector('b')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
  });
});
