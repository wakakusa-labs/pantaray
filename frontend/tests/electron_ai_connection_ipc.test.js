const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const {
  createLocalConnectionRuntime,
} = require('../electron/dist/aiConnection/localConnectionRuntime.js');
const { createConnectionSettingsStore } = require('../electron/dist/aiConnection/settingsStore.js');
const { registerAiConnectionHandlers } = require('../electron/dist/ipc/handlers/aiConnection.js');
const { createIpcRegistrar } = require('../electron/dist/ipc/registrar.js');
const { createIpcSenderSecurity } = require('../electron/dist/ipc/senderTrust.js');
const { CredentialStorageError } = require('../electron/dist/auth/encryptedJsonFile.js');
const { createSettingsApi } = require('../electron/preload/settings_api.js');

function sender(url, id) {
  const mainFrame = { url };
  return { sender: { id, mainFrame }, senderFrame: mainFrame };
}

function fixture(t) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'connection-ipc-'));
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(value.split('').reverse().join('')),
    decryptString: (value) => value.toString().split('').reverse().join(''),
  };
  const events = [];
  const status = {
    helperInstanceId: 'helper-1',
    activeOwnerId: 'local-owner',
    configured: true,
    cloudSessionState: 'absent',
    llmRoute: 'unconfigured',
    webSearchRoute: 'unconfigured',
  };
  const runtime = createLocalConnectionRuntime({
    userDataDir,
    safeStorage,
    whenReady: async () => {},
    onChanged: () => events.push('changed'),
    onRuntimeUnavailable: () => {},
    openExternal: async () => {
      throw new Error('not used');
    },
    logger: {},
    sessionSync: { getConnectionStatus: async () => status },
    helperManager: {
      ensureStarted: async () => {
        throw new Error('not used');
      },
    },
  });
  t.after(() => {
    runtime.dispose();
    fs.rmSync(userDataDir, { recursive: true, force: true });
  });
  const main = sender('file:///app/index.html#/settings', 1);
  const audits = [];
  const security = createIpcSenderSecurity({
    getMainWindow: () => ({ isDestroyed: () => false, webContents: main.sender }),
    isDevRuntime: () => false,
    frontendDevOrigin: null,
    frontendDistIndex: '/app/index.html',
    recordSecurityEvent: (...args) => audits.push(args),
  });
  const handlers = new Map();
  const registrar = createIpcRegistrar(
    { handle: (channel, handler) => handlers.set(channel, handler) },
    security
  );
  registerAiConnectionHandlers({ aiConnection: runtime, security }, registrar);
  const invoke = (channel, input, event = main) => handlers.get(channel)(event, input);
  return {
    userDataDir,
    runtime,
    events,
    audits,
    security,
    main,
    invoke,
    safeStorage,
    store: createConnectionSettingsStore({ userDataDir, safeStorage }),
  };
}

test('IPC normalizes input once, binds it to its provider, and exposes only non-secret state', async (t) => {
  const f = fixture(t);
  assert.deepEqual(
    await f.invoke('aiConnection:update', {
      operation: 'save_preferences',
      preferences: { method: 'api_key', provider: 'anthropic', model: ' custom ' },
    }),
    { ok: true }
  );
  assert.deepEqual(
    await f.invoke('aiConnection:update', {
      operation: 'save_api_key',
      provider: 'anthropic',
      apiKey: ' private-api-key ',
    }),
    { ok: true }
  );
  await f.invoke('aiConnection:update', {
    operation: 'save_web_search_key',
    apiKey: ' private-search-key ',
  });
  assert.equal(f.store.readApiKey('anthropic'), 'private-api-key');
  assert.equal(f.store.readApiKey('openai'), null);
  assert.equal(f.store.readWebSearchKey(), 'private-search-key');
  const result = await f.invoke('aiConnection:getState');
  assert.equal(result.ok, true);
  assert.equal(result.settings.preferences.model, 'custom');
  assert.equal(result.settings.hasSavedApiKey, true);
  assert.equal(result.runtime.ok, true);
  assert.equal(result.runtime.status.activeOwnerId, 'local-owner');
  assert.equal(result.settings.hasSavedWebSearchKey, true);
  assert.equal(JSON.stringify(result).includes('private-'), false);
  assert.equal(JSON.stringify(f.audits).includes('private-'), false);
  assert.deepEqual(f.events, ['changed', 'changed', 'changed']);
  await f.invoke('aiConnection:update', { operation: 'remove_api_key', provider: 'anthropic' });
  assert.equal(f.store.readApiKey('anthropic'), null);
  assert.equal(f.store.readWebSearchKey(), 'private-search-key');
  await f.invoke('aiConnection:update', { operation: 'remove_web_search_key' });
  assert.equal(f.store.readWebSearchKey(), null);
});

test('IPC rejects invalid or extra fields before storage and does not echo their contents', async (t) => {
  const f = fixture(t);
  for (const command of [
    { operation: 'save_api_key', provider: '../../private', apiKey: 'private-input' },
    { operation: 'save_api_key', provider: 'openai', apiKey: '  ' },
    {
      operation: 'save_api_key',
      provider: 'openai',
      apiKey: 'private-input',
      refreshToken: 'private-token',
    },
    {
      operation: 'save_preferences',
      preferences: {
        method: 'api_key',
        provider: 'openai',
        model: 'x',
        endpoint: 'http://private:secret@host',
      },
    },
    { operation: 'unknown', apiKey: 'private-input' },
    {
      operation: 'save_preferences',
      preferences: { method: 'chatgpt', provider: 'openai', model: 'private-invalid-model' },
    },
    {
      operation: 'save_preferences',
      preferences: { method: 'chatgpt', provider: 'openai', model: '' },
    },
    null,
  ])
    assert.deepEqual(await f.invoke('aiConnection:update', command), {
      ok: false,
      error: 'invalid_input',
    });
  assert.equal(f.store.readApiKey('openai'), null);
  assert.deepEqual(f.events, []);
  assert.equal(JSON.stringify(f.audits).includes('private'), false);
});

test('IPC accepts a Codex picker model and keeps API key model names configurable', async (t) => {
  const f = fixture(t);
  for (const preferences of [
    { method: 'chatgpt', provider: 'openai', model: 'gpt-6-astra' },
    { method: 'chatgpt', provider: 'openai', model: 'gpt-5.6-luna' },
    { method: 'api_key', provider: 'openai', model: 'custom-api-model' },
  ]) {
    assert.deepEqual(
      await f.invoke('aiConnection:update', { operation: 'save_preferences', preferences }),
      { ok: true }
    );
    assert.deepEqual(f.store.readPreferences(), preferences);
  }
});

test('only the registered main top frame can read or change device credentials', async (t) => {
  const f = fixture(t);
  const overlay = sender('file:///app/notification.html', 2);
  f.security.registerWindow('overlay', overlay.sender);
  for (const event of [
    overlay,
    sender('https://untrusted.example/?private=secret', 4),
    { sender: f.main.sender, senderFrame: { url: 'file:///app/index.html' } },
    sender('file:///app/index.html', 5),
  ])
    for (const channel of ['aiConnection:getState', 'aiConnection:update']) {
      assert.throws(
        () =>
          f.invoke(
            channel,
            { operation: 'save_api_key', provider: 'openai', apiKey: 'private-key' },
            event
          ),
        { name: 'IpcSenderRejectedError' }
      );
    }
  assert.equal(f.store.readApiKey('openai'), null);
  assert.equal(JSON.stringify(f.audits).includes('private'), false);
});

test('storage and transport failures return fixed codes and never appear as successful mutations', async (t) => {
  const f = fixture(t);
  f.safeStorage.isEncryptionAvailable = () => false;
  assert.deepEqual(
    await f.invoke('aiConnection:update', {
      operation: 'save_api_key',
      provider: 'openai',
      apiKey: 'private-key',
    }),
    { ok: false, error: 'encryption_unavailable' }
  );
  f.runtime.getStatus = async () => {
    throw new Error('private-runtime-token');
  };
  const result = await f.invoke('aiConnection:getState');
  assert.equal(result.ok, true);
  assert.deepEqual(result.runtime, { ok: false, error: 'runtime_unavailable' });
  assert.equal(result.settings.hasSavedApiKey, false);
  assert.equal(JSON.stringify(result).includes('private-'), false);
  f.runtime.removeApiKey = async () => {
    throw new CredentialStorageError('delete_failed');
  };
  assert.deepEqual(
    await f.invoke('aiConnection:update', { operation: 'remove_api_key', provider: 'openai' }),
    { ok: false, error: 'delete_failed' }
  );
  assert.equal(JSON.stringify(f.audits).includes('private-'), false);
  assert.ok(
    f.audits.some(([name, metadata]) => name === 'IPC_MUTATION' && metadata.outcome === 'failed')
  );
});

test('preload invokes only the connection API and change notifications discard IPC event and payload', async () => {
  const calls = [];
  const listeners = new Map();
  const api = createSettingsApi({
    ipcRenderer: {
      invoke: async (...args) => {
        calls.push(args);
      },
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel) => listeners.delete(channel),
    },
  }).aiConnection;
  await api.getState();
  const command = { operation: 'save_web_search_key', apiKey: 'private-key' };
  await api.update(command);
  assert.deepEqual(calls, [['aiConnection:getState'], ['aiConnection:update', command]]);
  const received = [];
  const unsubscribe = api.onChanged((...args) => received.push(args));
  listeners.get('aiConnection:changed')({ privileged: true }, 'private-payload');
  assert.deepEqual(received, [[]]);
  unsubscribe();
  assert.equal(listeners.has('aiConnection:changed'), false);
});

for (const [name, fileName, recovery] of [
  ['ChatGPT', 'chatgpt-oauth.enc.json', { operation: 'disconnect_chatgpt' }],
  [
    'preferences',
    'ai-connection-preferences.enc.json',
    {
      operation: 'save_preferences',
      preferences: { method: 'api_key', provider: 'openai', model: '' },
    },
  ],
  ...['openai', 'anthropic', 'fireworks'].map((provider) => [
    provider,
    `llm-${provider}-api-key.enc.json`,
    { operation: 'remove_api_key', provider },
  ]),
  ['Tavily', 'tavily-api-key.enc.json', { operation: 'remove_web_search_key' }],
]) {
  test(`IPC identifies unreadable ${name} and recovers only that store`, async (t) => {
    const f = fixture(t);
    f.store.savePreferences({
      method: 'api_key',
      provider: recovery.provider ?? 'openai',
      model: 'custom',
    });
    for (const provider of ['openai', 'anthropic', 'fireworks'])
      f.store.saveApiKey(provider, `private-${provider}`);
    f.store.saveWebSearchKey('private-tavily');
    fs.writeFileSync(path.join(f.userDataDir, fileName), 'invalid-private-content');
    const others = fs
      .readdirSync(f.userDataDir)
      .filter((file) => file !== fileName)
      .map((file) => [file, fs.readFileSync(path.join(f.userDataDir, file))]);

    const result = await f.invoke('aiConnection:getState');
    assert.deepEqual(result, { ok: false, error: 'invalid_data', recovery });
    assert.equal(JSON.stringify(result).includes('private'), false);
    assert.deepEqual(await f.invoke('aiConnection:update', result.recovery), { ok: true });
    assert.equal((await f.invoke('aiConnection:getState')).ok, true);
    for (const [file, content] of others)
      assert.deepEqual(fs.readFileSync(path.join(f.userDataDir, file)), content);
    assert.equal(JSON.stringify(f.audits).includes('private'), false);
  });
}

test('read failure identifies its provider; failed deletion preserves that recovery path', async (t) => {
  const f = fixture(t);
  f.store.savePreferences({ method: 'api_key', provider: 'fireworks', model: 'custom' });
  f.store.saveApiKey('fireworks', 'private-fireworks');
  const target = path.join(f.userDataDir, 'llm-fireworks-api-key.enc.json');
  const read = fs.readFileSync;
  t.mock.method(fs, 'readFileSync', (file, ...args) => {
    if (file === target) throw Object.assign(new Error('private-os-message'), { code: 'EACCES' });
    return read(file, ...args);
  });
  const unlink = fs.unlinkSync;
  t.mock.method(fs, 'unlinkSync', (file) => {
    if (file === target) throw Object.assign(new Error('private-os-message'), { code: 'EACCES' });
    return unlink(file);
  });
  const failed = await f.invoke('aiConnection:getState');
  assert.deepEqual(failed, {
    ok: false,
    error: 'read_failed',
    recovery: { operation: 'remove_api_key', provider: 'fireworks' },
  });
  assert.deepEqual(await f.invoke('aiConnection:update', failed.recovery), {
    ok: false,
    error: 'delete_failed',
  });
  assert.deepEqual(await f.invoke('aiConnection:getState'), failed);
  assert.equal(fs.existsSync(target), true);
  assert.equal(JSON.stringify(f.audits).includes('private'), false);
});

test('unavailable encryption does not suggest deleting healthy saved data', async (t) => {
  const f = fixture(t);
  f.store.savePreferences({ method: 'api_key', provider: 'openai', model: 'custom' });
  const target = path.join(f.userDataDir, 'ai-connection-preferences.enc.json');
  const stored = fs.readFileSync(target);
  f.safeStorage.isEncryptionAvailable = () => false;
  assert.deepEqual(await f.invoke('aiConnection:getState'), {
    ok: false,
    error: 'encryption_unavailable',
  });
  assert.deepEqual(fs.readFileSync(target), stored);
});
