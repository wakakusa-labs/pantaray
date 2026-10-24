import { useCallback, useEffect, useState } from 'react';

import { useLocalOwner } from '@/context/localOwnerContext';
import type { CaptureGateState } from '../../electron/src/ipc/context';

/** `null` while the gate has not been read yet. */
export type CaptureGate = CaptureGateState | null;

/** Nothing gated: outside the desktop app there is no recorder to wait on. */
const OPEN_GATE: CaptureGateState = {
  permissions: { accessibility: true, screenRecording: true },
  introRequested: false,
};

/**
 * Whether macOS lets this app record at all.
 *
 * Everything the assistant reads comes from the recorder, and the recorder cannot
 * run without its macOS permissions — so until they are granted no conversation
 * window opens, and this is what the recording screen asks for. The permissions are
 * granted outside the app, in System Settings, so the state is re-read whenever this
 * window is focused or becomes visible. A window that was already in front is given
 * no focus event to react to, so main also asks for the re-read directly.
 */
export function useCaptureGate(): {
  gate: CaptureGate;
  dismissIntro: () => Promise<void>;
} {
  const owner = useLocalOwner();
  const [gate, setGate] = useState<CaptureGate>(null);

  useEffect(() => {
    const read = async () => {
      const getGateState = window.electron?.screenshot?.getGateState ?? (async () => OPEN_GATE);
      try {
        setGate(await getGateState());
      } catch (error) {
        console.error('Failed to read the capture gate state:', error);
        // Main enforces the gate on every window it opens, so a transient read
        // failure here must not put an unanswerable screen in front of the user.
        setGate(OPEN_GATE);
      }
    };
    void read();

    const refresh = () => {
      if (document.visibilityState === 'visible') void read();
    };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    const unsubscribe = window.electron?.screenshot?.onGateStateChanged?.(() => void read());
    return () => {
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
      unsubscribe?.();
    };
  }, []);

  const dismissIntro = useCallback(async () => {
    // Answering the screen drops the conversation main deferred for it; this only
    // avoids waiting a round trip to close the screen.
    setGate((current) => (current ? { ...current, introRequested: false } : current));
    try {
      // The answer names the owner this screen was shown to: a start can wait on macOS
      // permission long enough for the owner to change, and main takes the answer only
      // from the owner it still has.
      await window.electron?.screenshot?.dismissIntro?.(owner.id);
    } catch (error) {
      console.error('Failed to drop the conversation waiting on capture permissions:', error);
    }
  }, [owner.id]);

  return { gate, dismissIntro };
}
