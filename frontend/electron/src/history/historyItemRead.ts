import { LocalBackendRequestError } from '../localBackend/client';
import {
  ConversationHistoryListItemSchema,
  type ConversationHistoryListItem,
  type HistoryItemKey,
} from './historyContracts';

const HISTORY_ITEM_READ_TIMEOUT_MS = 30_000;
const NOT_FOUND = 404;

type HistoryItemRequestJson = <T>(request: {
  path: string;
  method: 'GET';
  timeoutMs: number;
}) => Promise<T>;

/**
 * One History row by its key, titled as the list titles it. Null when History holds none for
 * this user: gone, another user's, or not there at all. A suggestion that became a task answers
 * with that task's row. Any other failure rejects.
 */
export function createHistoryItemReader(params: {
  requestJson: HistoryItemRequestJson;
}): (key: HistoryItemKey) => Promise<ConversationHistoryListItem | null> {
  return async (key) => {
    let payload: unknown;
    try {
      payload = await params.requestJson<unknown>({
        path: `/api/agent/history/items/${key.kind}/${encodeURIComponent(key.id)}`,
        method: 'GET',
        timeoutMs: HISTORY_ITEM_READ_TIMEOUT_MS,
      });
    } catch (error) {
      if (error instanceof LocalBackendRequestError && error.status === NOT_FOUND) return null;
      throw error;
    }
    return ConversationHistoryListItemSchema.parse(payload);
  };
}
