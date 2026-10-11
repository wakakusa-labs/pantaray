import { useEffect } from 'react';
import type { RefObject } from 'react';

/**
 * A standalone Overlay is opened to write in, so its composer takes focus on mount and again
 * whenever main asks (`overlay:focusComposer`, sent when the existing window is reused).
 */
export function useStandaloneComposerFocus(
  enabled: boolean,
  composerRef: RefObject<HTMLTextAreaElement>
) {
  useEffect(() => {
    if (!enabled) return;
    composerRef.current?.focus();
    return window.electron?.ipcRenderer.on('overlay:focusComposer', () => {
      composerRef.current?.focus();
    });
  }, [enabled, composerRef]);
}
