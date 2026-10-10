/** Navigation state asking History's chat to show an Action, from the Overlay or a task pane. */
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
