import type { createLocalBackendClient } from '../localBackend/client';

import {
  ActionMessageRequestSchema,
  ActionWireContractError,
  type ActionConversationPage,
  type ActionMessageRequest,
  type ActionMessageResponse,
  type ActionResumeRequest,
  type ActionWithdrawMessageRequest,
  type ActionToolOutputDetail,
  parseActionConversationPage,
  parseActionMessageResponse,
  parseActionToolOutputDetail,
} from './actionContracts';

export const ACTION_CONVERSATION_REQUEST_TIMEOUT_MS = 30_000;

type RequestJson = ReturnType<typeof createLocalBackendClient>['requestJson'];

export type ActionConversationPageRequest = {
  actionId: string;
  cursor: string | null;
  limit: number;
};

export type ActionConversationPageReadResult = ActionConversationPage | { kind: 'stale_cursor' };

export type ActionToolOutputRequest = {
  actionId: string;
  stepId: string;
  cursor: string | null;
  limitBytes: number;
};

function requireAuthenticatedUserId(getUserId: () => string | null): string {
  const userId = getUserId();
  if (!userId) throw new Error('Missing authenticated user id.');
  return encodeURIComponent(userId);
}

export function createActionFetcher(params: {
  requestJson: RequestJson;
  getUserId: () => string | null;
}): {
  submitMessage: (request: ActionMessageRequest) => Promise<ActionMessageResponse>;
  resumeAction: (request: ActionResumeRequest) => Promise<ActionMessageResponse>;
  withdrawMessage: (request: ActionWithdrawMessageRequest) => Promise<void>;
  readConversationPage: (request: ActionConversationPageRequest) => Promise<ActionConversationPage>;
  readToolOutputPage: (request: ActionToolOutputRequest) => Promise<ActionToolOutputDetail>;
} {
  const userPath = (): string =>
    `/v1/agents/users/${requireAuthenticatedUserId(params.getUserId)}/actions`;

  return {
    submitMessage: async (request): Promise<ActionMessageResponse> => {
      const body = ActionMessageRequestSchema.parse(request);
      const response = parseActionMessageResponse(
        await params.requestJson<unknown>({
          path: `${userPath()}/messages`,
          method: 'POST',
          body,
          timeoutMs: ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
        })
      );
      if (
        response.message_id !== body.message.message_id ||
        (body.target.kind === 'existing' && response.action_id !== body.target.action_id)
      ) {
        throw new ActionWireContractError('Action message');
      }
      return response;
    },
    resumeAction: async (request): Promise<ActionMessageResponse> => {
      const actionId = encodeURIComponent(request.actionId);
      const response = parseActionMessageResponse(
        await params.requestJson<unknown>({
          path: `${userPath()}/${actionId}/resume`,
          method: 'POST',
          body: { message_id: request.messageId },
          timeoutMs: ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
        })
      );
      if (response.message_id !== request.messageId || response.action_id !== request.actionId) {
        throw new ActionWireContractError('Action resume');
      }
      return response;
    },
    withdrawMessage: async (request): Promise<void> => {
      const actionId = encodeURIComponent(request.actionId);
      const stepId = encodeURIComponent(request.stepId);
      await params.requestJson<void>({
        path: `${userPath()}/${actionId}/unadopted-messages/${stepId}`,
        method: 'DELETE',
        timeoutMs: ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
      });
    },
    readConversationPage: async (request): Promise<ActionConversationPage> => {
      const actionId = encodeURIComponent(request.actionId);
      const page = parseActionConversationPage(
        await params.requestJson<unknown>({
          path: `${userPath()}/${actionId}/state`,
          method: 'GET',
          query: { cursor: request.cursor, limit: request.limit },
          timeoutMs: ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
        })
      );
      if (page.action.action_id !== request.actionId) {
        throw new ActionWireContractError('Action conversation');
      }
      return page;
    },
    readToolOutputPage: async (request): Promise<ActionToolOutputDetail> => {
      const actionId = encodeURIComponent(request.actionId);
      const stepId = encodeURIComponent(request.stepId);
      return parseActionToolOutputDetail(
        await params.requestJson<unknown>({
          path: `${userPath()}/${actionId}/steps/${stepId}/output`,
          method: 'GET',
          query: { cursor: request.cursor, limit_bytes: request.limitBytes },
          timeoutMs: ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
        })
      );
    },
  };
}
