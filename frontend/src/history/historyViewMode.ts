/** How the History page shows the user's work: the single chat (default) or today's list. */
export type HistoryViewMode = 'chat' | 'list';

const STORAGE_KEY = 'pantaray.history-view-mode';

export const historyModeButtonId = (mode: HistoryViewMode) => `history-mode:${mode}`;

// A per-viewer layout choice, so it lives in this window's storage like the read positions.
export function readHistoryViewMode(): HistoryViewMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'list' ? 'list' : 'chat';
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('History view mode could not be read.');
    return 'chat';
  }
}

export function saveHistoryViewMode(mode: HistoryViewMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('History view mode could not be saved.');
  }
}

/** The navigation state `Layout` sends with an Overlay's request to show an Action in the chat. */
export type ShowChatState = { showChat: { actionId: string } };

export function showChatState(actionId: string): ShowChatState {
  return { showChat: { actionId } };
}

/** The request this navigation carries, keyed by the navigation so a repeat is a new request. */
export function readShowChatState(
  state: unknown,
  navigationKey: string
): { actionId: string; key: string } | null {
  if (typeof state !== 'object' || state === null || !('showChat' in state)) return null;
  const { showChat } = state as ShowChatState;
  return { actionId: showChat.actionId, key: navigationKey };
}
