import { useEffect, useRef, useState } from 'react';

import type {
  ApiDenial,
  IntegrationConnectionState,
  IntegrationConnectResult,
} from '../../../../electron/src/settings/integrationSettingsFetch';

/** How often the backend is asked whether the browser step finished. */
const POLL_INTERVAL_MS = 2_000;

type Target = {
  toolkit: string;
  /** The account being reconnected or disconnected; null while connecting a new one. */
  account: string | null;
};

/** One connect, reconnect or disconnect at a time; null when none is shown. */
export type IntegrationFlow = Target &
  (
    | { phase: 'starting' | 'pending' | 'disconnecting' }
    /** The new or reconnected account is in the reloaded list; its service opens. */
    | { phase: 'active' }
    | { phase: 'failed' | 'expired' | 'start_failed' | 'check_failed' | 'disconnect_failed' }
    | { phase: 'not_allowed'; reason: ApiDenial }
  );

export type IntegrationFlowActions = {
  connect: (toolkit: string) => void;
  reconnect: (toolkit: string, account: string) => void;
  /** Stops following the browser step; the link itself stays valid until it expires. */
  cancel: () => void;
  disconnect: (toolkit: string, account: string) => void;
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Starts a connect link, opens it in the browser and polls until the backend reports an end
 * state or the link expires. `reload` re-reads the integrations status after every end.
 */
export function useIntegrationConnectFlow(
  ownerId: string | null,
  reload: () => Promise<void>
): { flow: IntegrationFlow | null; actions: IntegrationFlowActions } {
  const [flow, setFlow] = useState<IntegrationFlow | null>(null);
  // Each flow has a number; a newer one, Cancel, an owner change or unmount retires the older.
  const current = useRef(0);

  useEffect(
    () => () => {
      current.current += 1;
      setFlow(null);
    },
    [ownerId]
  );

  const begin = (): (() => boolean) => {
    const id = ++current.current;
    return () => current.current === id;
  };

  const follow = async (
    live: () => boolean,
    target: Target,
    connectionId: string,
    expiresAt: number
  ): Promise<void> => {
    const api = window.electron?.integrations;
    if (!api) return;
    for (;;) {
      await sleep(POLL_INTERVAL_MS);
      if (!live()) return;
      let phase: IntegrationConnectionState | 'check_failed';
      if (Date.now() >= expiresAt) {
        phase = 'expired';
      } else {
        try {
          phase = await api.getConnectionState({ connection_id: connectionId });
        } catch {
          phase = 'check_failed';
        }
        if (!live()) return;
        if (phase === 'pending') continue;
      }
      await reload();
      if (live()) setFlow({ ...target, phase });
      return;
    }
  };

  const start = (target: Target): void => {
    const api = window.electron?.integrations;
    if (!api) return;
    const live = begin();
    setFlow({ ...target, phase: 'starting' });
    const request: Promise<IntegrationConnectResult> =
      target.account === null
        ? api.connect({ toolkit: target.toolkit })
        : api.reconnect({ toolkit: target.toolkit, account: target.account });
    request.then(
      (result) => {
        if (!live()) return;
        if (result.kind === 'not_allowed') {
          setFlow({ ...target, phase: 'not_allowed', reason: result.reason });
          return;
        }
        // Main opens new windows in the default browser.
        window.open(result.redirect_url, '_blank');
        setFlow({ ...target, phase: 'pending' });
        void follow(live, target, result.connection_id, Date.parse(result.expires_at));
      },
      () => {
        if (live()) setFlow({ ...target, phase: 'start_failed' });
      }
    );
  };

  const disconnect = (toolkit: string, account: string): void => {
    const api = window.electron?.integrations;
    if (!api) return;
    const live = begin();
    const target = { toolkit, account };
    setFlow({ ...target, phase: 'disconnecting' });
    // A lost answer does not prove a failed removal, so the list is read back either way.
    void api.disconnect(target).then(
      async () => {
        await reload();
        if (live()) setFlow(null);
      },
      async () => {
        await reload();
        if (live()) setFlow({ ...target, phase: 'disconnect_failed' });
      }
    );
  };

  return {
    flow,
    actions: {
      connect: (toolkit) => start({ toolkit, account: null }),
      reconnect: (toolkit, account) => start({ toolkit, account }),
      cancel: () => {
        begin();
        setFlow(null);
      },
      disconnect,
    },
  };
}
