const assert = require('assert');
const { test } = require('node:test');

const { createTodoImportFetcher } = require('../electron/dist/todos/todoImportFetch.js');
const { registerTodoImportHandlers } = require('../electron/dist/ipc/handlers/todoImports.js');
const { createLocalBackendClient } = require('../electron/dist/localBackend/client.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');

const IMPORT = '6f1c2a52-7d0e-4c55-9f0e-1f2b3c4d5e6f';
const BASE = 'http://127.0.0.1:61131/v1/agents/users/user%201/todo-imports';

const LINE = {
  text: 'Call the bank',
  due: '2026-11-20',
  starts_on: null,
  duplicate: true,
  keep_duplicate: false,
};

const OPEN_IMPORT = {
  import_id: IMPORT,
  scope: 'organization',
  organization_id: 'org-1',
  revision: 2,
  state: 'open',
  action_id: 'action-1',
  groups: [
    {
      organization: { name: 'A', new: false },
      project: { name: 'Launch', new: true },
      todos: [LINE],
    },
    { organization: null, project: null, todos: [] },
  ],
  issue: { reason: 'project_name_taken', group_index: 0, todo_index: null },
  result: null,
};

const REGISTERED_IMPORT = {
  ...OPEN_IMPORT,
  scope: 'all',
  organization_id: null,
  state: 'registered',
  issue: null,
  result: { organization_ids: [], project_ids: ['p-1'], todo_ids: ['t-1'], skipped_duplicates: 1 },
};

const DRAFT = {
  groups: [
    {
      organization: 'A',
      project: 'Launch',
      todos: [{ text: 'Call the bank', due: '2026-11-20', starts_on: null, keep_duplicate: true }],
    },
    { organization: null, project: null, todos: [{ text: 'Renew the passport' }] },
  ],
};

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function refused(status, reason, groupIndex = null, todoIndex = null) {
  return jsonResponse(status, {
    type: 'TodoImportRefused',
    reason,
    group_index: groupIndex,
    todo_index: todoIndex,
  });
}

async function withImportBackend(respond, run) {
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
      createTodoImportFetcher({ requestJson: client.requestJson, getUserId: () => 'user 1' }),
      requests
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('every import route reaches its backend path with the token, and the views parse', async () => {
  const responses = [OPEN_IMPORT, OPEN_IMPORT, OPEN_IMPORT, REGISTERED_IMPORT, REGISTERED_IMPORT];
  await withImportBackend(
    (count) => jsonResponse(200, responses[count - 1]),
    async (imports, requests) => {
      const start = {
        import_id: IMPORT,
        scope: 'organization',
        organization_id: 'org-1',
        text: 'Call the bank',
      };
      assert.deepEqual(await imports.start(start), { kind: 'ok', result: OPEN_IMPORT });
      await imports.read({ import_id: IMPORT });
      await imports.write({ import_id: IMPORT, expected_revision: 2, draft: DRAFT });
      assert.deepEqual(await imports.register({ import_id: IMPORT, expected_revision: 2 }), {
        kind: 'ok',
        result: REGISTERED_IMPORT,
      });
      await imports.cancel({ import_id: IMPORT });

      // The id that names the path stays out of the body.
      assert.deepEqual(
        requests.map(({ url, options }) => [
          options.method,
          url.replace(BASE, ''),
          options.body === undefined ? undefined : JSON.parse(options.body),
        ]),
        [
          ['POST', '', start],
          ['GET', `/${IMPORT}`, undefined],
          ['PUT', `/${IMPORT}`, { expected_revision: 2, draft: DRAFT }],
          ['POST', `/${IMPORT}/register`, { expected_revision: 2 }],
          ['POST', `/${IMPORT}/cancel`, undefined],
        ]
      );
      for (const { options } of requests) {
        assert.equal(options.headers.Authorization, 'Bearer local-api-token');
      }
    }
  );
});

test('a refusal keeps its reason and indexes; other failures and off-contract views throw', async () => {
  const responses = [
    refused(409, 'stale_revision'),
    refused(422, 'organization_outside_scope', 1),
    refused(422, 'invalid_text', 0, 3),
    refused(404, 'not_found'),
    refused(409, 'start_conflict'),
    refused(422, 'images_invalid'),
    // FastAPI's own validation error is no refusal.
    jsonResponse(422, { detail: [{ loc: ['body', 'import_id'], msg: 'bad' }] }),
    jsonResponse(200, { ...OPEN_IMPORT, state: 'drafting' }),
    jsonResponse(200, { ...OPEN_IMPORT, groups: [{ ...OPEN_IMPORT.groups[0], extra: 1 }] }),
  ];
  const write = { import_id: IMPORT, expected_revision: 1, draft: DRAFT };
  await withImportBackend(
    (count) => responses[count - 1],
    async (imports) => {
      const refusal = (reason, group_index = null, todo_index = null) => ({
        kind: 'refused',
        refusal: { reason, group_index, todo_index },
      });
      assert.deepEqual(await imports.write(write), refusal('stale_revision'));
      assert.deepEqual(await imports.write(write), refusal('organization_outside_scope', 1));
      assert.deepEqual(await imports.write(write), refusal('invalid_text', 0, 3));
      assert.deepEqual(await imports.read({ import_id: IMPORT }), refusal('not_found'));
      assert.deepEqual(
        await imports.start({ import_id: IMPORT, scope: 'all', text: 'x' }),
        refusal('start_conflict')
      );
      assert.deepEqual(
        await imports.start({
          import_id: IMPORT,
          scope: 'all',
          images: [{ kind: 'image', storage_path: 'images/a.png' }],
        }),
        refusal('images_invalid')
      );
      await assert.rejects(imports.write(write), (error) => error.status === 422);
      await assert.rejects(imports.read({ import_id: IMPORT }), /TODO import response is invalid/);
      await assert.rejects(imports.read({ import_id: IMPORT }), /TODO import response is invalid/);
    }
  );
});

test('the open imports are read for the scope, and an off-contract row throws', async () => {
  const opened = { import_id: IMPORT, request: 'Call the bank', started_at: '2026-11-04T03:00Z' };
  const responses = [[opened], [{ ...opened, request: null }]];
  await withImportBackend(
    (count) => jsonResponse(200, responses[count - 1]),
    async (imports, requests) => {
      assert.deepEqual(
        await imports.listOpen({ scope: 'organization', organization_id: 'org 1' }),
        [opened]
      );
      assert.equal(requests[0].options.method, 'GET');
      assert.equal(requests[0].url, `${BASE}?scope=organization&organization_id=org+1`);
      await assert.rejects(imports.listOpen({ scope: 'all' }), /response is invalid/);
    }
  );
});

function importHandlers() {
  const handlers = new Map();
  const calls = [];
  const record = (name) => async (request) => {
    calls.push([name, request]);
    return { kind: 'ok', result: OPEN_IMPORT };
  };
  registerTodoImportHandlers(
    {
      todoImports: {
        start: record('start'),
        listOpen: record('listOpen'),
        read: record('read'),
        write: record('write'),
        register: record('register'),
        cancel: record('cancel'),
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  return {
    calls,
    invoke: (channel, payload) => handlers.get(channel)({ sender: { id: 1 } }, payload),
  };
}

test('TODO import IPC validates the renderer payload before anything reaches the backend', async () => {
  const ipc = importHandlers();
  const file = {
    attachment_id: '00000000-0000-4000-8000-000000000001',
    name: 'minutes.pdf',
    byte_size: 10,
  };
  const image = { kind: 'image', storage_path: 'images/board.png' };
  const valid = [
    ['todoImports:start', { import_id: IMPORT, scope: 'all', text: 'Call\nRenew' }],
    ['todoImports:start', { import_id: IMPORT, scope: 'no_organization', files: [file] }],
    [
      'todoImports:start',
      {
        import_id: IMPORT,
        scope: 'organization',
        organization_id: 'org-1',
        text: null,
        files: [file],
      },
    ],
    // Images alone start an import, and with a document.
    ['todoImports:start', { import_id: IMPORT, scope: 'all', images: [image] }],
    ['todoImports:start', { import_id: IMPORT, scope: 'all', files: [file], images: [image] }],
    ['todoImports:listOpen', { scope: 'all' }],
    ['todoImports:listOpen', { scope: 'organization', organization_id: 'org-1' }],
    ['todoImports:read', { import_id: IMPORT }],
    ['todoImports:write', { import_id: IMPORT, expected_revision: 1, draft: DRAFT }],
    ['todoImports:write', { import_id: IMPORT, expected_revision: 3, draft: { groups: [] } }],
    ['todoImports:register', { import_id: IMPORT, expected_revision: 2 }],
    ['todoImports:cancel', { import_id: IMPORT }],
  ];
  for (const [channel, payload] of valid) await ipc.invoke(channel, payload);
  assert.deepEqual(
    ipc.calls.map(([, request]) => request),
    valid.map(([, payload]) => payload)
  );

  const start = valid[0][1];
  const invalid = {
    'todoImports:start': [
      { ...start, import_id: 'import-1' },
      { ...start, text: '  ' },
      { import_id: IMPORT, scope: 'all' },
      { ...start, scope: 'organization' },
      { ...start, organization_id: 'org-1' },
      { ...start, text: 'x'.repeat(32_001) },
      { ...start, files: [{ ...file, attachment_id: 'x' }] },
      { import_id: IMPORT, scope: 'all', files: [], images: [] },
      { ...start, images: [{ kind: 'image', storage_path: '' }] },
      { ...start, images: [{ storage_path: 'images/board.png' }] },
      { ...start, images: Array.from({ length: 33 }, () => image) },
      { ...start, user_id: 'someone-else' },
    ],
    'todoImports:listOpen': [{}, { scope: 'organization' }, { scope: 'all', organization_id: 'x' }],
    'todoImports:read': [{}, { import_id: IMPORT, extra: 1 }],
    'todoImports:write': [
      { import_id: IMPORT, draft: DRAFT },
      { import_id: IMPORT, expected_revision: 0, draft: DRAFT },
      { import_id: IMPORT, expected_revision: 1, draft: {} },
      {
        import_id: IMPORT,
        expected_revision: 1,
        draft: { groups: [{ organization: '', project: null, todos: [] }] },
      },
      {
        import_id: IMPORT,
        expected_revision: 1,
        draft: {
          groups: [{ organization: null, project: null, todos: [{ text: 'x', due: '11/20' }] }],
        },
      },
      {
        import_id: IMPORT,
        expected_revision: 1,
        draft: { groups: [{ project: null, todos: [] }] },
      },
    ],
    'todoImports:register': [{ import_id: IMPORT }, { import_id: IMPORT, expected_revision: '2' }],
    'todoImports:cancel': [{ import_id: '' }],
  };
  for (const [channel, payloads] of Object.entries(invalid)) {
    for (const payload of payloads) {
      await assert.rejects(ipc.invoke(channel, payload), IpcValidationError, channel);
    }
  }
  assert.equal(ipc.calls.length, valid.length);
});
