import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';

import type { ChatItem } from '../../../electron/src/chat/chatContracts';

/** Within this distance of the bottom, the reader is following the chat and new items scroll in. */
const FOLLOW_SLACK_PX = 48;
/** Older messages start loading this far before the top of the chat comes into view. */
const PRELOAD_MARGIN_PX = 240;

function isAtBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_SLACK_PX;
}

/**
 * Keeps the reader's place in the chat: it opens at the newest message, follows new ones while
 * the reader is at the bottom, and holds the visible messages still when an older page lands
 * above them.
 */
export function useChatScroll({
  items,
  typing,
  ready,
  hasOlder,
  failed,
  loadingOlder,
  loadOlder,
}: {
  items: readonly ChatItem[];
  /** The 「…」 bubble after the last item; it comes into view like a new item. */
  typing: boolean;
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
  // Where the last pin to the bottom left the scroll. Its scroll event arrives a frame later,
  // possibly after the messages grew, and must not read as the reader moving away; a reader's own
  // scroll moves away from this position. (The chat turns off scroll anchoring, so growth alone
  // never moves it.)
  const pinnedTopRef = useRef<number | null>(null);
  const pinToBottom = useCallback((element: HTMLElement) => {
    element.scrollTop = element.scrollHeight;
    pinnedTopRef.current = element.scrollTop;
  }, []);
  // The distance from the bottom when an older page was asked for, so it can be kept.
  const anchorRef = useRef<number | null>(null);
  const firstItemRef = useRef<string | null>(null);

  // The composer grows with a quote or attachments and shrinks the chat above it, and the messages
  // grow after they are drawn (a card's work state, an image); a reader at the bottom stays at the
  // bottom instead of losing the newest message under either.
  const attachScrollElement = useCallback(
    (element: HTMLDivElement | null) => {
      resizeObserverRef.current?.disconnect();
      resizeObserverRef.current = null;
      scrollRef.current = element;
      if (!element) return;
      const observer = new ResizeObserver(() => {
        if (followingRef.current) pinToBottom(element);
      });
      observer.observe(element);
      // The scroll element holds one column with all the messages.
      if (element.firstElementChild) observer.observe(element.firstElementChild);
      resizeObserverRef.current = observer;
    },
    [pinToBottom]
  );

  const onScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element || element.scrollTop === pinnedTopRef.current) return;
    followingRef.current = isAtBottom(element);
  }, []);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element || !ready) return;
    const firstItem = items[0]?.item_id ?? null;
    if (anchorRef.current !== null && firstItem !== firstItemRef.current) {
      element.scrollTop = element.scrollHeight - anchorRef.current;
      anchorRef.current = null;
    } else if (followingRef.current) {
      pinToBottom(element);
    }
    firstItemRef.current = firstItem;
  }, [items, typing, ready, pinToBottom]);

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

  /**
   * The reader was taken to a message: away from the newest, a resize or a new item must not pull
   * it back; at the newest, the chat follows again. Its scroll event cannot tell: one back to the
   * last pin's position is skipped, and a scroll that did not move fires none.
   */
  const readPlace = useCallback(() => {
    if (scrollRef.current) followingRef.current = isAtBottom(scrollRef.current);
  }, []);

  /** Back to the newest message, and the next items scroll into view, as after a send. */
  const followNewest = useCallback(() => {
    followingRef.current = true;
    if (scrollRef.current) pinToBottom(scrollRef.current);
  }, [pinToBottom]);

  /** True while the chat is drawn and the reader is at its newest message, so it is in view. */
  const isAtNewest = useCallback(() => scrollRef.current !== null && followingRef.current, []);

  return {
    scrollRef: attachScrollElement,
    isAtNewest,
    olderTriggerRef,
    onScroll,
    loadOlderKeepingPlace,
    followNewest,
    readPlace,
  };
}
