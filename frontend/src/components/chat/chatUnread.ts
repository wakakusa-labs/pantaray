import { createContext, useContext } from 'react';

import type { LocalOwner } from '../../../electron/src/auth/localRuntimeState';

const STORAGE_PREFIX = 'pantaray.chat-read:';

const listeners = new Set<() => void>();

const storageKey = (owner: LocalOwner) => `${STORAGE_PREFIX}${owner.kind}:${owner.id}`;

/**
 * The sequence of the newest chat item this viewer has seen, or null before the first read.
 * A per-viewer position, so it lives in this window's storage like the History view mode.
 */
export function readChatReadSequence(owner: LocalOwner): number | null {
  try {
    const raw = localStorage.getItem(storageKey(owner));
    const sequence = raw === null ? NaN : Number(raw);
    return Number.isSafeInteger(sequence) && sequence > 0 ? sequence : null;
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('Chat read position could not be read.');
    return null;
  }
}

/** Moves the read position forward to `sequence`; an older position never replaces a newer one. */
export function saveChatReadSequence(owner: LocalOwner, sequence: number): void {
  const current = readChatReadSequence(owner);
  if (current !== null && current >= sequence) return;
  try {
    localStorage.setItem(storageKey(owner), String(sequence));
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('Chat read position could not be saved.');
    return;
  }
  for (const listener of listeners) listener();
}

export function subscribeChatReadSequence(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Pantaray's chat messages the viewer has not seen yet; 0 without an owner. */
export const ChatUnreadContext = createContext(0);

export function useChatUnreadCount(): number {
  return useContext(ChatUnreadContext);
}
