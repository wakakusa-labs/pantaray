import { useCallback, useEffect, useRef, useState } from 'react';

import type { AgentOverlayState } from './model/overlayTypes';

type DecisionState = Pick<
  AgentOverlayState,
  'suggestionId' | 'reactionState' | 'currentActionId' | 'actionFailureStage'
>;

/**
 * Once work starts in a panel the panel closes and the main window opens the task. A decision
 * made here waits for its outcome: 承認 for the Action's start, 見送る without words for the
 * dismissal's record. A start that fails keeps the panel, which shows the failure; so does a
 * main window that cannot be shown, since main closes the panel only once it holds the task.
 */
export function usePanelHandoff(state: DecisionState, acceptFailed: boolean) {
  const awaitingRef = useRef<{ suggestionId: string; decision: 'accept' | 'dismiss' } | null>(null);
  // The suggestion (none for a new-task panel) whose task the main window could not show.
  const [failedFor, setFailedFor] = useState<{ suggestionId: string | null } | null>(null);
  const { suggestionId, reactionState, currentActionId, actionFailureStage } = state;
  const openTask = useCallback(
    (actionId: string) => {
      void window.electron?.agentOverlay?.openTask?.({ actionId }).then(
        () => setFailedFor(null),
        (error: unknown) => {
          console.error('Failed to open the task in the main window:', error);
          setFailedFor({ suggestionId });
        }
      );
    },
    [suggestionId]
  );
  useEffect(() => {
    const awaiting = awaitingRef.current;
    if (awaiting === null) return;
    if (awaiting.suggestionId !== suggestionId) {
      awaitingRef.current = null;
    } else if (awaiting.decision === 'dismiss') {
      if (reactionState !== 'rejected') return;
      awaitingRef.current = null;
      window.electron?.agentOverlay?.hide();
    } else if (acceptFailed || actionFailureStage !== null) {
      awaitingRef.current = null;
    } else if (currentActionId !== null) {
      awaitingRef.current = null;
      openTask(currentActionId);
    }
  }, [suggestionId, reactionState, currentActionId, actionFailureStage, acceptFailed, openTask]);
  const awaitDecisionOutcome = useCallback(
    (suggestionId: string, decision: 'accept' | 'dismiss') => {
      awaitingRef.current = { suggestionId, decision };
    },
    []
  );
  return {
    awaitDecisionOutcome,
    openTask,
    openTaskFailed: failedFor !== null && failedFor.suggestionId === suggestionId,
  };
}
