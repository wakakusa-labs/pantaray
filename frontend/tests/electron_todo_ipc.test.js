const assert = require('assert');
const { test } = require('node:test');

const { createTodoFetcher } = require('../electron/dist/todos/todoFetch.js');
const { registerTodoHandlers } = require('../electron/dist/ipc/handlers/todos.js');
const { createLocalBackendClient } = require('../electron/dist/localBackend/client.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');

const BASE = 'http://127.0.0.1:61131/v1/agents/users/user%201/todos';

const OPEN_TODO = {
  todo_id: 'todo-1',
  project_id: null,
  text: 'Send the estimate',
  author: 'user',
  origin: 'written',
  week_start: '2026-11-02',
  carried: false,
  due: { date: '2026-11-06', repeat: 'weekly' },
  state: { kind: 'not_started', reason: 'starting' },
  deletion_request: null,
  conversation: { kind: 'none' },
};

// One TODO per display state and conversation kind the backend sends.
const STATE_VARIANTS = [
  { state: { kind: 'done', by: 'ai', at: '2026-11-03T01:02:03.000000Z' } },
  { state: { kind: 'working', note: null }, conversation: { kind: 'action', action_id: 'a-1' } },
  { state: { kind: 'waiting', reason: 'approval', note: 'Needs your OK' } },
  {
    state: { kind: 'waiting', reason: 'reply', note: null },
    conversation: { kind: 'suggestion', suggestion_id: 's-1' },
  },
  { state: { kind: 'partial', note: null } },
  { state: { kind: 'partial', note: 'Drafted half' } },
  { state: { kind: 'stopped', outcome: 'failed' } },
  { state: { kind: 'stopped', outcome: 'canceled' } },
  { state: { kind: 'not_started', reason: 'week_not_started' }, due: null },
  { deletion_request: { request_id: 'req-1', reason: 'Duplicate' }, author: 'ai', carried: true },
].map((overrides, index) => ({ ...OPEN_TODO, todo_id: `todo-${index + 2}`, ...overrides }));

const DELETED_TODO = {
  todo_id: 'todo-9',
  project_id: 'project-1',
  text: 'Old task',
  deleted_by: 'ai_approved_by_user',
  deleted_at: '2026-11-01T00:00:00.000000Z',
  purge_on: '2026-12-01',
};

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// The body `public_agent_http_error_handler` returns for a refused TODO request.
function refusal(status, errorCode) {
  return jsonResponse(status, {
    detail: { error_code: errorCode, error_type: 'conflict_error', request_id: 'r-1' },
  });
}

/** The TODO fetcher over the real loopback client, with `fetch` answered by `respond`. */
async function withTodoBackend(respond, run) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url: String(url), options });
    return respond(requests.length);
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'local-api-token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });
  try {
    await run(
      createTodoFetcher({ requestJson: client.requestJson, getUserId: () => 'user 1' }),
      requests
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('every route reaches its backend path with the token, and each response variant parses', async () => {
  const page = {
    today: '2026-11-04',
    current_week_start: '2026-11-02',
    week: { start: '2026-11-02', end: '2026-11-08' },
    items: [OPEN_TODO, ...STATE_VARIANTS],
  };
  const checked = { ...OPEN_TODO, state: STATE_VARIANTS[0].state };
  const next = {
    ...OPEN_TODO,
    todo_id: 'todo-next',
    origin: 'recurrence',
    week_start: '2026-11-09',
  };
  const responses = [
    jsonResponse(200, page),
    jsonResponse(200, OPEN_TODO),
    jsonResponse(200, { todo: checked, next_todo: next }),
    jsonResponse(200, { todo: OPEN_TODO, next_todo: null }),
    jsonResponse(200, { ...OPEN_TODO, due: null }),
    jsonResponse(200, OPEN_TODO),
    jsonResponse(200, OPEN_TODO),
    jsonResponse(200, OPEN_TODO),
    jsonResponse(200, [DELETED_TODO]),
  ];
  await withTodoBackend(
    (count) => responses[count - 1],
    async (todos, requests) => {
      const create = {
        client_todo_id: 'c-1',
        project_id: null,
        text: 'Send the estimate',
        week_start: '2026-11-04',
        due: null,
      };
      assert.deepEqual(
        await todos.listWeek({
          scope: 'organization',
          organization_id: 'org-1',
          project: 'any',
          week_start: '2026-11-04',
        }),
        page
      );
      assert.deepEqual(await todos.create(create), { kind: 'ok', result: OPEN_TODO });
      assert.deepEqual(await todos.setCheck({ todo_id: 'todo 1', checked: true }), {
        kind: 'ok',
        result: { todo: checked, next_todo: next },
      });
      assert.deepEqual(await todos.setCheck({ todo_id: 'todo 1', checked: false }), {
        kind: 'ok',
        result: { todo: OPEN_TODO, next_todo: null },
      });
      await todos.setDue({ todo_id: 'todo 1', due: null });
      await todos.delete({ todo_id: 'todo 1' });
      await todos.answerDeletion({ todo_id: 'todo 1', request_id: 'req/1', answer: 'keep' });
      await todos.restore({ todo_id: 'todo 1' });
      assert.deepEqual(await todos.listDeleted({ scope: 'all' }), [DELETED_TODO]);

      assert.deepEqual(
        requests.map(({ url, options }) => [
          options.method,
          url.replace(BASE, ''),
          options.body === undefined ? undefined : JSON.parse(options.body),
        ]),
        [
          [
            'GET',
            '/weeks?scope=organization&organization_id=org-1&project=any&week_start=2026-11-04',
            undefined,
          ],
          ['POST', '', create],
          ['PUT', '/todo%201/check', { checked: true }],
          ['PUT', '/todo%201/check', { checked: false }],
          ['PUT', '/todo%201/due', { due: null }],
          ['DELETE', '/todo%201', undefined],
          ['POST', '/todo%201/deletion-requests/req%2F1/answer', { answer: 'keep' }],
          ['POST', '/todo%201/restore', undefined],
          ['GET', '/deleted?scope=all', undefined],
        ]
      );
      for (const { options } of requests) {
        assert.equal(options.headers.Authorization, 'Bearer local-api-token');
        assert.ok(options.signal instanceof AbortSignal);
      }
    }
  );
});

test('a refusal keeps its error code; other failures and off-contract responses throw', async () => {
  const responses = [
    refusal(409, 'TODO_BUSY'),
    refusal(409, 'TODO_REQUEST_STALE'),
    refusal(409, 'TODO_RESTORE_EXPIRED'),
    refusal(409, 'TODO_DELETED'),
    refusal(404, 'TODO_NOT_FOUND'),
    refusal(422, 'TODO_INVALID_TEXT'),
    refusal(404, 'PROJECT_NOT_FOUND'),
    refusal(409, 'TODO_CLIENT_ID_CONFLICT'),
    // FastAPI's own validation error carries no code.
    jsonResponse(422, { detail: [{ loc: ['body', 'text'], msg: 'too long' }] }),
    refusal(500, 'UNEXPECTED_SERVER_ERROR'),
    jsonResponse(200, { ...OPEN_TODO, state: { kind: 'partial' } }),
    jsonResponse(200, { ...OPEN_TODO, state: { kind: 'done', by: 'ai', at: 'x' }, extra: 1 }),
    jsonResponse(200, [{ ...DELETED_TODO, deleted_by: 'ai' }]),
  ];
  const create = {
    client_todo_id: 'c',
    project_id: 'p',
    text: 't',
    week_start: '2026-11-02',
    due: null,
  };
  await withTodoBackend(
    (count) => responses[count - 1],
    async (todos) => {
      const refused = (code) => ({ kind: 'refused', code });
      assert.deepEqual(await todos.delete({ todo_id: 't' }), refused('TODO_BUSY'));
      assert.deepEqual(
        await todos.answerDeletion({ todo_id: 't', request_id: 'r', answer: 'approve' }),
        refused('TODO_REQUEST_STALE')
      );
      assert.deepEqual(await todos.restore({ todo_id: 't' }), refused('TODO_RESTORE_EXPIRED'));
      assert.deepEqual(
        await todos.setCheck({ todo_id: 't', checked: true }),
        refused('TODO_DELETED')
      );
      assert.deepEqual(await todos.setDue({ todo_id: 't', due: null }), refused('TODO_NOT_FOUND'));
      assert.deepEqual(await todos.create(create), refused('TODO_INVALID_TEXT'));
      assert.deepEqual(await todos.create(create), refused('PROJECT_NOT_FOUND'));
      assert.deepEqual(await todos.create(create), refused('TODO_CLIENT_ID_CONFLICT'));
      await assert.rejects(todos.create(create), (error) => error.status === 422);
      await assert.rejects(todos.delete({ todo_id: 't' }), (error) => error.status === 500);
      await assert.rejects(todos.restore({ todo_id: 't' }), /TODO response is invalid/);
      await assert.rejects(todos.restore({ todo_id: 't' }), /TODO response is invalid/);
      await assert.rejects(
        todos.listDeleted({ scope: 'all' }),
        /deleted TODOs response is invalid/
      );
    }
  );
});

function todoHandlers() {
  const handlers = new Map();
  const calls = [];
  const record = (name) => async (request) => {
    calls.push([name, request]);
    return { kind: 'ok', result: OPEN_TODO };
  };
  registerTodoHandlers(
    {
      todos: {
        listWeek: record('listWeek'),
        create: record('create'),
        setCheck: record('setCheck'),
        setDue: record('setDue'),
        delete: record('delete'),
        answerDeletion: record('answerDeletion'),
        restore: record('restore'),
        listDeleted: record('listDeleted'),
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  return {
    calls,
    invoke: (channel, payload) => handlers.get(channel)({ sender: { id: 1 } }, payload),
  };
}

test('TODO IPC validates the renderer payload before anything reaches the backend', async () => {
  const ipc = todoHandlers();
  const valid = {
    'todos:listWeek': { scope: 'all', project: 'project', project_id: 'p-1' },
    'todos:create': {
      client_todo_id: 'c-1',
      project_id: null,
      text: ' two\nlines ',
      week_start: '2026-11-04',
      due: { date: '2026-11-06', repeat: 'none' },
    },
    'todos:setCheck': { todo_id: 't-1', checked: false },
    'todos:setDue': { todo_id: 't-1', due: null },
    'todos:delete': { todo_id: 't-1' },
    'todos:answerDeletion': { todo_id: 't-1', request_id: 'r-1', answer: 'approve' },
    'todos:restore': { todo_id: 't-1' },
    'todos:listDeleted': { scope: 'no_organization' },
  };
  for (const [channel, payload] of Object.entries(valid)) await ipc.invoke(channel, payload);
  // The text goes as written: whether it is one line is the backend's to decide.
  assert.deepEqual(
    ipc.calls.map(([, request]) => request),
    Object.values(valid)
  );

  const invalid = {
    'todos:listWeek': [
      { scope: 'organization', project: 'any' },
      { scope: 'all', organization_id: 'org-1', project: 'any' },
      { scope: 'all', project: 'project' },
      { scope: 'all', project: 'no_project', project_id: 'p-1' },
      { scope: 'all', project: 'any', week_start: '11/04/2026' },
      { scope: 'all', project: 'any', user_id: 'someone-else' },
    ],
    'todos:create': [
      { ...valid['todos:create'], client_todo_id: '' },
      { ...valid['todos:create'], client_todo_id: 'x'.repeat(129) },
      { ...valid['todos:create'], text: 'x'.repeat(32_001) },
      { ...valid['todos:create'], due: { date: '2026-11-06', repeat: 'daily' } },
      { ...valid['todos:create'], project_id: undefined },
    ],
    'todos:setCheck': [{ todo_id: 't-1', checked: 'true' }, { checked: true }],
    'todos:setDue': [{ todo_id: 't-1' }, { todo_id: 't-1', due: { date: '2026-11-06' } }],
    'todos:delete': [{}, { todo_id: '' }],
    'todos:answerDeletion': [{ todo_id: 't-1', request_id: 'r-1', answer: 'delete' }],
    'todos:restore': [{ todo_id: 't-1', extra: 1 }],
    'todos:listDeleted': [{ scope: 'organization' }, { scope: 'all', project: 'any' }],
  };
  for (const [channel, payloads] of Object.entries(invalid)) {
    for (const payload of payloads) {
      await assert.rejects(ipc.invoke(channel, payload), IpcValidationError, channel);
    }
  }
  assert.equal(ipc.calls.length, Object.keys(valid).length);
});
