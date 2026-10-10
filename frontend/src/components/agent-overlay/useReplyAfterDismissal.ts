import { useEffect, useRef } from 'react';

/**
 * 見送る with words in the composer: the dismissal goes first, and the words follow as a reply
 * once the dismissal is recorded, since the backend takes a reply only to a dismissed offer.
 *
 * `reply` is the composer's own reply-to-the-suggestion send, so a reply that fails leaves the
 * words in the composer with that send's error and its send button.
 */
export function useReplyAfterDismissal(
  suggestionId: string | null,
  canReplyToDismissal: boolean,
  reply: () => void
) {
  const pendingRef = useRef<string | null>(null);
  const replyRef = useRef(reply);
  useEffect(() => {
    replyRef.current = reply;
  });
  useEffect(() => {
    if (!canReplyToDismissal || pendingRef.current === null) return;
    if (pendingRef.current !== suggestionId) return;
    pendingRef.current = null;
    replyRef.current();
  }, [canReplyToDismissal, suggestionId]);
  /** Called as 見送る is pressed: whether a reply is to follow the dismissal. */
  return (withReply: boolean) => {
    pendingRef.current = withReply ? suggestionId : null;
  };
}
