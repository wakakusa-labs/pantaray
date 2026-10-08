import type { ChatCard, ChatItem } from '../../../electron/src/chat/chatContracts';

export type ChatMessageItem = ChatItem & {
  content: Extract<ChatItem['content'], { kind: 'user_message' | 'assistant_message' }>;
};

export function isChatMessage(item: ChatItem): item is ChatMessageItem {
  return item.content.kind === 'user_message' || item.content.kind === 'assistant_message';
}

/** Who wrote a message, as the catalog names them. */
export function speakerKey(item: ChatMessageItem): 'history.chat.you' | 'history.chat.pantaray' {
  return item.content.kind === 'user_message' ? 'history.chat.you' : 'history.chat.pantaray';
}

/** Where a card is in the page, so a request to show a work can scroll to it. */
export function cardElementId(position: string): string {
  return `chat-card:${position}`;
}

/** One Action or Suggestion, named the way the history list names its rows. */
export type WorkKey = `action:${string}` | `suggestion:${string}`;

export function cardWorkKey(card: ChatCard): WorkKey {
  return card.kind === 'action' ? `action:${card.action_id}` : `suggestion:${card.suggestion_id}`;
}

/** A card's place in the chat: its item and its position among that item's cards. */
export function cardPosition(itemId: string, index: number): string {
  return `${itemId}#${index}`;
}

/**
 * The chat in reading order, oldest first. Pages, live appends and reloads overlap, so an item
 * is kept once by `item_id`; `sequence` is the backend's order of the append-only chat.
 */
export function mergeChatItems(
  current: readonly ChatItem[],
  incoming: readonly ChatItem[]
): ChatItem[] {
  const byId = new Map(current.map((item) => [item.item_id, item]));
  for (const item of incoming) byId.set(item.item_id, item);
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
}

/**
 * Keeps a fresh newest page and only the items appended after it: older pages read before a
 * reconnect may leave a gap below the new page, so they are read again by scrolling up.
 */
export function replaceWithNewestPage(
  current: readonly ChatItem[],
  page: readonly ChatItem[]
): ChatItem[] {
  const newestInPage = page.reduce((max, item) => Math.max(max, item.sequence), 0);
  return mergeChatItems(
    page,
    current.filter((item) => item.sequence > newestInPage)
  );
}

/**
 * The position of each work's latest card. Only that card shows the work's current status;
 * an older card keeps the summary it was written with.
 */
export function latestCardPositions(items: readonly ChatItem[]): ReadonlyMap<WorkKey, string> {
  const latest = new Map<WorkKey, string>();
  for (const item of items) {
    if (item.content.kind !== 'assistant_message') continue;
    item.content.cards.forEach((card, index) => {
      latest.set(cardWorkKey(card), cardPosition(item.item_id, index));
    });
  }
  return latest;
}

type TurnFailureReason = Extract<ChatItem['content'], { kind: 'turn_failure' }>['reason'];

/**
 * The failed turn a retry can still run again: the newest turn_failure with no message after it,
 * and the user's last message, which the retry is offered under. Bridge events do not count.
 */
export function retryableFailure(
  items: readonly ChatItem[]
): { failureItemId: string; reason: TurnFailureReason; messageItemId: string } | null {
  const newestFirst = [...items].reverse();
  const last = newestFirst.find(
    (item) => item.content.kind !== 'suggestion_event' && item.content.kind !== 'action_event'
  );
  if (last?.content.kind !== 'turn_failure') return null;
  const message = newestFirst.find((item) => item.content.kind === 'user_message');
  if (!message) return null;
  return {
    failureItemId: last.item_id,
    reason: last.content.reason,
    messageItemId: message.item_id,
  };
}
