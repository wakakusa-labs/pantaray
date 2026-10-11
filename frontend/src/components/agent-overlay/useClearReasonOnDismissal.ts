import { useEffect, useRef } from 'react';

/**
 * 見送る with words: they go with the dismissal as its reason, and leave the composer once the
 * dismissal is recorded. Words changed since 見送る was pressed stay, and so do the words of a
 * dismissal that failed, for another try.
 *
 * `draft` is the composer's words as 見送る would send them.
 */
export function useClearReasonOnDismissal(
  suggestionId: string | null,
  dismissed: boolean,
  draft: string | null,
  clear: () => void
) {
  const sentRef = useRef<{ suggestionId: string; reason: string } | null>(null);
  const latestRef = useRef({ draft, clear });
  useEffect(() => {
    latestRef.current = { draft, clear };
  });
  useEffect(() => {
    const sent = sentRef.current;
    if (!dismissed || sent === null || sent.suggestionId !== suggestionId) return;
    sentRef.current = null;
    if (latestRef.current.draft === sent.reason) latestRef.current.clear();
  }, [dismissed, suggestionId]);
  /** Called as 見送る is pressed, with the reason it sends. */
  return (reason: string | null) => {
    sentRef.current = suggestionId !== null && reason !== null ? { suggestionId, reason } : null;
  };
}
