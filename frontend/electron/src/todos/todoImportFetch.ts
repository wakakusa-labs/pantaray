import { LocalBackendRequestError, type createLocalBackendClient } from '../localBackend/client';

import {
  TodoImportRefusedSchema,
  type TodoImportRefRequest,
  type TodoImportRegisterRequest,
  type TodoImportResult,
  type TodoImportStartRequest,
  type TodoImportWriteRequest,
  parseTodoImportView,
} from './todoImportContracts';

/** The TODO routes' wait (`TODO_REQUEST_TIMEOUT_MS`): each route is one or two store transactions. */
const TODO_IMPORT_REQUEST_TIMEOUT_MS = 30_000;

type RequestJson = ReturnType<typeof createLocalBackendClient>['requestJson'];
type Method = 'GET' | 'POST' | 'PUT';

export function createTodoImportFetcher(params: {
  requestJson: RequestJson;
  getUserId: () => string | null;
}): {
  start: (request: TodoImportStartRequest) => Promise<TodoImportResult>;
  read: (request: TodoImportRefRequest) => Promise<TodoImportResult>;
  write: (request: TodoImportWriteRequest) => Promise<TodoImportResult>;
  register: (request: TodoImportRegisterRequest) => Promise<TodoImportResult>;
  cancel: (request: TodoImportRefRequest) => Promise<TodoImportResult>;
} {
  const importsPath = (): string => {
    const userId = params.getUserId();
    if (!userId) throw new Error('Missing authenticated user id.');
    return `/v1/agents/users/${encodeURIComponent(userId)}/todo-imports`;
  };
  const importPath = (importId: string): string =>
    `${importsPath()}/${encodeURIComponent(importId)}`;

  const send = async (path: string, method: Method, body?: unknown): Promise<TodoImportResult> => {
    let payload: unknown;
    try {
      payload = await params.requestJson<unknown>({
        path,
        method,
        body,
        timeoutMs: TODO_IMPORT_REQUEST_TIMEOUT_MS,
      });
    } catch (error) {
      if (error instanceof LocalBackendRequestError) {
        const refused = TodoImportRefusedSchema.safeParse(error.payload);
        if (refused.success) {
          const { type: _type, ...refusal } = refused.data;
          return { kind: 'refused', refusal };
        }
      }
      throw error;
    }
    return { kind: 'ok', result: parseTodoImportView(payload) };
  };

  return {
    start: (request) => send(importsPath(), 'POST', request),
    read: ({ import_id }) => send(importPath(import_id), 'GET'),
    write: ({ import_id, ...body }) => send(importPath(import_id), 'PUT', body),
    register: ({ import_id, expected_revision }) =>
      send(`${importPath(import_id)}/register`, 'POST', { expected_revision }),
    cancel: ({ import_id }) => send(`${importPath(import_id)}/cancel`, 'POST'),
  };
}
