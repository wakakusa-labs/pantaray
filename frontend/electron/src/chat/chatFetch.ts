import { LocalBackendRequestError, type createLocalBackendClient } from '../localBackend/client';

import {
  ChatMessageRejectedSchema,
  type ChatItemPage,
  type ChatItemPageRequest,
  type ChatMessageRequest,
  type ChatMessageSendResult,
  type ChatTurnRetryRequest,
  type ChatTurnRetryResult,
  parseChatItem,
  parseChatItemPage,
} from './chatContracts';

const CHAT_REQUEST_TIMEOUT_MS = 30_000;

type RequestJson = ReturnType<typeof createLocalBackendClient>['requestJson'];

export function createChatFetcher(params: {
  requestJson: RequestJson;
  getUserId: () => string | null;
}): {
  sendMessage: (request: ChatMessageRequest) => Promise<ChatMessageSendResult>;
  listItems: (request: ChatItemPageRequest) => Promise<ChatItemPage>;
  retryTurn: (request: ChatTurnRetryRequest) => Promise<ChatTurnRetryResult>;
} {
  const chatPath = (): string => {
    const userId = params.getUserId();
    if (!userId) throw new Error('Missing authenticated user id.');
    return `/v1/agents/users/${encodeURIComponent(userId)}/chat`;
  };

  return {
    sendMessage: async (request): Promise<ChatMessageSendResult> => {
      let payload: unknown;
      try {
        payload = await params.requestJson<unknown>({
          path: `${chatPath()}/messages`,
          method: 'POST',
          body: request,
          timeoutMs: CHAT_REQUEST_TIMEOUT_MS,
        });
      } catch (error) {
        // A quote, image or staged file the backend no longer has: the composer points at it.
        if (error instanceof LocalBackendRequestError && error.status === 400) {
          const rejected = ChatMessageRejectedSchema.safeParse(error.payload);
          if (rejected.success) return { kind: 'rejected', field: rejected.data.field };
        }
        throw error;
      }
      return { kind: 'sent', item: parseChatItem(payload) };
    },
    listItems: async (request): Promise<ChatItemPage> =>
      parseChatItemPage(
        await params.requestJson<unknown>({
          path: `${chatPath()}/items`,
          method: 'GET',
          query: { before: request.before, limit: request.limit },
          timeoutMs: CHAT_REQUEST_TIMEOUT_MS,
        })
      ),
    retryTurn: async (request): Promise<ChatTurnRetryResult> => {
      try {
        await params.requestJson<unknown>({
          path: `${chatPath()}/turns/retry`,
          method: 'POST',
          body: request,
          timeoutMs: CHAT_REQUEST_TIMEOUT_MS,
        });
      } catch (error) {
        if (error instanceof LocalBackendRequestError && error.status === 409) {
          return { kind: 'stale' };
        }
        throw error;
      }
      return { kind: 'started' };
    },
  };
}
