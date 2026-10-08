import { useLayoutEffect, useRef } from 'react';

import { cardElementId, type WorkKey } from './chatTimeline';

/** A request from the Overlay to show this Action's latest card; `key` tells requests apart. */
export type ChatReveal = Readonly<{ actionId: string; key: string }>;

/**
 * Brings an Action's latest card into view and focus, reading older pages until the card is
 * found or the chat has none older. A chat with no card for the Action stays where it is.
 */
export function useChatReveal({
  reveal,
  ready,
  latestCards,
  hasOlder,
  failed,
  loadingOlder,
  loadOlder,
  holdPlace,
}: {
  reveal: ChatReveal | null;
  ready: boolean;
  latestCards: ReadonlyMap<WorkKey, string>;
  hasOlder: boolean;
  failed: boolean;
  loadingOlder: boolean;
  loadOlder: () => Promise<void>;
  /** Stops the chat from following the newest message once the card is shown. */
  holdPlace: () => void;
}): void {
  const doneRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (!reveal || !ready || doneRef.current === reveal.key) return;
    const position = latestCards.get(`action:${reveal.actionId}`);
    if (position !== undefined) {
      const card = document.getElementById(cardElementId(position));
      holdPlace();
      card?.scrollIntoView({ block: 'center' });
      card?.focus({ preventScroll: true });
      doneRef.current = reveal.key;
    } else if (hasOlder && !failed) {
      if (!loadingOlder) void loadOlder();
    } else {
      doneRef.current = reveal.key;
    }
  }, [reveal, ready, latestCards, hasOlder, failed, loadingOlder, loadOlder, holdPlace]);
}
