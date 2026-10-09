import { useLayoutEffect, useRef } from 'react';

/** A request from the Overlay to show this Action's latest card; `key` tells requests apart. */
export type ChatReveal = Readonly<{ actionId: string; key: string }>;

/**
 * A place in the chat to bring into view: `key` tells requests apart, and `elementId` is null
 * while the item it belongs to is on a page not read yet.
 */
export type ChatRevealTarget = Readonly<{ key: string; elementId: string | null }>;

/**
 * Brings a card or a message into view and focus, reading older pages until its item is found
 * or the chat has none older. A target that is never found leaves the chat where it is.
 */
export function useChatReveal({
  target,
  ready,
  hasOlder,
  failed,
  loadingOlder,
  loadOlder,
  readPlace,
  onShown,
}: {
  target: ChatRevealTarget | null;
  ready: boolean;
  hasOlder: boolean;
  failed: boolean;
  loadingOlder: boolean;
  loadOlder: () => Promise<void>;
  /** Tells the chat it was scrolled, so it follows the newest message only from there. */
  readPlace: () => void;
  /** The target is in view and has focus. */
  onShown?: () => void;
}): void {
  const key = target?.key ?? null;
  const elementId = target?.elementId ?? null;
  const doneRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (key === null || !ready || doneRef.current === key) return;
    if (elementId !== null) {
      doneRef.current = key;
      // An item the chat does not draw, such as a bridge event, has no element.
      const element = document.getElementById(elementId);
      if (!element) return;
      element.scrollIntoView({ block: 'center' });
      element.focus({ preventScroll: true });
      readPlace();
      onShown?.();
    } else if (hasOlder && !failed) {
      if (!loadingOlder) void loadOlder();
    } else {
      doneRef.current = key;
    }
  }, [key, elementId, ready, hasOlder, failed, loadingOlder, loadOlder, readPlace, onShown]);
}
