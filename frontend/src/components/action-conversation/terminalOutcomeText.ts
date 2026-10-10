import type { MessageKey } from '@/i18n/types';

/**
 * The backend's fixed sentences for a run's end the user cannot act on: the canceled, failed and
 * unrecoverable ones in `action_status.py`, and the relay's for a broken stream. They are stored
 * in English whatever the language, and a failed run can carry any failure code, so the sentence
 * names them rather than the code. A message the user can act on arrives already written in the
 * user's language and is shown as sent.
 */
const UNACTIONABLE_OUTCOMES: ReadonlyMap<string, MessageKey> = new Map([
  ['Action execution was canceled.', 'overlay.executionCanceled'],
  ['Action execution failed.', 'overlay.executionStoppedWithError'],
  ['Action execution became unrecoverable before completion.', 'overlay.executionStoppedWithError'],
  ['Action stream payload is invalid.', 'overlay.executionStoppedWithError'],
]);

/** What the conversation says for a canceled or failed run's public message. */
export function terminalOutcomeText(message: string, t: (key: MessageKey) => string): string {
  const key = UNACTIONABLE_OUTCOMES.get(message);
  return key === undefined ? message : t(key);
}
