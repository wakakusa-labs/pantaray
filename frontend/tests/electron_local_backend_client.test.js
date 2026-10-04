const assert = require('assert');
const { test } = require('node:test');

const {
  createLocalBackendClient,
  LocalBackendRequestError,
} = require('../electron/dist/localBackend/client.js');

const ACTION_STATE_REQUEST = { path: '/v1/agents/users/u/actions/a/state', method: 'GET' };
const readyClient = createLocalBackendClient({
  getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
  getLocalApiToken: () => 'token',
  getRuntimeState: () => ({ status: 'ready', message: null }),
});

test('local backend client は現在のローカル API トークンを Bearer に使う', async () => {
  const originalFetch = globalThis.fetch;
  const sentAuthorization = [];
  let token = 'helper-token-1';
  globalThis.fetch = async (_url, options) => {
    sentAuthorization.push(options.headers.Authorization);
    return new Response('{}');
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => token,
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });

  try {
    await client.requestJson({ path: '/api/agent/history', method: 'GET' });
    token = 'helper-token-2';
    await client.requestJson({ path: '/api/agent/history', method: 'GET' });
    assert.deepEqual(sentAuthorization, ['Bearer helper-token-1', 'Bearer helper-token-2']);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は 401 を認証エラーコードとして返す', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: 'Authentication required. Please sign in again.' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });

  try {
    await assert.rejects(
      () => readyClient.requestJson({ path: '/api/agent/history', method: 'GET' }),
      (error) =>
        error instanceof LocalBackendRequestError &&
        error.status === 401 &&
        error.errorCode === 'AUTHENTICATION_REQUIRED' &&
        error.message === 'Authentication required. Please sign in again.'
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は helper が無い間 request を送らない', async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response('{}');
  };
  const cases = [
    [{ status: 'ready', message: null }, null, 'Local runtime is initializing.'],
    [{ status: 'syncing', message: null }, 'token', 'Local runtime is initializing.'],
    [{ status: 'unknown', message: null }, 'token', 'Local runtime is initializing.'],
    [{ status: 'degraded', message: 'helper stopped' }, 'token', 'helper stopped'],
  ];

  try {
    for (const [runtimeState, token, message] of cases) {
      const client = createLocalBackendClient({
        getRuntimeBackendUrl: () => 'http://127.0.0.1:61131/',
        getLocalApiToken: () => token,
        getRuntimeState: () => runtimeState,
      });
      await assert.rejects(
        () => client.requestJson({ path: '/api/agent/history', method: 'GET' }),
        (error) => error instanceof LocalBackendRequestError && error.message === message
      );
    }
    assert.equal(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は token 送信前に non-loopback URL を拒否する', async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response('{}');
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'https://attacker.example.test',
    getLocalApiToken: () => 'secret-token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });

  try {
    await assert.rejects(() => client.requestJson({ path: '/api/agent/history', method: 'GET' }));
    assert.equal(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は redirect を拒否する', async () => {
  const originalFetch = globalThis.fetch;
  let redirectMode = null;
  globalThis.fetch = async (_url, options) => {
    redirectMode = options.redirect;
    return new Response('{}');
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });

  try {
    await client.requestJson({ path: '/api/agent/history', method: 'GET' });
    assert.equal(redirectMode, 'error');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は未許可 path/method を token 送信前に拒否する', async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response('{}');
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'secret-token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });

  try {
    await assert.rejects(
      () => client.requestJson({ path: '/not-allowed', method: 'GET' }),
      (error) =>
        error instanceof LocalBackendRequestError &&
        error.message === 'Local backend route is not allowed.'
    );
    await assert.rejects(
      () => client.requestJson({ path: '/api/agent/history', method: 'POST' }),
      (error) =>
        error instanceof LocalBackendRequestError &&
        error.message === 'Local backend route is not allowed.'
    );
    await assert.rejects(() =>
      client.requestJson({ path: '/v1/agents/users/user-1/actions/messages', method: 'GET' })
    );
    // Deleting a history row is DELETE on a conversation or a Suggestion, and nothing else.
    for (const [path, method] of [
      ['/api/agent/history/items/conversation/action-1', 'GET'],
      ['/api/agent/history/items/process/process-1', 'DELETE'],
      ['/api/agent/history/items/conversation/action-1/extra', 'DELETE'],
      ['/api/agent/history', 'DELETE'],
      ['/v1/agents/users/user-1/suggestions/welcome', 'GET'],
      ['/v1/agents/users/user-1/suggestions/sug-1', 'POST'],
    ]) {
      await assert.rejects(
        () => client.requestJson({ path, method }),
        (error) =>
          error instanceof LocalBackendRequestError &&
          error.message === 'Local backend route is not allowed.'
      );
    }
    for (const method of ['GET', 'PUT', 'DELETE']) {
      await assert.rejects(
        () => client.requestJson({ path: '/local/action-screen-capture', method }),
        (error) =>
          error instanceof LocalBackendRequestError &&
          error.message === 'Local backend route is not allowed.'
      );
    }
    assert.equal(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client の route allowlist は現在の全 call site を許可する', async () => {
  const originalFetch = globalThis.fetch;
  const requested = [];
  globalThis.fetch = async (url, options) => {
    requested.push({ url: String(url), method: options.method });
    return new Response('{}');
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });
  const routes = [
    ['/local/action-screen-capture', 'POST'],
    ['/v1/agents/users/user-1/suggestions/welcome', 'POST'],
    ['/api/agent/history', 'GET'],
    ['/api/agent/history/suggestion-1/overlay-bootstrap', 'GET'],
    ['/api/agent/history/items/conversation/action-1', 'DELETE'],
    ['/api/agent/history/items/suggestion/suggestion-1', 'DELETE'],
    ['/v1/agents/users/user-1/actions/action-1/approvals', 'POST'],
    ['/v1/agents/users/user-1/actions/action-1/approval-mode', 'GET'],
    ['/v1/agents/users/user-1/actions/action-1/approval-mode', 'PUT'],
    ['/v1/agents/users/user-1/actions/messages', 'POST'],
    [ACTION_STATE_REQUEST.path, ACTION_STATE_REQUEST.method],
    ['/v1/agents/users/user-1/actions/action-1/steps/step-1/output', 'GET'],
    ['/v1/agents/users/user-1/workspace-settings', 'GET'],
    ['/v1/agents/users/user-1/workspace-settings/read-access-scope', 'GET'],
    ['/v1/agents/users/user-1/workspace-settings/read-access-scope', 'PUT'],
    ['/v1/agents/users/user-1/workspace-settings/projects/order', 'PUT'],
    ['/v1/agents/users/user-1/workspace-settings/organizations', 'POST'],
    ['/v1/agents/users/user-1/workspace-settings/projects', 'POST'],
    ['/v1/agents/users/user-1/workspace-settings/folders', 'POST'],
    ['/v1/agents/users/user-1/workspace-settings/organizations/org-1', 'DELETE'],
    ['/v1/agents/users/user-1/workspace-settings/projects/project-1', 'DELETE'],
    ['/v1/agents/users/user-1/workspace-settings/folders/folder-1', 'DELETE'],
    ['/v1/agents/users/user-1/workspace-settings/projects/project-1/links', 'PUT'],
    ['/v1/agents/users/user-1/workspace-settings/folders/folder-1/links', 'PUT'],
    [
      '/v1/agents/users/user-1/approval-preferences/workspace-edit-and-command',
      'GET',
    ],
    [
      '/v1/agents/users/user-1/approval-preferences/workspace-edit-and-command',
      'PUT',
    ],
  ];

  try {
    for (const [path, method] of routes) {
      await client.requestJson({ path, method });
    }
    assert.equal(requested.length, routes.length);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は caller 指定時だけ timeout signal を渡し失敗を型付けする', async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = async (_url, options) => {
    if (fetchCalls++ === 0) {
      assert.equal(Object.prototype.hasOwnProperty.call(options, 'signal'), false);
      return new Response('{}');
    }
    assert.ok(options.signal instanceof AbortSignal);
    if (fetchCalls === 2) throw new DOMException('The operation timed out.', 'TimeoutError');
    const responseStatus = fetchCalls === 3 ? 200 : 409;
    return {
      ok: responseStatus === 200,
      status: responseStatus,
      json: () => Promise.reject(new DOMException('The operation timed out.', 'TimeoutError')),
    };
  };

  try {
    await readyClient.requestJson(ACTION_STATE_REQUEST);
    for (let requestCount = 0; requestCount < 3; requestCount += 1) {
      await assert.rejects(
        () => readyClient.requestJson({ ...ACTION_STATE_REQUEST, timeoutMs: 1_000 }),
        (error) =>
          error instanceof LocalBackendRequestError &&
          error.status === null &&
          error.errorCode === 'LOCAL_BACKEND_REQUEST_TIMEOUT'
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は public type を保持し nested error_code を優先する', async () => {
  const originalFetch = globalThis.fetch;
  const payloads = [
    { type: 'ActionConflict' },
    {
      type: 'ActionConflict',
      detail: { error_code: 'APPROVAL_DECISION_CONFLICT', message: 'Approval conflict.' },
    },
  ];
  globalThis.fetch = async () =>
    new Response(JSON.stringify(payloads.shift()), {
      status: 409,
      headers: { 'Content-Type': 'application/json' },
    });
  try {
    await assert.rejects(
      () => readyClient.requestJson(ACTION_STATE_REQUEST),
      (error) => error instanceof LocalBackendRequestError && error.errorCode === 'ActionConflict'
    );
    await assert.rejects(
      () =>
        readyClient.requestJson({
          path: '/v1/agents/users/user-1/actions/action-1/approvals',
          method: 'POST',
        }),
      (error) =>
        error instanceof LocalBackendRequestError &&
        error.errorCode === 'APPROVAL_DECISION_CONFLICT' &&
        error.message === 'Approval conflict.'
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('local backend client は本文のない 204 を成功として返す', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 204 });
  try {
    assert.equal(
      await readyClient.requestJson({
        path: '/api/agent/history/items/conversation/action-1',
        method: 'DELETE',
      }),
      undefined
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
