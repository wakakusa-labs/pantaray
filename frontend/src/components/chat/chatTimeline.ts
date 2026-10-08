import type { ChatCard, ChatItem } from '../../../electron/src/chat/chatContracts';

export type ChatMessageItem = ChatItem & {
  content: Extract<ChatItem['content'], { kind: 'user_message' | 'assistant_message' }>;
};

export function isChatMessage(item: ChatItem): item is ChatMessageItem {
  return item.content.kind === 'user_message' || item.content.kind === 'assistant_message';
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

/** Bridge and turn events feed the chat's model; the user sees the message that follows them. */
export function isShownInChat(item: ChatItem): boolean {
  return item.content.kind !== 'suggestion_event' && item.content.kind !== 'action_event';
}
