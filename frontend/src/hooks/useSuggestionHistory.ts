import { useCallback, useEffect, useRef, useState } from 'react';

import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import { useI18n } from '@/context/useI18n';

type UseSuggestionHistoryResult = {
  items: ConversationHistoryListItem[];
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  searchText: string;
  setSearchText: (searchText: string) => void;
  loadMore: () => Promise<void>;
  hasMore: boolean;
  isRealtimeSyncing: boolean;
  isUnread: (item: ConversationHistoryListItem) => boolean;
  /** Drops a row whose conversation was deleted. */
  removeItem: (identity: string) => void;
};

const HISTORY_PAGE_SIZE = 25;
const HISTORY_MAX_SEARCH_CODE_POINTS = 256;
const AUTHENTICATION_REQUIRED_ERROR_CODE = 'AUTHENTICATION_REQUIRED';
const HISTORY_CHANGED_REFRESH_DELAY_MS = 150;

export function limitHistorySearchText(searchText: string): string {
  return [...searchText].slice(0, HISTORY_MAX_SEARCH_CODE_POINTS).join('');
}

export function itemIdentity(item: ConversationHistoryListItem): string {
  return item.kind === 'conversation'
    ? `conversation:${item.action_id}`
    : `suggestion:${item.suggestion_id}`;
}

function appendUniqueItems(
  current: ConversationHistoryListItem[],
  incoming: ConversationHistoryListItem[]
): ConversationHistoryListItem[] {
  const identities = new Set(current.map(itemIdentity));
  return [...current, ...incoming.filter((item) => !identities.has(itemIdentity(item)))];
}

function unreadCompletions(
  items: ConversationHistoryListItem[],
  unreadActionIds: string[]
): Map<string, string> {
  const unread = new Set(unreadActionIds);
  return new Map(
    items.flatMap((item) =>
      item.kind === 'conversation' &&
      item.latest_completion_event_id !== null &&
      unread.has(item.action_id)
        ? [[item.action_id, item.latest_completion_event_id] as const]
        : []
    )
  );
}

/**
 * The conversation list of the owner this page was mounted for.
 *
 * `LocalOwnerBoundary` renders the page only for a confirmed owner and remounts it when the
 * owner changes, so this hook never re-checks the owner: it holds one owner for its whole
 * life, and main names the owner of every request itself and refuses one whose owner changed
 * while it was in flight (electron/src/history/historyFetch.ts). What is left is the order
 * within that one owner — a request generation that a search change, a reload and a realtime
 * refresh all bump, and a first page that has to land before its cursor can be used.
 */
export const useSuggestionHistory = (): UseSuggestionHistoryResult => {
  const { t } = useI18n();
  const [searchText, updateSearchText] = useState('');
  const [items, setItems] = useState<ConversationHistoryListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [unreadByAction, setUnreadByAction] = useState<Map<string, string>>(new Map());
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRealtimeSyncing, setRealtimeSyncing] = useState(false);
  const requestGenerationRef = useRef(0);
  const firstPageRequestRef = useRef<number | null>(null);
  const pendingRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clearPendingRefresh = useCallback(() => {
    if (pendingRefreshRef.current === null) return;
    clearTimeout(pendingRefreshRef.current);
    pendingRefreshRef.current = null;
  }, []);

  // A new search changes `runFetch`, so the effects below drop the old read and any scheduled
  // refresh. The same search changes nothing and must not touch the read in flight.
  const setSearchText = useCallback((next: string) => {
    updateSearchText(limitHistorySearchText(next));
  }, []);

  const runFetch = useCallback(
    async (cursor: string | null, silent: boolean): Promise<void> => {
      const requestGeneration = ++requestGenerationRef.current;
      const isFirstPage = cursor === null;
      if (isFirstPage) firstPageRequestRef.current = requestGeneration;

      try {
        if (!window.electron?.history?.fetch) {
          setItems([]);
          setError(t('history.error.bridgeUnavailable'));
          return;
        }
        if (isFirstPage && !silent) {
          setItems([]);
          setUnreadByAction(new Map());
          setNextCursor(null);
          setLoading(true);
          setError(null);
        } else if (!isFirstPage) {
          setLoadingMore(true);
        }

        const fetchHistory = window.electron.history.fetch;
        const response = await fetchHistory({
          cursor,
          limit: HISTORY_PAGE_SIZE,
          filters: { searchText },
        });
        if (requestGeneration !== requestGenerationRef.current) return;
        if (response.error !== null) {
          setError(
            response.errorCode === AUTHENTICATION_REQUIRED_ERROR_CODE
              ? t('history.error.authenticationRequired')
              : t('history.error.fetchFailed')
          );
          return;
        }
        setItems((current) =>
          isFirstPage ? response.data : appendUniqueItems(current, response.data)
        );
        setUnreadByAction((current) => {
          const incoming = unreadCompletions(response.data, response.unreadActionIds);
          if (isFirstPage) return incoming;
          const merged = new Map(current);
          for (const [actionId, completionEventId] of incoming) {
            if (!merged.has(actionId)) merged.set(actionId, completionEventId);
          }
          return merged;
        });
        setNextCursor(response.nextCursor);
        setError(null);
      } catch {
        if (requestGeneration === requestGenerationRef.current) {
          setError(t('history.error.fetchFailed'));
        }
      } finally {
        if (firstPageRequestRef.current === requestGeneration) {
          firstPageRequestRef.current = null;
        }
        if (requestGeneration === requestGenerationRef.current) {
          setLoading(false);
          setLoadingMore(false);
          setRealtimeSyncing(false);
        }
      }
    },
    [searchText, t]
  );

  const loadMore = useCallback(
    () =>
      nextCursor === null || firstPageRequestRef.current !== null
        ? Promise.resolve()
        : runFetch(nextCursor, false),
    [nextCursor, runFetch]
  );

  useEffect(() => {
    requestGenerationRef.current += 1;
    const timer = window.setTimeout(() => void runFetch(null, false), 0);
    return () => window.clearTimeout(timer);
  }, [runFetch]);

  useEffect(() => {
    const onChanged = window.electron?.history?.onChanged;
    if (!onChanged) return;
    const unsubscribe = onChanged(() => {
      clearPendingRefresh();
      setRealtimeSyncing(true);
      pendingRefreshRef.current = setTimeout(() => {
        pendingRefreshRef.current = null;
        void runFetch(null, true);
      }, HISTORY_CHANGED_REFRESH_DELAY_MS);
    });
    return () => {
      unsubscribe();
      // The refresh this notification scheduled would carry the search it was scheduled
      // with, and after unmount it would reach main on behalf of whoever owns it then.
      clearPendingRefresh();
      setRealtimeSyncing(false);
    };
  }, [clearPendingRefresh, runFetch]);

  const removeItem = useCallback(
    (identity: string) => {
      setItems((current) => current.filter((item) => itemIdentity(item) !== identity));
      // A page read before the delete committed would bring the row back; this read replaces it.
      void runFetch(null, true);
    },
    [runFetch]
  );

  const isUnread = useCallback(
    (item: ConversationHistoryListItem) =>
      item.kind === 'conversation' &&
      item.latest_completion_event_id !== null &&
      unreadByAction.get(item.action_id) === item.latest_completion_event_id,
    [unreadByAction]
  );
  return {
    items,
    loading,
    loadingMore,
    error,
    searchText,
    setSearchText,
    loadMore,
    hasMore: nextCursor !== null,
    isRealtimeSyncing,
    isUnread,
    removeItem,
  };
};
