export const NEW_WORK_BUTTON_ID = 'history-new-conversation';

/** Opens an empty Overlay for a new work, the same as the global shortcut. */
export async function openNewWork(): Promise<void> {
  const open = window.electron?.history?.openNewConversation;
  if (!open) throw new Error('New conversation bridge is unavailable.');
  await open();
}
