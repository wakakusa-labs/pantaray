import {
  LocalBackendRequestError,
  type LocalBackendRequest,
  type createLocalBackendClient,
} from '../localBackend/client';

import {
  TODO_REFUSAL_CODES,
  type DeletedTodoView,
  type GoalView,
  type ProgressRecordCreateRequest,
  type ProgressRecordRefRequest,
  type ProgressRecordView,
  type TodoCheckRequest,
  type TodoCheckResult,
  type TodoCreateRequest,
  type TodoDeletionAnswerRequest,
  type TodoDueRequest,
  type TodoGoalRequest,
  type TodoProgressPage,
  type TodoProgressQuery,
  type TodoProjectRefRequest,
  type TodoRefRequest,
  type TodoRefusalCode,
  type TodoScopeQuery,
  type TodoView,
  type TodoWeekPage,
  type TodoWeekQuery,
  type TodoWriteResult,
  parseDeletedTodos,
  parseGoalView,
  parseProgressRecordView,
  parseTodoCheckResult,
  parseTodoProgressPage,
  parseTodoView,
  parseTodoWeekPage,
} from './todoContracts';

/** The chat's wait (`CHAT_REQUEST_TIMEOUT_MS`): each route is one store transaction. */
const TODO_REQUEST_TIMEOUT_MS = 30_000;

type RequestJson = ReturnType<typeof createLocalBackendClient>['requestJson'];

function refusalCode(error: unknown): TodoRefusalCode | null {
  if (!(error instanceof LocalBackendRequestError)) return null;
  return TODO_REFUSAL_CODES.find((code) => code === error.errorCode) ?? null;
}

export function createTodoFetcher(params: {
  requestJson: RequestJson;
  getUserId: () => string | null;
}): {
  listWeek: (query: TodoWeekQuery) => Promise<TodoWeekPage>;
  create: (request: TodoCreateRequest) => Promise<TodoWriteResult<TodoView>>;
  setCheck: (request: TodoCheckRequest) => Promise<TodoWriteResult<TodoCheckResult>>;
  setDue: (request: TodoDueRequest) => Promise<TodoWriteResult<TodoView>>;
  delete: (request: TodoRefRequest) => Promise<TodoWriteResult<TodoView>>;
  answerDeletion: (request: TodoDeletionAnswerRequest) => Promise<TodoWriteResult<TodoView>>;
  restore: (request: TodoRefRequest) => Promise<TodoWriteResult<TodoView>>;
  listDeleted: (query: TodoScopeQuery) => Promise<DeletedTodoView[]>;
  progress: (query: TodoProgressQuery) => Promise<TodoProgressPage>;
  setGoal: (request: TodoGoalRequest) => Promise<TodoWriteResult<GoalView>>;
  clearGoal: (request: TodoProjectRefRequest) => Promise<TodoWriteResult<null>>;
  addProgressRecord: (
    request: ProgressRecordCreateRequest
  ) => Promise<TodoWriteResult<ProgressRecordView>>;
  deleteProgressRecord: (request: ProgressRecordRefRequest) => Promise<void>;
} {
  const todosPath = (): string => {
    const userId = params.getUserId();
    if (!userId) throw new Error('Missing authenticated user id.');
    return `/v1/agents/users/${encodeURIComponent(userId)}/todos`;
  };
  const todoPath = (todoId: string): string => `${todosPath()}/${encodeURIComponent(todoId)}`;
  const goalPath = (projectId: string): string =>
    `${todosPath()}/projects/${encodeURIComponent(projectId)}/goal`;

  const read = (request: Omit<LocalBackendRequest, 'timeoutMs'>): Promise<unknown> =>
    params.requestJson<unknown>({ ...request, timeoutMs: TODO_REQUEST_TIMEOUT_MS });

  const write = async <T>(
    request: Omit<LocalBackendRequest, 'timeoutMs'>,
    parse: (payload: unknown) => T
  ): Promise<TodoWriteResult<T>> => {
    let payload: unknown;
    try {
      payload = await read(request);
    } catch (error) {
      const code = refusalCode(error);
      if (code) return { kind: 'refused', code };
      throw error;
    }
    return { kind: 'ok', result: parse(payload) };
  };

  return {
    listWeek: async (query) =>
      parseTodoWeekPage(await read({ path: `${todosPath()}/weeks`, method: 'GET', query })),
    create: (request) => write({ path: todosPath(), method: 'POST', body: request }, parseTodoView),
    setCheck: ({ todo_id, checked }) =>
      write(
        { path: `${todoPath(todo_id)}/check`, method: 'PUT', body: { checked } },
        parseTodoCheckResult
      ),
    setDue: ({ todo_id, due }) =>
      write({ path: `${todoPath(todo_id)}/due`, method: 'PUT', body: { due } }, parseTodoView),
    delete: ({ todo_id }) => write({ path: todoPath(todo_id), method: 'DELETE' }, parseTodoView),
    answerDeletion: ({ todo_id, request_id, answer }) =>
      write(
        {
          path: `${todoPath(todo_id)}/deletion-requests/${encodeURIComponent(request_id)}/answer`,
          method: 'POST',
          body: { answer },
        },
        parseTodoView
      ),
    restore: ({ todo_id }) =>
      write({ path: `${todoPath(todo_id)}/restore`, method: 'POST' }, parseTodoView),
    listDeleted: async (query) =>
      parseDeletedTodos(await read({ path: `${todosPath()}/deleted`, method: 'GET', query })),
    progress: async (query) =>
      parseTodoProgressPage(await read({ path: `${todosPath()}/progress`, method: 'GET', query })),
    setGoal: ({ project_id, ...goal }) =>
      write({ path: goalPath(project_id), method: 'PUT', body: goal }, parseGoalView),
    // 204 also when the project has no goal.
    clearGoal: ({ project_id }) =>
      write({ path: goalPath(project_id), method: 'DELETE' }, () => null),
    addProgressRecord: ({ todo_id, ...record }) =>
      write(
        { path: `${todoPath(todo_id)}/progress-records`, method: 'POST', body: record },
        parseProgressRecordView
      ),
    // The backend never refuses it: 204 also when the record is gone.
    deleteProgressRecord: async ({ record_id }) => {
      await read({
        path: `${todosPath()}/progress-records/${encodeURIComponent(record_id)}`,
        method: 'DELETE',
      });
    },
  };
}
