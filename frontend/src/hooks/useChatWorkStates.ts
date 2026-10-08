import { useCallback, useEffect, useRef, useState } from 'react';

import type { ConversationHistoryStatus } from '../../electron/src/history/historyContracts';
import type { WorkKey } from '@/components/chat/chatTimeline';

export type ChatWorkState = { title: string; status: ConversationHistoryStatus };

// Design limit: a card names its work's title and status only while that work is among the 100
// most recently updated; older works are idle in practice and show their summary alone. Add a
// lookup by id to the history API when a card for a running work is reported without its status.
const WORK_STATE_PAGE_SIZE = 100;
const HISTORY_CHANGED_REFRESH_DELAY_MS = 150;

/**
 * The current title and status of the works the chat's cards point at, read from the same
 * history list the list mode shows. A deleted work is simply absent, so its cards show no status.
 */
export function useChatWorkStates(): {
  states: ReadonlyMap<WorkKey, ChatWorkState>;
  reload: () => Promise<void>;
} {
  const [states, setStates] = useState<ReadonlyMap<WorkKey, ChatWorkState>>(new Map());
  const generationRef = useRef(0);

  const reload = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const fetchHistory = window.electron?.history?.fetch;
      if (!fetchHistory) throw new Error('History bridge is unavailable.');
      const response = await fetchHistory({
        cursor: null,
        limit: WORK_STATE_PAGE_SIZE,
        filters: { searchText: '' },
      });
      if (generation !== generationRef.current || response.error !== null) return;
      setStates(
        new Map(
          response.data.map((item) => [
            item.kind === 'conversation'
              ? (`action:${item.action_id}` as const)
              : (`suggestion:${item.suggestion_id}` as const),
            { title: item.title, status: item.status },
          ])
        )
      );
    } catch {
      // The cards keep the states last read; the chat itself does not depend on them.
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const onChanged = window.electron?.history?.onChanged;
    if (!onChanged) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = onChanged(() => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        void reload();
      }, HISTORY_CHANGED_REFRESH_DELAY_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [reload]);

  return { states, reload };
}
