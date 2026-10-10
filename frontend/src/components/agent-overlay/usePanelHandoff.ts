import { useCallback, useEffect, useRef } from 'react';

import type { AgentOverlayState } from './model/overlayTypes';

type DecisionState = Pick<
  AgentOverlayState,
  'suggestionId' | 'reactionState' | 'currentActionId' | 'actionFailureStage'
>;

/** Closes this panel; main brings the main window forward with the Action selected. */
export function openTaskInMainWindow(actionId: string): void {
  void window.electron?.agentOverlay?.openTask?.({ actionId }).catch((error: unknown) => {
    console.error('Failed to open the task in the main window:', error);
  });
}

/**
 * Once work starts in a panel the panel closes and the main window opens the task. A decision
 * made here waits for its outcome: 承認 for the Action's start, 見送る without words for the
 * dismissal's record. A start that fails keeps the panel, which shows the failure.
 */
export function usePanelHandoff(state: DecisionState, acceptFailed: boolean) {
  const awaitingRef = useRef<{ suggestionId: string; decision: 'accept' | 'dismiss' } | null>(null);
  const { suggestionId, reactionState, currentActionId, actionFailureStage } = state;
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
      openTaskInMainWindow(currentActionId);
    }
  }, [suggestionId, reactionState, currentActionId, actionFailureStage, acceptFailed]);
  return useCallback((suggestionId: string, decision: 'accept' | 'dismiss') => {
    awaitingRef.current = { suggestionId, decision };
  }, []);
}
