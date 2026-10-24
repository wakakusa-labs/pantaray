import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import type { WorkKey } from '@/components/chat/chatTimeline';

/** What History's detail pane shows: the chat, a new task's composer, or one Action or suggestion. */
export type HistorySelection = 'chat' | 'new' | WorkKey;

const NEW_TASK = 'new';

const SELECTION_PARAM = 'item';
/** A Suggestion file an Overlay chip asked the detail pane to preview; main writes it too. */
const FILE_PARAM = 'file';
const WORK_KEY = /^(action|suggestion):(.+)$/;

/** The work a value names, or null when it is not a well-formed key. */
export function parseWorkKey(value: unknown): WorkKey | null {
  const match = typeof value === 'string' ? WORK_KEY.exec(value) : null;
  // Ids are canonical: never blank, never padded.
  if (!match || match[2] !== match[2].trim()) return null;
  return `${match[1] as 'action' | 'suggestion'}:${match[2]}`;
}

/** The selection a `?item=` value names. A missing or malformed value shows the chat. */
export function parseHistorySelection(search: string): HistorySelection {
  const value = new URLSearchParams(search).get(SELECTION_PARAM);
  if (value === NEW_TASK) return NEW_TASK;
  return parseWorkKey(value) ?? 'chat';
}

/** The file a `&file=` value asks the selected work to preview, or null. */
export function parseRequestedFile(search: string): string | null {
  return new URLSearchParams(search).get(FILE_PARAM) || null;
}

export function historySelectionSearch(
  selection: HistorySelection,
  file: string | null = null
): string {
  if (selection === 'chat' || selection === NEW_TASK) return `?${SELECTION_PARAM}=${selection}`;
  // A query may hold `:` as is, so the kind's separator stays readable; the id is encoded.
  const { kind, id } = splitWorkKey(selection);
  const fileQuery = file === null ? '' : `&${FILE_PARAM}=${encodeURIComponent(file)}`;
  return `?${SELECTION_PARAM}=${kind}:${encodeURIComponent(id)}${fileQuery}`;
}

/** The kind and id a work's key names. */
export function splitWorkKey(key: WorkKey): { kind: 'action' | 'suggestion'; id: string } {
  const separator = key.indexOf(':');
  return {
    kind: key.slice(0, separator) as 'action' | 'suggestion',
    id: key.slice(separator + 1),
  };
}

/** The selection a history row stands for. */
export function historyItemSelection(item: ConversationHistoryListItem): WorkKey {
  return item.kind === 'conversation'
    ? `action:${item.action_id}`
    : `suggestion:${item.suggestion_id}`;
}
