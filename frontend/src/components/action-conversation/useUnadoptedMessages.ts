import { useCallback, useState } from 'react';

import type { ActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import type { ActionLiveSnapshot } from '../../../electron/src/actions/actionLiveCore';
import { actionStopTarget } from '../agent-overlay/useActionStop';

/** Send now for the messages waiting for a run, or null while it is not offered. */
export type SendNow = Readonly<{ state: 'sending' | 'failed' | null; send: () => void }>;

type SendNowRequest = { actionId: string; processId: string; state: 'sending' | 'failed' };

/**
 * What the user can do with the messages no run has taken yet: withdraw one, or send the ones
 * waiting for the running run now. Send now stops that run and resumes it, and the resumed run
 * takes every waiting message, in the order sent.
 */
export function useUnadoptedMessages(
  view: ActionConversationView,
  lifecycle: ActionLiveSnapshot['lifecycle']
) {
  const actionId = view.action?.action_id ?? null;
  // Hidden as soon as it is withdrawn: a page read before then can still hold it.
  const [withdrawn, setWithdrawn] = useState<ReadonlySet<string>>(() => new Set());
  const withdraw = useCallback(
    async (stepId: string) => {
      const actions = window.electron?.actions;
      if (actionId === null || !actions) return;
      const result = await actions.withdrawMessage({ actionId, stepId });
      // A run took it first: main has refreshed the conversation, which shows it as run.
      if (result.kind === 'withdrawn') setWithdrawn((current) => new Set(current).add(stepId));
    },
    [actionId]
  );
  const items = view.items.filter(
    (item) =>
      !(item.kind === 'user' && item.source === 'canonical' && withdrawn.has(item.entry.step_id))
  );

  // Offered while the Stop is: not while the run waits for an approval, which the user decides.
  const stopTarget = actionStopTarget(lifecycle, view.action && { action: view.action }, null);
  const target =
    stopTarget !== null &&
    !view.items.some(
      (item) =>
        item.kind === 'run' && item.runId === stopTarget && item.status === 'approval_pending'
    )
      ? stopTarget
      : null;
  // A request applies only while its run is the one going, as a Stop's does.
  const [request, setRequest] = useState<SendNowRequest | null>(null);
  const state =
    request !== null && request.actionId === actionId && request.processId === target
      ? request.state
      : null;
  const send = () => {
    const actions = window.electron?.actions;
    if (!actions || actionId === null || target === null || state === 'sending') return;
    const processId = target;
    setRequest({ actionId, processId, state: 'sending' });
    // Once it succeeds the run it stopped is no longer the one going, which ends the request.
    actions.sendNow({ actionId, processId }).catch((error: unknown) => {
      console.error('Failed to send the waiting messages now:', error);
      setRequest((current) =>
        current?.actionId === actionId && current.processId === processId
          ? { actionId, processId, state: 'failed' }
          : current
      );
    });
  };
  const sendNow: SendNow | null = actionId !== null && target !== null ? { state, send } : null;
  return { items, withdraw, sendNow };
}
