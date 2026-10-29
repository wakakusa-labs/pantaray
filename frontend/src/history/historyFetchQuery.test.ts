import { describe, expect, it, vi } from 'vitest';

import type { LocalRuntimeState } from '../../electron/src/auth/localRuntimeState';
import { createHistoryFetcher } from '../../electron/src/history/historyFetch';

type HistoryRequestJson = Parameters<typeof createHistoryFetcher>[0]['requestJson'];

const READY_RUNTIME: LocalRuntimeState = {
  status: 'ready',
  message: null,
  owner: { kind: 'account', id: 'owner-a' },
};

const PAGE = {
  items: [
    {
      kind: 'conversation',
      action_id: 'action-unread',
      title: 'Fix the report',
      updated_at: '2026-08-30T01:02:03.456Z',
      status: 'idle',
      latest_completion_event_id: 'event-new',
      status_line: 'The report is fixed',
      artifact_names: ['report.md', 'deck.pdf'],
    },
    {
      kind: 'conversation',
      action_id: 'action-running',
      title: 'Still working',
      updated_at: '2026-08-30T01:01:03.456Z',
      status: 'running',
      latest_completion_event_id: 'event-running',
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'suggestion',
      suggestion_id: 'suggestion-1',
      title: 'Review this',
      updated_at: '2026-08-30T01:00:03.456Z',
      status: 'approval_pending',
      artifact_names: [],
    },
  ],
  next_cursor: 'cursor-next',
};

const REQUEST = {
  cursor: 'cursor-current',
  limit: 100,
  filters: { searchText: ' café ' },
} as const;

describe('createHistoryFetcher', () => {
  it.each(['guest', 'account'] as const)(
    '%s の strict page を取得し、完了した Action だけを未読にする',
    async (kind) => {
      const requestJsonMock = vi.fn(async () => PAGE);
      const runtimeState: LocalRuntimeState = { ...READY_RUNTIME, owner: { kind, id: 'owner-a' } };
      const fetchHistory = createHistoryFetcher({
        getRuntimeState: () => runtimeState,
        requestJson: requestJsonMock as unknown as HistoryRequestJson,
        getCompletionUnreadSnapshot: () => () => true,
      });

      const result = await fetchHistory(REQUEST);

      expect(result).toEqual({
        data: PAGE.items,
        nextCursor: 'cursor-next',
        unreadActionIds: ['action-unread'],
        error: null,
        errorCode: null,
      });
      expect(requestJsonMock).toHaveBeenCalledWith({
        path: '/api/agent/history',
        method: 'GET',
        query: {
          cursor: 'cursor-current',
          limit: 100,
          search_text: ' café ',
        },
        timeoutMs: 30_000,
      });
    }
  );

  it('応答待機中の同じownerの既読更新を反映する', async () => {
    let releaseResponse!: () => void;
    const responseGate = new Promise<void>((resolve) => {
      releaseResponse = resolve;
    });
    const scope = { viewed: new Set<string>() };
    const fetchHistory = createHistoryFetcher({
      getRuntimeState: () => READY_RUNTIME,
      requestJson: (async () => {
        await responseGate;
        return PAGE;
      }) as HistoryRequestJson,
      getCompletionUnreadSnapshot: () => {
        const requestScope = scope;
        return (_actionId, eventId) => eventId !== null && !requestScope.viewed.has(eventId);
      },
    });

    const pending = fetchHistory(REQUEST);
    scope.viewed.add('event-new');
    releaseResponse();

    await expect(pending).resolves.toMatchObject({ unreadActionIds: [] });
  });

  it.each([
    { ...REQUEST, limit: 0 },
    { ...REQUEST, limit: 101 },
    { ...REQUEST, filters: { ...REQUEST.filters, searchText: '😀'.repeat(257) } },
  ])('invalid request %# は HTTP を呼ばない', async (request) => {
    const requestJsonMock = vi.fn(async () => PAGE);
    const fetchHistory = createHistoryFetcher({
      getRuntimeState: () => READY_RUNTIME,
      requestJson: requestJsonMock as unknown as HistoryRequestJson,
      getCompletionUnreadSnapshot: () => () => false,
    });

    const result = await fetchHistory(request);

    expect(result.data).toEqual([]);
    expect(result.error).toBeTruthy();
    expect(requestJsonMock).not.toHaveBeenCalled();
  });

  it.each([
    {
      ...PAGE,
      items: [{ ...PAGE.items[0], private_failure: 'do not expose' }],
    },
    {
      ...PAGE,
      items: [{ ...PAGE.items[0], updated_at: '2026-08-30T01:02:03Z' }],
    },
    { ...PAGE, total_count: 3 },
    { ...PAGE, items: [{ ...PAGE.items[0], status_line: undefined }] },
    { ...PAGE, items: [{ ...PAGE.items[0], status_line: ' ' }] },
    { ...PAGE, items: [{ ...PAGE.items[0], status_line: '😀'.repeat(121) }] },
    { ...PAGE, items: [{ ...PAGE.items[2], status_line: null }] },
    { ...PAGE, items: [{ ...PAGE.items[0], artifact_names: undefined }] },
    { ...PAGE, items: [{ ...PAGE.items[0], artifact_names: [''] }] },
    { ...PAGE, items: [{ ...PAGE.items[2], artifact_names: ['/Users/me/notes.md', 1] }] },
  ])(
    'private field・extra field・非 canonical timestamp・不正な状況行やファイル名をページ全体で拒否する',
    async (page) => {
      const fetchHistory = createHistoryFetcher({
        getRuntimeState: () => READY_RUNTIME,
        requestJson: vi.fn(async () => page) as unknown as HistoryRequestJson,
        getCompletionUnreadSnapshot: () => () => true,
      });

      const result = await fetchHistory(REQUEST);

      expect(result).toEqual({
        data: [],
        nextCursor: null,
        unreadActionIds: [],
        error: 'Invalid conversation history response.',
        errorCode: null,
      });
    }
  );

  it('local backend の typed error code を renderer 契約へ保持する', async () => {
    const { LocalBackendRequestError } = await import('../../electron/src/localBackend/client');
    const requestJson = vi.fn(async () => {
      throw new LocalBackendRequestError(
        'Authentication required. Please sign in again.',
        401,
        'AUTHENTICATION_REQUIRED'
      );
    });
    const fetchHistory = createHistoryFetcher({
      getRuntimeState: () => READY_RUNTIME,
      requestJson: requestJson as unknown as HistoryRequestJson,
      getCompletionUnreadSnapshot: () => () => false,
    });

    const result = await fetchHistory(REQUEST);

    expect(result).toEqual({
      data: [],
      nextCursor: null,
      unreadActionIds: [],
      error: 'Authentication required. Please sign in again.',
      errorCode: 'AUTHENTICATION_REQUIRED',
    });
  });

  it.each(['ready', 'syncing', 'degraded'] as const)(
    '%s でもownerが無効ならHTTPへ送らない',
    async (status) => {
      const requestJson = vi.fn(async () => PAGE);
      const fetchHistory = createHistoryFetcher({
        getRuntimeState: () => ({ status, message: null, owner: null }),
        requestJson: requestJson as unknown as HistoryRequestJson,
        getCompletionUnreadSnapshot: () => () => true,
      });
      expect(await fetchHistory(REQUEST)).toEqual({
        data: [],
        nextCursor: null,
        unreadActionIds: [],
        error: 'Local owner is unavailable.',
        errorCode: null,
      });
      expect(requestJson).not.toHaveBeenCalled();
    }
  );

  it.each(['owner-b', 'owner-a'])('切替後の%sに古いページ・cursor・未読を返さない', async (id) => {
    let runtimeState = READY_RUNTIME;
    let resolve!: (page: typeof PAGE) => void;
    const pending = new Promise<typeof PAGE>((settle) => {
      resolve = settle;
    });
    const fetchHistory = createHistoryFetcher({
      getRuntimeState: () => runtimeState,
      requestJson: (() => pending) as HistoryRequestJson,
      getCompletionUnreadSnapshot: () => () => true,
    });
    const result = fetchHistory(REQUEST);
    runtimeState = { ...runtimeState, owner: null };
    runtimeState = { ...runtimeState, owner: { kind: 'account', id: 'owner-b' } };
    runtimeState = { ...runtimeState, owner: { kind: 'account', id } };
    resolve(PAGE);
    expect(await result).toEqual({
      data: [],
      nextCursor: null,
      unreadActionIds: [],
      error: 'Local owner changed.',
      errorCode: null,
    });
  });
});
