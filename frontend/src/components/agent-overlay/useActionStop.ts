import { useState } from 'react';

import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import type { ActionLiveSnapshot } from '../../../electron/src/actions/actionLiveCore';

export type ActionStopState = 'stopping' | 'failed';

type StopRequest = { actionId: string; processId: string; state: ActionStopState };

/**
 * The run a Stop ends, or null when none is going. The lifecycle notification is authoritative
 * until the canonical page read replaces it. `pageless` is the run a window knows of before any
 * page is read.
 */
export function actionStopTarget(
  lifecycle: ActionLiveSnapshot['lifecycle'],
  page: ActionConversationPage | null,
  pageless: string | null
): string | null {
  if (lifecycle) return lifecycle.status === 'processing' ? lifecycle.processId : null;
  if (page === null) return pageless;
  const status = page.action.status;
  return status === 'queued' || status === 'processing' ? page.action.latest_run_id : null;
}

/**
 * Stop for the run a window shows, the same in the Overlay and the main window's task pane.
 *
 * A run with subagents ends only once they have stopped too, so an accepted Stop stays visible
 * until the run's terminal arrives. The request applies only while its run is still the one
 * shown: a run that ended, or the next one, starts with a Stop of its own.
 */
export function useActionStop({
  actionId,
  stopTarget,
  running,
}: {
  actionId: string | null;
  /** The run the Stop ends, or null when none is going. */
  stopTarget: string | null;
  running: boolean;
}) {
  const [request, setRequest] = useState<StopRequest | null>(null);
  const state =
    request !== null && request.actionId === actionId && request.processId === stopTarget
      ? request.state
      : null;
  const stopping = running && state === 'stopping';
  const canStop = running && actionId !== null && stopTarget !== null && !stopping;
  const stopState = running ? state : null;

  const stop = () => {
    const actions = window.electron?.actions;
    if (!canStop || !actions || actionId === null || stopTarget === null) return;
    const processId = stopTarget;
    setRequest({ actionId, processId, state: 'stopping' });
    actions
      .stopAction({ actionId, processId })
      .catch(() =>
        setRequest((current) =>
          current?.actionId === actionId && current.processId === processId
            ? { actionId, processId, state: 'failed' }
            : current
        )
      );
  };

  /** The Stop control is shown: pressable, or disabled while an accepted Stop settles. */
  const stopShown = canStop || stopping;
  return { stopShown, stopState, stop };
}
