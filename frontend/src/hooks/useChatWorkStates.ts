import { useEffect, useRef, useState } from 'react';

import type { ConversationHistoryStatus } from '../../electron/src/history/historyContracts';
import type { WorkKey } from '@/components/chat/chatTimeline';
import { splitWorkKey } from '@/history/historySelection';

/** `kind` is the row's own: a suggestion that became a task reads as that task. */
export type ChatWorkState = {
  kind: 'conversation' | 'suggestion';
  title: string;
  status: ConversationHistoryStatus;
};

const HISTORY_CHANGED_REFRESH_DELAY_MS = 150;

/**
 * The current title and status of the works the chat's cards point at, each read by its key the
 * way History names one row. The dated list leaves some works out (a TODO's conversation lives
 * under its TODO), so a card is not looked up there. A deleted work reads as none, so its cards
 * show no status; a suggestion that became a task reads as that task.
 */
export function useChatWorkStates(works: readonly WorkKey[]): ReadonlyMap<WorkKey, ChatWorkState> {
  const [states, setStates] = useState<ReadonlyMap<WorkKey, ChatWorkState>>(new Map());
  const generationRef = useRef(0);
  // The keys as one value, so a new array naming the same works reads nothing again.
  // Design limit: one read per work the loaded pages name, on every change; read them in one
  // request when a chat's loaded pages name more than about 50 works.
  const wanted = [...new Set(works)].sort().join('\n');

  useEffect(() => {
    const keys = wanted === '' ? [] : (wanted.split('\n') as WorkKey[]);
    const reload = async () => {
      const generation = ++generationRef.current;
      try {
        const readItem = window.electron?.history?.readItem;
        if (!readItem) throw new Error('History bridge is unavailable.');
        const rows = await Promise.all(
          keys.map((key) => {
            const { kind, id } = splitWorkKey(key);
            return readItem({ kind: kind === 'action' ? 'conversation' : 'suggestion', id });
          })
        );
        if (generation !== generationRef.current) return;
        const next = new Map<WorkKey, ChatWorkState>();
        rows.forEach((row, index) => {
          if (row !== null) {
            next.set(keys[index], { kind: row.kind, title: row.title, status: row.status });
          }
        });
        setStates(next);
      } catch {
        // The cards keep the states last read; the chat itself does not depend on them.
      }
    };
    void reload();

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
  }, [wanted]);

  return states;
}
