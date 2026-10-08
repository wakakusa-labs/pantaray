import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageContext } from '@/context/uiLanguageContextShared';
import type { HistoryFetchResult } from '../../electron/src/history/historyFetch';
import { useSuggestionHistory } from './useSuggestionHistory';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

type HistoryBridge = NonNullable<NonNullable<Window['electron']>['history']>;

function installHistoryBridge(fetch: HistoryBridge['fetch']) {
  const onChanged = vi.fn<NonNullable<HistoryBridge['onChanged']>>(() => () => undefined);
  window.electron = {
    ipcRenderer: {},
    history: { fetch, onChanged },
  } as unknown as Window['electron'];
  return onChanged;
}

/** Messages are the only context the hook reads; its owner is the page's boundary. */
function Wrapper({ children }: { children: ReactNode }) {
  return (
    <UiLanguageContext.Provider
      value={{
        language: 'ja',
        setLanguage: async () => undefined,
        t: (key) => key,
        formatDateTime: (date) => date.toISOString(),
      }}
    >
      {children}
    </UiLanguageContext.Provider>
  );
}

const historyResult = (
  data: HistoryFetchResult['data'],
  nextCursor: string | null,
  unreadActionIds: string[] = []
): HistoryFetchResult => ({ data, nextCursor, unreadActionIds, error: null, errorCode: null });
const CURRENT_ITEM = {
  kind: 'conversation' as const,
  action_id: 'action-current',
  title: 'Current conversation',
  updated_at: '2026-08-30T01:02:03.456Z',
  status: 'idle' as const,
  latest_completion_event_id: 'completion-current',
};

describe('useSuggestionHistory', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    delete window.electron;
  });

  it('最初のページを取得し、検索語の変更で検索上限付きの取り直しをする', async () => {
    const second = deferred<HistoryFetchResult>();
    const fetch = vi
      .fn<HistoryBridge['fetch']>()
      .mockResolvedValueOnce(historyResult([], null))
      .mockReturnValueOnce(second.promise);
    installHistoryBridge(fetch);
    const { result } = renderHook(() => useSuggestionHistory(), { wrapper: Wrapper });

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith({
      cursor: null,
      limit: 25,
      filters: { searchText: '' },
    });
    act(() => result.current.setSearchText(`${'😀'.repeat(256)}x`));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(result.current.loading).toBe(true);
    expect(fetch.mock.calls[1][0].filters.searchText).toBe('😀'.repeat(256));
    await act(async () => second.resolve(historyResult([], null)));
    expect(result.current.loading).toBe(false);
  });

  it('同じ検索語を送り直しても読み込み中の取得を捨てない', async () => {
    const first = deferred<HistoryFetchResult>();
    const fetch = vi.fn<HistoryBridge['fetch']>().mockReturnValueOnce(first.promise);
    installHistoryBridge(fetch);
    const { result } = renderHook(() => useSuggestionHistory(), { wrapper: Wrapper });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    act(() => result.current.setSearchText(''));
    await act(async () => first.resolve(historyResult([CURRENT_ITEM], null)));

    expect(result.current.loading).toBe(false);
    expect(result.current.items).toEqual([CURRENT_ITEM]);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('検索語の変更は予約済みのrealtime refreshを捨てる', async () => {
    const fetch = vi.fn<HistoryBridge['fetch']>(async () => historyResult([], null));
    const onChanged = installHistoryBridge(fetch);
    const { result } = renderHook(() => useSuggestionHistory(), { wrapper: Wrapper });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    act(() => onChanged.mock.calls[0][0]({}));
    expect(result.current.isRealtimeSyncing).toBe(true);
    // The scheduled refresh carries the search it was scheduled with, and would land
    // after the new query and replace it.
    act(() => result.current.setSearchText('needle'));
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)));

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][0].filters.searchText).toBe('needle');
    expect(result.current.isRealtimeSyncing).toBe(false);
  });

  it('追加取得を再試行でき、realtime first pageを競合で失わない', async () => {
    const pending: Array<ReturnType<typeof deferred<HistoryFetchResult>>> = [];
    const fetch = vi.fn<HistoryBridge['fetch']>(() => {
      const request = deferred<HistoryFetchResult>();
      pending.push(request);
      return request.promise;
    });
    const onChanged = installHistoryBridge(fetch);
    const { result } = renderHook(() => useSuggestionHistory(), { wrapper: Wrapper });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await act(async () => pending[0].resolve(historyResult([CURRENT_ITEM], 'next')));
    act(() => onChanged.mock.calls[0][0]({}));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await act(async () => pending[1].resolve({ ...historyResult([], null), error: 'timeout' }));
    expect(result.current.items).toEqual([CURRENT_ITEM]);
    act(() => void result.current.loadMore());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    await act(async () => pending[2].resolve({ ...historyResult([], null), error: 'timeout' }));
    expect(result.current.items).toEqual([CURRENT_ITEM]);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.error).toBe('history.error.fetchFailed');

    act(() => void result.current.loadMore());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(fetch.mock.calls[3][0]).toEqual({
      cursor: 'next',
      limit: 25,
      filters: { searchText: '' },
    });
    act(() => onChanged.mock.calls[0][0]({}));
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)));
    expect(fetch).toHaveBeenCalledTimes(5);
    act(() => void result.current.loadMore());
    expect(fetch).toHaveBeenCalledTimes(5);
    const refreshed = { ...CURRENT_ITEM, title: 'Refreshed conversation' };
    await act(async () =>
      pending[4].resolve(historyResult([refreshed], 'fresh-next', ['action-current']))
    );
    await act(async () => pending[3].resolve(historyResult([CURRENT_ITEM], null)));
    expect(result.current.items).toEqual([refreshed]);
    expect(result.current.isRealtimeSyncing).toBe(false);

    act(() => void result.current.loadMore());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
    const suggestion = {
      kind: 'suggestion' as const,
      suggestion_id: 'suggestion-1',
      title: 'Suggestion',
      updated_at: CURRENT_ITEM.updated_at,
      status: 'idle' as const,
    };
    await act(async () => pending[5].resolve(historyResult([refreshed, suggestion], null)));
    expect(result.current.items).toEqual([refreshed, suggestion]);
    expect(result.current.isUnread(refreshed)).toBe(true);
    act(() => onChanged.mock.calls[0][0]({ source: 'read_state' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(7));
    await act(async () => pending[6].resolve(historyResult([refreshed], null)));
    expect(result.current.isUnread(refreshed)).toBe(false);
  });
  it('削除した行は消え、削除前に始まった読み取りで戻らない', async () => {
    const pending: Array<ReturnType<typeof deferred<HistoryFetchResult>>> = [];
    const fetch = vi.fn<HistoryBridge['fetch']>(() => {
      const request = deferred<HistoryFetchResult>();
      pending.push(request);
      return request.promise;
    });
    const onChanged = installHistoryBridge(fetch);
    const { result } = renderHook(() => useSuggestionHistory(), { wrapper: Wrapper });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const other = { ...CURRENT_ITEM, action_id: 'action-other', title: 'Other' };
    await act(async () => pending[0].resolve(historyResult([CURRENT_ITEM, other], null)));

    // A realtime refresh read the list before the delete committed.
    act(() => onChanged.mock.calls[0][0]({}));
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)));
    expect(fetch).toHaveBeenCalledTimes(2);
    act(() => result.current.removeItem('conversation:action-current'));
    expect(result.current.items).toEqual([other]);

    await act(async () => pending[1].resolve(historyResult([CURRENT_ITEM, other], null)));
    expect(result.current.items).toEqual([other]);
    await act(async () => pending[2].resolve(historyResult([other], null)));
    expect(result.current.items).toEqual([other]);
  });
});
