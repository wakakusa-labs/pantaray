import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import type { WorkKey } from '@/components/chat/chatTimeline';

/** What History's detail pane shows: the chat, or one Action or suggestion. */
export type HistorySelection = 'chat' | WorkKey;

const SELECTION_PARAM = 'item';
const WORK_KEY = /^(action|suggestion):(.+)$/;

/** The selection a `?item=` value names. A missing or malformed value shows the chat. */
export function parseHistorySelection(search: string): HistorySelection {
  const value = new URLSearchParams(search).get(SELECTION_PARAM);
  const match = value === null ? null : WORK_KEY.exec(value);
  // Ids are canonical: never blank, never padded.
  if (!match || match[2] !== match[2].trim()) return 'chat';
  return `${match[1] as 'action' | 'suggestion'}:${match[2]}`;
}

export function historySelectionSearch(selection: HistorySelection): string {
  // A query may hold `:` as is, so the kind's separator stays readable; the id is encoded.
  return `?${SELECTION_PARAM}=${encodeURIComponent(selection).replace('%3A', ':')}`;
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
