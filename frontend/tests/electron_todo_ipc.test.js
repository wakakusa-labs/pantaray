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
  starts_on: '2026-11-02',
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
  { state: { kind: 'not_started', reason: 'scheduled' }, due: null },
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
    starts_on: '2026-11-09',
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
    jsonResponse(200, { today: '2026-11-04', items: [OPEN_TODO, next] }),
    jsonResponse(200, { ...OPEN_TODO, starts_on: '2026-11-09' }),
  ];
  await withTodoBackend(
    (count) => responses[count - 1],
    async (todos, requests) => {
      const create = {
        client_todo_id: 'c-1',
        project_id: null,
        text: 'Send the estimate',
        starts_on: '2026-11-04',
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
      assert.deepEqual(await todos.list({ scope: 'all', project: 'no_project', sort: 'due' }), {
        today: '2026-11-04',
        items: [OPEN_TODO, next],
      });
      await todos.setStart({ todo_id: 'todo 1', starts_on: '2026-11-09' });

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
          ['GET', '?scope=all&project=no_project&sort=due', undefined],
          ['PUT', '/todo%201/start', { starts_on: '2026-11-09' }],
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
    starts_on: '2026-11-02',
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

const GOAL = {
  project_id: 'project 1',
  text: 'Ship 40 estimates',
  target: 40,
  unit: '件',
  deadline: '2026-12-31',
  set_at: '2026-11-01T00:00:00.000000Z',
};

const GOAL_REQUEST = {
  project_id: 'project 1',
  text: 'Ship 40 estimates',
  target: 40,
  unit: '件',
  deadline: '2026-12-31',
};

const RECORD_REQUEST = {
  todo_id: 'todo 1',
  client_record_id: 'c-rec-1',
  quantity: 2.5,
  unit: '件',
  note: 'Sent two and a half',
};

const USER_RECORD = {
  record_id: 'rec-1',
  todo_id: 'todo 1',
  project_id: 'project 1',
  quantity: 2.5,
  unit: '件',
  note: 'Sent two and a half',
  evidence: { kind: 'user' },
  recorded_at: '2026-11-03T01:02:03.000000Z',
};

const AI_RECORD = {
  ...USER_RECORD,
  record_id: 'rec-2',
  project_id: null,
  evidence: { kind: 'action_steps', action_id: 'a-1', step_ids: ['s-1', 's-2'] },
};

const WINDOW = { window_start: '2026-10-08', record_ids: ['rec-1'] };

// One goal per progress kind and one measured goal per pace kind the backend sends.
const GOAL_VARIANTS = [
  { kind: 'no_goal', project_id: 'project 2' },
  { kind: 'no_records', goal: GOAL, other_unit_record_ids: ['rec-2'] },
  ...[
    { kind: 'too_few_records', ...WINDOW },
    { kind: 'reached' },
    { kind: 'projected', ...WINDOW, per_day: 0.5, projected_on: '2027-01-20' },
    // Too far ahead to name a day: no `projected_on`.
    { kind: 'beyond_horizon', ...WINDOW, per_day: 0.001 },
  ].map((pace) => ({
    kind: 'measured',
    goal: GOAL,
    total: 2.5,
    share: 0.0625,
    record_ids: ['rec-1'],
    other_unit_record_ids: [],
    expected_share: 0.5,
    deadline_passed: false,
    pace,
  })),
];

const PROGRESS_PAGE = {
  today: '2026-11-04',
  period: 'day',
  page_start: '2026-11-01',
  rows: [
    { start: '2026-11-02', end: '2026-11-02', record_ids: [], checked: [] },
    {
      start: '2026-11-03',
      end: '2026-11-03',
      record_ids: ['rec-1', 'rec-2'],
      checked: [
        {
          todo_id: 'todo 1',
          project_id: null,
          text: 'Send the estimate',
          checked_at: '2026-11-03T00:00:00.000000Z',
        },
      ],
    },
  ],
  goals: GOAL_VARIANTS,
  records: [USER_RECORD, AI_RECORD],
};

test('every progress route reaches its backend path, and each goal, pace and evidence parses', async () => {
  const responses = [
    jsonResponse(200, PROGRESS_PAGE),
    jsonResponse(200, GOAL),
    new Response(null, { status: 204 }),
    jsonResponse(200, USER_RECORD),
    new Response(null, { status: 204 }),
  ];
  await withTodoBackend(
    (count) => responses[count - 1],
    async (todos, requests) => {
      assert.deepEqual(
        await todos.progress({
          scope: 'no_organization',
          project: 'project',
          project_id: 'project 1',
          period: 'day',
          on: '2026-10-15',
        }),
        PROGRESS_PAGE
      );
      assert.deepEqual(await todos.setGoal(GOAL_REQUEST), { kind: 'ok', result: GOAL });
      assert.deepEqual(await todos.clearGoal({ project_id: 'project 1' }), {
        kind: 'ok',
        result: null,
      });
      assert.deepEqual(await todos.addProgressRecord(RECORD_REQUEST), {
        kind: 'ok',
        result: USER_RECORD,
      });
      assert.equal(await todos.deleteProgressRecord({ record_id: 'rec/1' }), undefined);

      // The id that names the path stays out of the body.
      const { project_id: _project, ...goalBody } = GOAL_REQUEST;
      const { todo_id: _todo, ...recordBody } = RECORD_REQUEST;
      assert.deepEqual(
        requests.map(({ url, options }) => [
          options.method,
          url.replace(BASE, ''),
          options.body === undefined ? undefined : JSON.parse(options.body),
        ]),
        [
          [
            'GET',
            '/progress?scope=no_organization&project=project&project_id=project+1&period=day&on=2026-10-15',
            undefined,
          ],
          ['PUT', '/projects/project%201/goal', goalBody],
          ['DELETE', '/projects/project%201/goal', undefined],
          ['POST', '/todo%201/progress-records', recordBody],
          ['DELETE', '/progress-records/rec%2F1', undefined],
        ]
      );
      for (const { options } of requests) {
        assert.equal(options.headers.Authorization, 'Bearer local-api-token');
      }
    }
  );
});

test('a refused goal or record write keeps its code; off-contract progress throws', async () => {
  const responses = [
    refusal(422, 'TODO_GOAL_DEADLINE_BEFORE_SET'),
    refusal(422, 'TODO_PROGRESS_INVALID_TEXT'),
    refusal(404, 'PROJECT_NOT_FOUND'),
    refusal(404, 'PROJECT_NOT_FOUND'),
    refusal(404, 'TODO_NOT_FOUND'),
    refusal(409, 'TODO_DELETED'),
    refusal(409, 'TODO_PROGRESS_CLIENT_ID_CONFLICT'),
    refusal(422, 'TODO_PROGRESS_INVALID_TEXT'),
    refusal(500, 'UNEXPECTED_SERVER_ERROR'),
    // A projection names its day.
    jsonResponse(200, {
      ...PROGRESS_PAGE,
      goals: [{ ...GOAL_VARIANTS[4], pace: { kind: 'projected', ...WINDOW, per_day: 1 } }],
    }),
    jsonResponse(200, {
      ...PROGRESS_PAGE,
      records: [{ ...AI_RECORD, evidence: { kind: 'action_steps', action_id: 'a-1' } }],
    }),
    jsonResponse(200, { ...PROGRESS_PAGE, period: 'year' }),
    jsonResponse(200, { ...PROGRESS_PAGE, page_start: undefined }),
    jsonResponse(200, { ...GOAL, extra: 1 }),
    jsonResponse(200, { ...USER_RECORD, evidence: { kind: 'ai' } }),
  ];
  const progressQuery = { scope: 'all', project: 'any', period: 'day' };
  await withTodoBackend(
    (count) => responses[count - 1],
    async (todos) => {
      const refused = (code) => ({ kind: 'refused', code });
      assert.deepEqual(await todos.setGoal(GOAL_REQUEST), refused('TODO_GOAL_DEADLINE_BEFORE_SET'));
      assert.deepEqual(await todos.setGoal(GOAL_REQUEST), refused('TODO_PROGRESS_INVALID_TEXT'));
      assert.deepEqual(await todos.setGoal(GOAL_REQUEST), refused('PROJECT_NOT_FOUND'));
      assert.deepEqual(await todos.clearGoal({ project_id: 'p' }), refused('PROJECT_NOT_FOUND'));
      assert.deepEqual(await todos.addProgressRecord(RECORD_REQUEST), refused('TODO_NOT_FOUND'));
      assert.deepEqual(await todos.addProgressRecord(RECORD_REQUEST), refused('TODO_DELETED'));
      assert.deepEqual(
        await todos.addProgressRecord(RECORD_REQUEST),
        refused('TODO_PROGRESS_CLIENT_ID_CONFLICT')
      );
      assert.deepEqual(
        await todos.addProgressRecord(RECORD_REQUEST),
        refused('TODO_PROGRESS_INVALID_TEXT')
      );
      await assert.rejects(
        todos.deleteProgressRecord({ record_id: 'r' }),
        (error) => error.status === 500
      );
      for (let index = 0; index < 4; index += 1) {
        await assert.rejects(todos.progress(progressQuery), /TODO progress response is invalid/);
      }
      await assert.rejects(todos.setGoal(GOAL_REQUEST), /TODO goal response is invalid/);
      await assert.rejects(
        todos.addProgressRecord(RECORD_REQUEST),
        /progress record response is invalid/
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
        list: record('list'),
        listWeek: record('listWeek'),
        create: record('create'),
        setCheck: record('setCheck'),
        setDue: record('setDue'),
        setStart: record('setStart'),
        delete: record('delete'),
        answerDeletion: record('answerDeletion'),
        restore: record('restore'),
        listDeleted: record('listDeleted'),
        progress: record('progress'),
        setGoal: record('setGoal'),
        clearGoal: record('clearGoal'),
        addProgressRecord: record('addProgressRecord'),
        deleteProgressRecord: record('deleteProgressRecord'),
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
    'todos:list': { scope: 'all', project: 'any', sort: 'due' },
    'todos:listWeek': { scope: 'all', project: 'project', project_id: 'p-1' },
    'todos:create': {
      client_todo_id: 'c-1',
      project_id: null,
      text: ' two\nlines ',
      starts_on: '2026-11-04',
      due: { date: '2026-11-06', repeat: 'none' },
    },
    'todos:setCheck': { todo_id: 't-1', checked: false },
    'todos:setDue': { todo_id: 't-1', due: null },
    'todos:setStart': { todo_id: 't-1', starts_on: '2026-11-09' },
    'todos:delete': { todo_id: 't-1' },
    'todos:answerDeletion': { todo_id: 't-1', request_id: 'r-1', answer: 'approve' },
    'todos:restore': { todo_id: 't-1' },
    'todos:listDeleted': { scope: 'no_organization' },
    'todos:progress': {
      scope: 'organization',
      organization_id: 'org-1',
      project: 'any',
      period: 'month',
    },
    'todos:setGoal': GOAL_REQUEST,
    'todos:clearGoal': { project_id: 'p-1' },
    'todos:addProgressRecord': RECORD_REQUEST,
    'todos:deleteProgressRecord': { record_id: 'rec-1' },
  };
  for (const [channel, payload] of Object.entries(valid)) await ipc.invoke(channel, payload);
  // 200 code points pass though they are 400 UTF-16 units.
  await ipc.invoke('todos:setGoal', { ...GOAL_REQUEST, text: '😀'.repeat(200) });
  ipc.calls.pop();
  // The text goes as written: whether it is one line is the backend's to decide.
  assert.deepEqual(
    ipc.calls.map(([, request]) => request),
    Object.values(valid)
  );

  const invalid = {
    'todos:list': [
      { scope: 'all', project: 'project' },
      { scope: 'all', project: 'any', sort: 'name' },
      { scope: 'all', project: 'any', week_start: '2026-11-02' },
    ],
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
    'todos:setStart': [{ todo_id: 't-1' }, { todo_id: 't-1', starts_on: '11/09/2026' }],
    'todos:delete': [{}, { todo_id: '' }],
    'todos:answerDeletion': [{ todo_id: 't-1', request_id: 'r-1', answer: 'delete' }],
    'todos:restore': [{ todo_id: 't-1', extra: 1 }],
    'todos:listDeleted': [{ scope: 'organization' }, { scope: 'all', project: 'any' }],
    'todos:progress': [
      { scope: 'all', project: 'any' },
      { scope: 'all', project: 'any', period: 'year' },
      { scope: 'organization', project: 'any', period: 'day' },
      { scope: 'all', organization_id: 'org-1', project: 'any', period: 'day' },
      { scope: 'all', project: 'project', period: 'day' },
      { scope: 'all', project: 'any', project_id: 'p-1', period: 'day' },
      { scope: 'all', project: 'any', period: 'day', week_start: '2026-11-02' },
      { scope: 'all', project: 'any', period: 'month', on: '2026-11' },
    ],
    'todos:setGoal': [
      { ...GOAL_REQUEST, project_id: undefined },
      { ...GOAL_REQUEST, text: '' },
      // 200 code points, each two UTF-16 units: the limit counts code points.
      { ...GOAL_REQUEST, text: '😀'.repeat(201) },
      { ...GOAL_REQUEST, target: 0 },
      { ...GOAL_REQUEST, target: -1 },
      { ...GOAL_REQUEST, target: Infinity },
      { ...GOAL_REQUEST, target: '10' },
      { ...GOAL_REQUEST, unit: 'x'.repeat(21) },
      { ...GOAL_REQUEST, deadline: '2026/12/31' },
      { ...GOAL_REQUEST, set_at: '2026-11-01T00:00:00Z' },
    ],
    'todos:clearGoal': [{}, { project_id: 'p-1', text: 'x' }],
    'todos:addProgressRecord': [
      { ...RECORD_REQUEST, client_record_id: undefined },
      { ...RECORD_REQUEST, todo_id: '' },
      { ...RECORD_REQUEST, quantity: 0 },
      { ...RECORD_REQUEST, quantity: NaN },
      { ...RECORD_REQUEST, unit: '' },
      { ...RECORD_REQUEST, note: '' },
      { ...RECORD_REQUEST, note: 'x'.repeat(201) },
      // The renderer only adds the user's record: the AI's evidence never comes from it.
      { ...RECORD_REQUEST, evidence: { kind: 'action_steps', action_id: 'a', step_ids: [] } },
    ],
    'todos:deleteProgressRecord': [{ record_id: '' }, { todo_id: 't-1' }],
  };
  for (const [channel, payloads] of Object.entries(invalid)) {
    for (const payload of payloads) {
      await assert.rejects(ipc.invoke(channel, payload), IpcValidationError, channel);
    }
  }
  assert.equal(ipc.calls.length, Object.keys(valid).length);
});
