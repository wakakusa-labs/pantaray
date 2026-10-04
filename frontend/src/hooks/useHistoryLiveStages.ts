import { useEffect, useState } from 'react';

import { selectHistoryLiveStage, type HistoryLiveStage } from '@/history/historyLiveStage';

type LiveStages = ReadonlyMap<string, HistoryLiveStage>;

const NO_STAGES: LiveStages = new Map();

/**
 * The current stage of every running Action this window has heard about, keyed by action id.
 * The preload replays each Action's latest update on subscribe, so a remount starts complete.
 */
export function useHistoryLiveStages(): LiveStages {
  const [stages, setStages] = useState<LiveStages>(NO_STAGES);
  useEffect(() => {
    const subscribe = window.electron?.actions?.onConversationUpdated;
    if (!subscribe) return;
    return subscribe((update) => {
      if (update.kind === 'reset') {
        setStages(NO_STAGES);
        return;
      }
      const { actionId } = update.snapshot;
      const stage = selectHistoryLiveStage(update.snapshot);
      setStages((current) => {
        if (JSON.stringify(current.get(actionId) ?? null) === JSON.stringify(stage)) return current;
        const next = new Map(current);
        if (stage === null) next.delete(actionId);
        else next.set(actionId, stage);
        return next;
      });
    });
  }, []);
  return stages;
}
