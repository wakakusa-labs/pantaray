const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const {
  createLocalConnectionRuntime,
} = require('../electron/dist/aiConnection/localConnectionRuntime.js');
const { createConnectionSettingsStore } = require('../electron/dist/aiConnection/settingsStore.js');
const { createChatgptTokenStore } = require('../electron/dist/auth/chatgptTokenStore.js');

const ABSENT = { token: null, userId: null, sessionVersion: null, expiredIdentity: null };
const PREFERENCES = { method: 'api_key', provider: 'openai', model: 'custom-model' };

function fixture(t, overrides = {}) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'connection-runtime-'));
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(value.split('').reverse().join('')),
    decryptString: (value) => value.toString().split('').reverse().join(''),
  };
  const store = createConnectionSettingsStore({ userDataDir, safeStorage });
  const calls = [];
  const unavailable = [];
  const changed = [];
  let helperInstanceId = 'helper-a';
  let generation = 0;
  const runtime = createLocalConnectionRuntime({
    userDataDir,
    safeStorage,
    onChanged: overrides.onChanged ?? (() => changed.push(true)),
    onRuntimeUnavailable: () => unavailable.push(true),
    whenReady: overrides.whenReady ?? (async () => {}),
    openExternal: async () => {
      throw new Error('unexpected interactive login');
    },
    logger: { error: (...args) => calls.push(['error', ...args]) },
    sessionSync: {
      configure: async (cloud, connections) => {
        calls.push(['configure', cloud, connections]);
        return {
          cloudSessionState: cloud.state,
          credentialGeneration: ++generation,
          helperInstanceId,
        };
      },
      setLlmConnection: async (connection) => {
        calls.push(['llm', connection]);
        await overrides.setLlmConnection?.(connection);
        return connection ? 'direct' : 'unconfigured';
      },
      setWebSearchCredential: async (credential) => {
        calls.push(['search', credential]);
        return credential ? 'direct' : 'unconfigured';
      },
      setIntegrationCredential: async (credential) => {
        calls.push(['integration', credential]);
      },
      clearCloudSession: async () => {
        calls.push(['clearCloud']);
        return { cloudSessionState: 'absent', stale: false };
      },
    },
    helperManager: {
      ensureStarted: async () => {
        await overrides.ensureStarted?.();
        return { helperInstanceId };
      },
      terminateCurrentHelper: async () => {
        calls.push(['terminate']);
        helperInstanceId = 'helper-after-stop';
      },
    },
  });
  t.after(() => {
    runtime.dispose();
    fs.rmSync(userDataDir, { recursive: true, force: true });
  });
  return {
    runtime,
    unavailable,
    changed,
    store,
    calls,
    safeStorage,
    userDataDir,
    restartHelper: () => {
      helperInstanceId = 'helper-b';
    },
  };
}

test('saved settings are included in the first configure, only after Electron is ready', async (t) => {
  let ready;
  const f = fixture(t, {
    whenReady: () =>
      new Promise((resolve) => {
        ready = resolve;
      }),
  });
  f.store.savePreferences(PREFERENCES);
  f.store.saveApiKey('openai', 'private-api-key');
  f.store.saveWebSearchKey('private-search-key');
  f.store.saveIntegrationKey('private-integration-key');
  const apply = f.runtime.authContext.applyAuthContextChange(ABSENT);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(f.calls, [], 'no credential/configuration is sent before app readiness');
  ready();
  await apply;
  assert.deepEqual(f.calls, [
    [
      'configure',
      { state: 'absent' },
      {
        llmConnection: {
          kind: 'api_key',
          provider: 'openai',
          model: 'custom-model',
          api_key: 'private-api-key',
        },
        webSearchCredential: { provider: 'tavily', api_key: 'private-search-key' },
        integrationCredential: { provider: 'composio', api_key: 'private-integration-key' },
      },
    ],
  ]);
});

test('the integration key reaches the running helper, and a restarted one gets it in configure', async (t) => {
  const f = fixture(t);
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  await f.runtime.saveIntegrationKey('private-integration-key');
  const credential = { provider: 'composio', api_key: 'private-integration-key' };
  assert.deepEqual(f.calls.at(-1), ['integration', credential]);
  const settings = await f.runtime.getSettings();
  assert.equal(settings.hasSavedIntegrationKey, true);
  assert.equal(JSON.stringify(settings).includes('private-'), false);

  f.restartHelper();
  await f.runtime.savePreferences(PREFERENCES);
  assert.equal(f.calls.at(-1)[0], 'configure');
  assert.deepEqual(f.calls.at(-1)[2].integrationCredential, credential);

  await f.runtime.removeIntegrationKey();
  assert.deepEqual(f.calls.at(-1), ['integration', null]);
  assert.equal((await f.runtime.getSettings()).hasSavedIntegrationKey, false);
});

test('editing before cloud restoration persists without briefly enabling direct routing', async (t) => {
  const f = fixture(t);
  await f.runtime.savePreferences(PREFERENCES);
  await f.runtime.saveApiKey('openai', 'key');
  assert.deepEqual(f.calls, []);
  await f.runtime.authContext.applyAuthContextChange({
    token: 'cloud-token',
    userId: 'user',
    sessionVersion: '1',
    expiredIdentity: null,
  });
  assert.equal(f.calls[0][0], 'configure');
  assert.equal(f.calls[0][1].state, 'present');
  assert.equal(f.calls[0][2].llmConnection.api_key, 'key');
});

test('selection binds the correct provider key, with no connection for unfinished settings', async (t) => {
  const f = fixture(t);
  f.store.saveApiKey('openai', 'openai-key');
  f.store.saveApiKey('fireworks', 'fireworks-key');
  for (const [preferences, connection] of [
    [{ ...PREFERENCES, model: '' }, null],
    [{ ...PREFERENCES, provider: 'anthropic' }, null],
    [
      { ...PREFERENCES, provider: 'fireworks' },
      { kind: 'api_key', provider: 'fireworks', model: 'custom-model', api_key: 'fireworks-key' },
    ],
    [{ ...PREFERENCES, method: 'chatgpt' }, null],
  ]) {
    await f.runtime.savePreferences(preferences);
    assert.deepEqual((await f.runtime.getConfiguration()).llmConnection, connection);
  }
});

test('settings metadata and disconnect never expose OAuth or API secrets', async (t) => {
  const f = fixture(t);
  const tokens = {
    accessToken: 'private-access-token',
    refreshToken: 'private-refresh-token',
    accountId: 'account-1',
    expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
  };
  createChatgptTokenStore(f).save(tokens);
  f.store.savePreferences({ ...PREFERENCES, method: 'chatgpt' });
  f.store.saveApiKey('openai', 'private-api-key');
  f.store.saveWebSearchKey('private-search-key');
  const state = await f.runtime.getSettings();
  assert.deepEqual(state.chatgpt, { status: 'connected' });
  assert.equal(state.hasSavedApiKey, true);
  assert.equal(state.hasSavedWebSearchKey, true);
  assert.equal(JSON.stringify(state).includes('private-'), false);
  assert.deepEqual((await f.runtime.getConfiguration()).llmConnection, {
    kind: 'chatgpt',
    model: 'custom-model',
    credential: {
      access_token: tokens.accessToken,
      account_id: tokens.accountId,
      expires_at: tokens.expiresAt,
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  f.changed.length = 0;
  assert.deepEqual(await f.runtime.disconnectChatgpt(), { ok: true });
  assert.ok(f.changed.length > 0);
  assert.equal((await f.runtime.getConfiguration()).llmConnection, null);
  assert.equal(f.store.readApiKey('openai'), 'private-api-key');
  assert.equal(f.store.readWebSearchKey(), 'private-search-key');
  assert.equal(createChatgptTokenStore(f).load(), null);
});

test('a restarted helper receives the current saved configuration and cloud identity together', async (t) => {
  const f = fixture(t);
  await f.runtime.savePreferences(PREFERENCES);
  await f.runtime.saveApiKey('openai', 'old-key');
  const expired = { ...ABSENT, expiredIdentity: { userId: 'user-1', sessionVersion: '2' } };
  await f.runtime.authContext.applyAuthContextChange(expired);
  f.restartHelper();
  await f.runtime.saveApiKey('openai', 'new-key');
  const last = f.calls.at(-1);
  assert.equal(last[0], 'configure');
  assert.deepEqual(last[1], { state: 'expired', accountUserId: 'user-1', sessionVersion: '2' });
  assert.equal(last[2].llmConnection.api_key, 'new-key');
});

test('persistence and helper application serialize so an older operation cannot reinstall a newer key', async (t) => {
  let release;
  const f = fixture(t, {
    setLlmConnection: async (connection) => {
      if (connection?.api_key === 'first')
        await new Promise((resolve) => {
          release = resolve;
        });
    },
  });
  f.store.savePreferences(PREFERENCES);
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  const first = f.runtime.saveApiKey('openai', 'first');
  await new Promise((resolve) => setImmediate(resolve));
  const second = f.runtime.saveApiKey('openai', 'second');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(f.store.readApiKey('openai'), 'first');
  release();
  await Promise.all([first, second]);
  assert.deepEqual(
    f.calls.filter(([method]) => method === 'llm').map(([, value]) => value.api_key),
    ['first', 'second']
  );
  assert.equal(f.store.readApiKey('openai'), 'second');
});

test('a failed save preserves the old persisted and running key and rejects the operation', async (t) => {
  const f = fixture(t);
  f.store.savePreferences(PREFERENCES);
  f.store.saveApiKey('openai', 'old-key');
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  f.safeStorage.encryptString = () => {
    throw new Error('native private content');
  };
  await assert.rejects(f.runtime.saveApiKey('openai', 'new-key'), { code: 'write_failed' });
  assert.equal(f.store.readApiKey('openai'), 'old-key');
  assert.deepEqual(f.unavailable, []);
  assert.deepEqual(
    f.calls.map(([method]) => method),
    ['configure']
  );
});

test('a lost removal response stops the old helper and restart does not restore the removed key', async (t) => {
  const f = fixture(t, {
    setLlmConnection: async () => {
      throw new Error('response lost');
    },
  });
  f.store.savePreferences(PREFERENCES);
  f.store.saveApiKey('openai', 'old-key');
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  await assert.rejects(f.runtime.removeApiKey('openai'), /response lost/);
  assert.equal(f.store.readApiKey('openai'), null);
  assert.equal(f.calls.at(-1)[0], 'terminate');
  assert.deepEqual(f.unavailable, [true]);
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  assert.equal(f.calls.at(-1)[0], 'configure');
  assert.equal(f.calls.at(-1)[2].llmConnection, null);
});

test('a read failure after credential removal also contains the running helper', async (t) => {
  const f = fixture(t);
  f.store.savePreferences(PREFERENCES);
  f.store.saveApiKey('openai', 'old-key');
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  fs.writeFileSync(path.join(f.userDataDir, 'tavily-api-key.enc.json'), 'corrupt');
  await assert.rejects(f.runtime.removeApiKey('openai'), { code: 'invalid_data' });
  assert.equal(f.calls.at(-1)[0], 'terminate');
});

test('a helper status failure after removal does not leave old credentials running', async (t) => {
  let fail = false;
  const f = fixture(t, {
    ensureStarted: async () => {
      if (fail) throw new Error('status unavailable');
    },
  });
  f.store.savePreferences(PREFERENCES);
  f.store.saveApiKey('openai', 'old-key');
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  fail = true;
  await assert.rejects(f.runtime.removeApiKey('openai'), /status unavailable/);
  assert.equal(f.calls.at(-1)[0], 'terminate');
  assert.equal(f.store.readApiKey('openai'), null);
});

test('a connection edit after sign-out and helper restart cannot restore the signed-out cloud identity', async (t) => {
  const f = fixture(t);
  f.store.savePreferences(PREFERENCES);
  await f.runtime.authContext.applyAuthContextChange({
    token: 'cloud-token',
    userId: 'user',
    sessionVersion: '1',
    expiredIdentity: null,
  });
  assert.deepEqual(await f.runtime.authContext.clearForSignOut(), { contained: true });
  f.restartHelper();
  await f.runtime.saveApiKey('openai', 'direct-key');
  assert.deepEqual(f.calls.at(-1)[1], { state: 'absent' });
  assert.equal(f.calls.at(-1)[2].llmConnection.api_key, 'direct-key');
});


test('OAuth disconnection notifies the renderer even when helper synchronization fails', async (t) => {
  let fail = false;
  const f = fixture(t, {
    setLlmConnection: async () => {
      if (fail) throw new Error('response lost');
    },
  });
  createChatgptTokenStore(f).save({
    accessToken: 'private-access-token',
    refreshToken: 'private-refresh-token',
    accountId: 'account-1',
    expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
  });
  f.store.savePreferences({ ...PREFERENCES, method: 'chatgpt' });
  await f.runtime.authContext.applyAuthContextChange(ABSENT);
  await new Promise((resolve) => setImmediate(resolve));
  f.changed.length = 0;
  fail = true;
  await assert.rejects(f.runtime.disconnectChatgpt(), /response lost/);
  assert.equal((await f.runtime.getSettings()).chatgpt.status, 'disconnected');
  assert.ok(f.changed.length > 0);
  assert.deepEqual(f.unavailable, [true]);
});


test('a closed renderer cannot turn a persisted setting or OAuth disconnect into failure', async (t) => {
  const f = fixture(t, { onChanged: () => { throw new Error('WebContents was destroyed'); } });
  await f.runtime.saveApiKey('openai', 'new-key');
  assert.equal(f.store.readApiKey('openai'), 'new-key');
  await f.runtime.getSettings();
  assert.deepEqual(await f.runtime.disconnectChatgpt(), { ok: true });
  assert.equal((await f.runtime.getSettings()).chatgpt.status, 'disconnected');
});

test('disconnect removes unreadable OAuth data and makes settings available again', async (t) => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.userDataDir, 'chatgpt-oauth.enc.json'), 'corrupt');
  await assert.rejects(f.runtime.getSettings(), { code: 'invalid_data' });
  await assert.rejects(f.runtime.authContext.applyAuthContextChange(ABSENT), { code: 'invalid_data' });
  assert.deepEqual(await f.runtime.disconnectChatgpt(), { ok: true });
  assert.equal((await f.runtime.getSettings()).chatgpt.status, 'disconnected');
  assert.equal(createChatgptTokenStore(f).load(), null);
  assert.equal(f.calls.at(-1)[0], 'configure');
  assert.equal(f.calls.at(-1)[2].llmConnection, null);
  assert.ok(f.changed.length > 0);
});


test('failed deletion of unreadable OAuth data remains a failure with the stored data intact', async (t) => {
  const f = fixture(t);
  const storagePath = path.join(f.userDataDir, 'chatgpt-oauth.enc.json');
  fs.mkdirSync(storagePath);
  assert.equal((await f.runtime.disconnectChatgpt()).ok, false);
  assert.equal(fs.statSync(storagePath).isDirectory(), true);
  assert.deepEqual(f.calls, []);
});


test('a state read concurrent with cold disconnect cannot load the removed credential', async (t) => {
  const f = fixture(t);
  createChatgptTokenStore(f).save({
    accessToken: 'private-access-token',
    refreshToken: 'private-refresh-token',
    accountId: 'account-1',
    expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
  });
  f.store.savePreferences({ ...PREFERENCES, method: 'chatgpt' });
  await Promise.all([f.runtime.disconnectChatgpt(), f.runtime.getSettings()]);
  assert.equal((await f.runtime.getSettings()).chatgpt.status, 'disconnected');
  assert.equal((await f.runtime.getConfiguration()).llmConnection, null);
  assert.equal(createChatgptTokenStore(f).load(), null);
});
