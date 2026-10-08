import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';

import type { ChatItem } from '../../../electron/src/chat/chatContracts';

/** Within this distance of the bottom, the reader is following the chat and new items scroll in. */
const FOLLOW_SLACK_PX = 48;
/** Older messages start loading this far before the top of the chat comes into view. */
const PRELOAD_MARGIN_PX = 240;

/**
 * Keeps the reader's place in the chat: it opens at the newest message, follows new ones while
 * the reader is at the bottom, and holds the visible messages still when an older page lands
 * above them.
 */
export function useChatScroll({
  items,
  ready,
  hasOlder,
  failed,
  loadingOlder,
  loadOlder,
}: {
  items: readonly ChatItem[];
  ready: boolean;
  hasOlder: boolean;
  /** After a failed read, older pages load only when the reader asks again. */
  failed: boolean;
  loadingOlder: boolean;
  loadOlder: () => Promise<void>;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const olderTriggerRef = useRef<HTMLButtonElement>(null);
  const followingRef = useRef(true);
  // The distance from the bottom when an older page was asked for, so it can be kept.
  const anchorRef = useRef<number | null>(null);
  const firstItemRef = useRef<string | null>(null);

  // The composer grows with a quote or attachments and shrinks the chat above it; a reader at the
  // bottom stays at the bottom instead of losing the newest message under it.
  const attachScrollElement = useCallback((element: HTMLDivElement | null) => {
    resizeObserverRef.current?.disconnect();
    resizeObserverRef.current = null;
    scrollRef.current = element;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (followingRef.current) element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    resizeObserverRef.current = observer;
  }, []);

  const onScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    followingRef.current =
      element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_SLACK_PX;
  }, []);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element || !ready) return;
    const firstItem = items[0]?.item_id ?? null;
    if (anchorRef.current !== null && firstItem !== firstItemRef.current) {
      element.scrollTop = element.scrollHeight - anchorRef.current;
      anchorRef.current = null;
    } else if (followingRef.current) {
      element.scrollTop = element.scrollHeight;
    }
    firstItemRef.current = firstItem;
  }, [items, ready]);

  // A page that brought nothing new leaves the anchor unused; a later reload must not apply it.
  useEffect(() => {
    if (!loadingOlder) anchorRef.current = null;
  }, [loadingOlder]);

  const loadOlderKeepingPlace = useCallback(() => {
    const element = scrollRef.current;
    if (element) anchorRef.current = element.scrollHeight - element.scrollTop;
    void loadOlder();
  }, [loadOlder]);

  useEffect(() => {
    const root = scrollRef.current;
    const trigger = olderTriggerRef.current;
    if (!ready || !hasOlder || failed || loadingOlder || !root || !trigger) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadOlderKeepingPlace();
      },
      { root, rootMargin: `${PRELOAD_MARGIN_PX}px 0px 0px 0px` }
    );
    observer.observe(trigger);
    return () => observer.disconnect();
  }, [ready, hasOlder, failed, loadingOlder, loadOlderKeepingPlace]);

  /** The reader was taken elsewhere in the chat; a resize or a new item must not pull it back. */
  const holdPlace = useCallback(() => {
    followingRef.current = false;
  }, []);

  /** The next items scroll into view, as after the reader sends a message. */
  const followNewest = useCallback(() => {
    followingRef.current = true;
  }, []);

  return {
    scrollRef: attachScrollElement,
    olderTriggerRef,
    onScroll,
    loadOlderKeepingPlace,
    followNewest,
    holdPlace,
  };
}
