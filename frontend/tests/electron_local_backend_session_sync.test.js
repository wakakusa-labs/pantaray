const assert = require('assert');
const { EventEmitter } = require('events');
const fs = require('fs/promises');
const net = require('net');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  createLocalBackendSessionSync,
  resolveLocalBackendControlSocketPath,
} = require('../electron/dist/auth/localBackendSessionSync.js');

function createJwt(payload) {
  const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `${header}.${body}.signature`;
}

class FakeSocket extends EventEmitter {
  constructor(onWrite) {
    super();
    this._onWrite = onWrite;
    this.destroyed = false;
    this.ended = false;
  }

  write(chunk) {
    this._onWrite(String(chunk), this);
    return true;
  }

  end() {
    this.ended = true;
  }

  setTimeout() {
    return this;
  }

  destroy() {
    this.destroyed = true;
  }
}

function createConnectStub(assertions) {
  const connections = [];
  const connect = (socketPath) => {
    const socket = new FakeSocket((chunk, currentSocket) => {
      connections.push({ socketPath, chunk });
      assertions.onWrite(socketPath, chunk, currentSocket);
    });
    process.nextTick(() => {
      socket.emit('connect');
    });
    return socket;
  };
  return { connect, connections };
}

test('resolveLocalBackendControlSocketPath returns the privileged UDS path', () => {
  assert.equal(
    resolveLocalBackendControlSocketPath('/tmp/pantaray-user'),
    path.join('/tmp/pantaray-user', 'local-backend', 'control.sock')
  );
});

const SOCKET_PATH = path.join('/tmp/pantaray-user', 'local-backend', 'control.sock');

function createSync(connect) {
  return createLocalBackendSessionSync({ getControlSocketPath: () => SOCKET_PATH, connect });
}

test('localBackendSessionSync sets the cloud session with JWT-derived expiry and returns the generation', async () => {
  const observedRequests = [];
  const { connect, connections } = createConnectStub({
    onWrite(socketPath, chunk, socket) {
      observedRequests.push({ socketPath, chunk });
      socket.emit(
        'data',
        `${JSON.stringify({
          ok: true,
          cloud_session_state: 'present',
          credential_generation: 3,
          helper_instance_id: 'helper-a',
        })}\n`
      );
    },
  });
  const token = createJwt({ sub: 'user-1', exp: 1_900_000_000 });

  const result = await createSync(connect).setCloudSession('user-1', token, '1');

  assert.equal(connections.length, 1);
  assert.equal(observedRequests[0].socketPath, SOCKET_PATH);
  assert.deepStrictEqual(JSON.parse(observedRequests[0].chunk.trim()), {
    operation: 'set_cloud_session',
    payload: {
      state: 'present',
      account_user_id: 'user-1',
      access_token: token,
      expires_at: '2030-03-17T17:46:40.000Z',
      session_version: '1',
    },
  });
  assert.deepStrictEqual(result, {
    cloudSessionState: 'present',
    credentialGeneration: 3,
    helperInstanceId: 'helper-a',
  });
});

test('localBackendSessionSync configures the helper with the whole cloud session', async () => {
  const observedRequests = [];
  const { connect } = createConnectStub({
    onWrite(socketPath, chunk, socket) {
      observedRequests.push(JSON.parse(chunk.trim()));
      socket.emit(
        'data',
        `${JSON.stringify({
          ok: true,
          configured: true,
          cloud_session_state: 'expired',
          credential_generation: 1,
          helper_instance_id: 'helper-a',
        })}\n`
      );
    },
  });

  const result = await createSync(connect).configure(
    {
      state: 'expired',
      accountUserId: 'user-1',
      sessionVersion: '4',
    },
    { llmConnection: null, webSearchCredential: null }
  );

  assert.deepStrictEqual(observedRequests, [
    {
      operation: 'configure',
      payload: {
        cloud_session: { state: 'expired', account_user_id: 'user-1', session_version: '4' },
        llm_connection: null,
        web_search_credential: null,
      },
    },
  ]);
  assert.deepStrictEqual(result, {
    cloudSessionState: 'expired',
    credentialGeneration: 1,
    helperInstanceId: 'helper-a',
  });
});

test('localBackendSessionSync rejects token subject mismatch', async () => {
  const { connect } = createConnectStub({ onWrite() {} });
  const token = createJwt({ sub: 'user-2', exp: 1_900_000_000 });

  await assert.rejects(
    () => createSync(connect).setCloudSession('user-1', token, '1'),
    /access_token subject does not match account_user_id/
  );
});

test('localBackendSessionSync rejects non-integer session_version input', async () => {
  const { connect } = createConnectStub({ onWrite() {} });
  const token = createJwt({ sub: 'user-1', exp: 1_900_000_000 });

  await assert.rejects(
    () => createSync(connect).setCloudSession('user-1', token, 'session-1'),
    /session_version must be a positive integer string/
  );
});

test('localBackendSessionSync clears the cloud session with its fencing fields', async () => {
  const observedRequests = [];
  const { connect } = createConnectStub({
    onWrite(socketPath, chunk, socket) {
      observedRequests.push(JSON.parse(chunk.trim()));
      socket.emit(
        'data',
        `${JSON.stringify({ ok: true, cloud_session_state: 'present', stale: true })}\n`
      );
    },
  });

  const result = await createSync(connect).clearCloudSession({
    reason: 'signed_out',
    accountUserId: 'user-1',
    sessionVersion: '1',
    credentialGeneration: 2,
    helperInstanceId: 'helper-a',
  });

  assert.deepStrictEqual(observedRequests, [
    {
      operation: 'clear_cloud_session',
      payload: {
        reason: 'signed_out',
        account_user_id: 'user-1',
        session_version: '1',
        credential_generation: 2,
        helper_instance_id: 'helper-a',
      },
    },
  ]);
  assert.deepStrictEqual(result, { cloudSessionState: 'present', stale: true });
});

test('localBackendSessionSync surfaces control socket errors', async () => {
  const { connect } = createConnectStub({
    onWrite(socketPath, chunk, socket) {
      socket.emit(
        'data',
        `${JSON.stringify({ ok: false, error_code: 'invalid_request', message: 'bad' })}\n`
      );
    },
  });

  await assert.rejects(
    () =>
      createSync(connect).configure(
        { state: 'absent' },
        { llmConnection: null, webSearchCredential: null }
      ),
    /Local control socket request failed \(invalid_request\): bad/
  );
});

test('localBackendSessionSync times out and closes a control socket that never responds', async () => {
  const socketDir = await fs.mkdtemp(path.join(os.tmpdir(), 'pantaray-session-sync-'));
  const socketPath = path.join(socketDir, 'control.sock');
  const acceptedSockets = new Set();
  let resolveAccepted;
  let resolveClientClosed;
  let regressionTimer;
  const accepted = new Promise((resolve) => {
    resolveAccepted = resolve;
  });
  const clientClosed = new Promise((resolve) => {
    resolveClientClosed = resolve;
  });
  const server = net.createServer((socket) => {
    acceptedSockets.add(socket);
    resolveAccepted();
    socket.on('close', () => {
      acceptedSockets.delete(socket);
      resolveClientClosed();
    });
    socket.resume();
  });

  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(socketPath, resolve);
    });
    const sync = createLocalBackendSessionSync({
      getControlSocketPath: () => socketPath,
      requestTimeoutMs: 50,
    });

    const clearPromise = sync.clearCloudSession({
      reason: 'signed_out',
      accountUserId: 'user-1',
      sessionVersion: '1',
      credentialGeneration: 1,
      helperInstanceId: 'helper-a',
    });
    await Promise.race([
      (async () => {
        await accepted;
        await assert.rejects(clearPromise, /Local control socket request timed out after 50 ms\./);
        await clientClosed;
      })(),
      new Promise((_, reject) => {
        regressionTimer = setTimeout(
          () => reject(new Error('Control socket timeout regression test exceeded 1,000 ms.')),
          1_000
        );
      }),
    ]);
  } finally {
    clearTimeout(regressionTimer);
    for (const socket of acceptedSockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
    await fs.rm(socketDir, { recursive: true, force: true });
  }
});

test('configure carries saved direct credentials alongside an absent cloud session', async () => {
  const observed = [];
  const { connect } = createConnectStub({
    onWrite(_path, chunk, socket) {
      observed.push(JSON.parse(chunk));
      socket.emit(
        'data',
        JSON.stringify({
          ok: true,
          configured: true,
          cloud_session_state: 'absent',
          credential_generation: 0,
          helper_instance_id: 'helper-a',
        }) + '\n'
      );
    },
  });
  const connections = {
    llmConnection: {
      kind: 'api_key',
      provider: 'openai',
      model: 'custom-model',
      api_key: 'test-key',
    },
    webSearchCredential: { provider: 'tavily', api_key: 'test-search-key' },
    integrationCredential: { provider: 'composio', api_key: 'test-integration-key' },
  };
  await createSync(connect).configure({ state: 'absent' }, connections);
  assert.deepStrictEqual(observed, [
    {
      operation: 'configure',
      payload: {
        cloud_session: { state: 'absent' },
        llm_connection: connections.llmConnection,
        web_search_credential: connections.webSearchCredential,
        integration_credential: connections.integrationCredential,
      },
    },
  ]);
});

test('the integration credential is set and cleared, and a mismatched answer is a failure', async () => {
  const observed = [];
  let configured = true;
  const { connect } = createConnectStub({
    onWrite(_path, chunk, socket) {
      observed.push(JSON.parse(chunk));
      socket.emit('data', JSON.stringify({ ok: true, integration_configured: configured }) + '\n');
    },
  });
  const sync = createSync(connect);
  const credential = { provider: 'composio', api_key: 'integration-key' };
  await sync.setIntegrationCredential(credential);
  configured = false;
  await sync.setIntegrationCredential(null);
  await assert.rejects(sync.setIntegrationCredential(credential), /response is invalid/);
  assert.deepStrictEqual(observed.slice(0, 2), [
    { operation: 'set_integration_credential', payload: credential },
    { operation: 'clear_integration_credential', payload: {} },
  ]);
});

test('direct settings and removal use the existing serialized control channel', async () => {
  const observed = [];
  let releaseFirst;
  const { connect } = createConnectStub({
    onWrite(_path, chunk, socket) {
      const request = JSON.parse(chunk);
      observed.push(request);
      const reply = () =>
        socket.emit(
          'data',
          JSON.stringify({ ok: true, llm_route: 'cloud', web_search_route: 'cloud' }) + '\n'
        );
      if (observed.length === 1) releaseFirst = reply;
      else reply();
    },
  });
  const sync = createSync(connect);
  const connection = {
    kind: 'chatgpt',
    model: 'model',
    credential: {
      access_token: 'test-token',
      expires_at: '2027-01-01T00:00:00.000Z',
      account_id: 'account-1',
    },
  };
  const first = sync.setLlmConnection(connection);
  const next = sync.setWebSearchCredential({ provider: 'tavily', api_key: 'search-key' });
  const clearLlm = sync.setLlmConnection(null);
  const clearSearch = sync.setWebSearchCredential(null);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(observed.length, 1, 'a pending control mutation must finish before another is sent');
  releaseFirst();
  assert.deepStrictEqual(await Promise.all([first, next, clearLlm, clearSearch]), [
    'cloud',
    'cloud',
    'cloud',
    'cloud',
  ]);
  assert.deepStrictEqual(observed, [
    { operation: 'set_llm_connection', payload: connection },
    {
      operation: 'set_web_search_credential',
      payload: { provider: 'tavily', api_key: 'search-key' },
    },
    { operation: 'clear_llm_connection', payload: {} },
    { operation: 'clear_web_search_credential', payload: {} },
  ]);
});

test('connection status projects only state and identities, excluding privileged tokens', async () => {
  const { connect } = createConnectStub({
    onWrite(_path, chunk, socket) {
      assert.deepStrictEqual(JSON.parse(chunk), { operation: 'status', payload: {} });
      socket.emit(
        'data',
        JSON.stringify({
          ok: true,
          helper_instance_id: 'helper-a',
          active_owner_id: 'local-owner',
          configured: true,
          cloud_session_state: 'absent',
          llm_route: 'direct',
          web_search_route: 'unconfigured',
          local_api_token: 'never-return-this',
        }) + '\n'
      );
    },
  });
  assert.deepStrictEqual(await createSync(connect).getConnectionStatus(), {
    helperInstanceId: 'helper-a',
    activeOwnerId: 'local-owner',
    configured: true,
    cloudSessionState: 'absent',
    llmRoute: 'direct',
    webSearchRoute: 'unconfigured',
  });
});

test('an invalid route is rejected without leaking its value or poisoning the next operation', async () => {
  const { connect } = createConnectStub({
    onWrite(_path, chunk, socket) {
      const request = JSON.parse(chunk);
      socket.emit(
        'data',
        JSON.stringify({
          ok: true,
          llm_route: request.operation === 'set_llm_connection' ? 'private-secret' : 'unconfigured',
        }) + '\n'
      );
    },
  });
  const sync = createSync(connect);
  await assert.rejects(
    sync.setLlmConnection({ kind: 'api_key', provider: 'openai', model: 'model', api_key: 'key' }),
    { message: 'Local control socket returned an invalid connection route.' }
  );
  assert.equal(await sync.setLlmConnection(null), 'unconfigured');
});
