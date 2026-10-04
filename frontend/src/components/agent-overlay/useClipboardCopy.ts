import { useCallback, useEffect, useRef, useState } from 'react';

const COPY_STATUS_RESET_DELAY_MS = 1500;

export type ClipboardCopyStatus = 'idle' | 'copied' | 'failed';

/**
 * Copies text produced on demand and reports the outcome for a short time, so a
 * copy button can switch its icon. `produce` returns null when the text could not be
 * assembled; nothing reaches the clipboard then.
 */
export function useClipboardCopy() {
  const [status, setStatus] = useState<ClipboardCopyStatus>('idle');
  const resetTimerRef = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (resetTimerRef.current !== null) window.clearTimeout(resetTimerRef.current);
    },
    []
  );
  const copy = useCallback(async (produce: () => Promise<string | null> | string) => {
    let next: ClipboardCopyStatus = 'failed';
    try {
      const text = await produce();
      if (text !== null) {
        await navigator.clipboard.writeText(text);
        next = 'copied';
      }
    } catch {
      // A rejected read or clipboard write is reported through the failed status.
    }
    setStatus(next);
    if (resetTimerRef.current !== null) window.clearTimeout(resetTimerRef.current);
    resetTimerRef.current = window.setTimeout(() => {
      setStatus('idle');
      resetTimerRef.current = null;
    }, COPY_STATUS_RESET_DELAY_MS);
  }, []);
  return { status, copy };
}
