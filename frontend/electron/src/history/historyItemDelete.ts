import { LocalBackendRequestError } from '../localBackend/client';
import type { HistoryItemKey } from './historyContracts';

const HISTORY_DELETE_TIMEOUT_MS = 30_000;

/**
 * The error code crosses IPC as data because an Error thrown across `invoke` loses it, and the
 * renderer tells `CONVERSATION_BUSY` apart from every other failure.
 */
export type HistoryItemDeleteResult = { ok: true } | { ok: false; errorCode: string | null };

type HistoryDeleteRequestJson = <T>(request: {
  path: string;
  method: 'DELETE';
  timeoutMs: number;
}) => Promise<T>;

export function createHistoryItemDeleter(params: {
  requestJson: HistoryDeleteRequestJson;
  /** Closes the window still showing the conversation that is now gone. */
  closeConversationWindow: (request: HistoryItemKey) => void;
}): (request: HistoryItemKey) => Promise<HistoryItemDeleteResult> {
  return async (request) => {
    try {
      await params.requestJson<void>({
        path: `/api/agent/history/items/${request.kind}/${encodeURIComponent(request.id)}`,
        method: 'DELETE',
        timeoutMs: HISTORY_DELETE_TIMEOUT_MS,
      });
    } catch (error) {
      if (error instanceof LocalBackendRequestError) {
        return { ok: false, errorCode: error.errorCode };
      }
      throw error;
    }
    params.closeConversationWindow(request);
    return { ok: true };
  };
}
