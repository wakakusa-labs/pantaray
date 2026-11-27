/** How many schedules go with a conversation, or `unknown` when the list could not be read. */
export type ConversationScheduleCount = number | 'unknown';

/**
 * Counts the schedules asked for in the conversation and those sending to it: the ones deleting
 * the conversation, or its TODO, deletes with it.
 */
export async function countConversationSchedules(
  actionId: string
): Promise<ConversationScheduleCount> {
  try {
    const hooks = window.electron?.hooks;
    if (!hooks) throw new Error('Schedules bridge is unavailable.');
    const list = await hooks.list();
    return list.hooks.filter(
      (hook) => hook.created_from_action_id === actionId || hook.target_action_id === actionId
    ).length;
  } catch {
    return 'unknown';
  }
}
