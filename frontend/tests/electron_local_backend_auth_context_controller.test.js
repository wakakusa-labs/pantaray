const assert = require('assert');
const { test } = require('node:test');

const {
  createLocalBackendAuthContextController,
} = require('../electron/dist/auth/localBackendAuthContextController.js');

const getConnections = async () => ({
  llmConnection: null,
  webSearchCredential: null,
  integrationCredential: null,
});
const onRuntimeUnavailable = () => {};

function createStubs(overrides = {}) {
  const calls = [];
  let helperInstanceId = overrides.helperInstanceId ?? 'helper-a';
  let generation = 0;
  return {
    calls,
    getConnections,
    onRuntimeUnavailable: overrides.onRuntimeUnavailable ?? onRuntimeUnavailable,
    restartHelper(nextInstanceId) {
      helperInstanceId = nextInstanceId;
    },
    sessionSync: {
      setLlmConnection: async () => {
        calls.push({ method: 'setLlmConnection' });
        return 'unconfigured';
      },
      setWebSearchCredential: async () => {
        calls.push({ method: 'setWebSearchCredential' });
        return 'unconfigured';
      },
      setIntegrationCredential: async () => {
        calls.push({ method: 'setIntegrationCredential' });
      },
      configure: async (cloudSession) => {
        calls.push({ method: 'configure', cloudSession });
        generation += 1;
        return { cloudSessionState: cloudSession.state, credentialGeneration: generation, helperInstanceId };
      },
      setCloudSession: async (accountUserId, accessToken, sessionVersion) => {
        calls.push({ method: 'setCloudSession', accountUserId, accessToken, sessionVersion });
        if (overrides.setCloudSession) await overrides.setCloudSession();
        generation += 1;
        return { cloudSessionState: 'present', credentialGeneration: generation, helperInstanceId };
      },
      clearCloudSession: async (params) => {
        calls.push({ method: 'clearCloudSession', ...params });
        if (overrides.clearCloudSession) await overrides.clearCloudSession(params);
        if (overrides.staleClear) {
          return { cloudSessionState: 'present', stale: true };
        }
        return { cloudSessionState: params.reason === 'expired' ? 'expired' : 'absent', stale: false };
      },
    },
    helperManager: {
      ensureStarted: async () => {
        calls.push({ method: 'ensureStarted' });
        return { helperInstanceId };
      },
      terminateCurrentHelper: async () => {
        calls.push({ method: 'terminateCurrentHelper' });
        if (overrides.terminateCurrentHelper) await overrides.terminateCurrentHelper();
      },
    },
  };
}

const PRESENT = { token: 'token-1', userId: 'user-1', sessionVersion: '1', expiredIdentity: null };
const ABSENT = { token: null, userId: null, sessionVersion: null, expiredIdentity: null };

test('the first change after a helper starts configures it, later changes send deltas', async () => {
  const { calls, sessionSync, helperManager } = createStubs();
  const controller = createLocalBackendAuthContextController({ sessionSync, helperManager, getConnections, onRuntimeUnavailable });

  await controller.applyAuthContextChange(ABSENT);
  await controller.applyAuthContextChange(PRESENT);
  await controller.applyAuthContextChange({ ...PRESENT, token: 'token-2', userId: 'user-2', sessionVersion: '2' });
  await controller.applyAuthContextChange(ABSENT);

  assert.deepStrictEqual(calls, [
    { method: 'ensureStarted' },
    { method: 'configure', cloudSession: { state: 'absent' } },
    { method: 'ensureStarted' },
    { method: 'setCloudSession', accountUserId: 'user-1', accessToken: 'token-1', sessionVersion: '1' },
    { method: 'ensureStarted' },
    { method: 'setCloudSession', accountUserId: 'user-2', accessToken: 'token-2', sessionVersion: '2' },
    { method: 'ensureStarted' },
    {
      method: 'clearCloudSession',
      reason: 'signed_out',
      accountUserId: 'user-2',
      sessionVersion: '2',
      credentialGeneration: 3,
      helperInstanceId: 'helper-a',
    },
  ]);
});

test('a restarted helper is configured with the current session instead of a delta', async () => {
  const stubs = createStubs();
  const controller = createLocalBackendAuthContextController(stubs);

  await controller.applyAuthContextChange(PRESENT);
  stubs.restartHelper('helper-b');
  await controller.applyAuthContextChange({ ...PRESENT, token: 'token-1b' });
  await controller.applyAuthContextChange(ABSENT);

  assert.deepStrictEqual(stubs.calls.filter((call) => call.method !== 'ensureStarted'), [
    {
      method: 'configure',
      cloudSession: { state: 'present', accountUserId: 'user-1', accessToken: 'token-1', sessionVersion: '1' },
    },
    {
      method: 'configure',
      cloudSession: { state: 'present', accountUserId: 'user-1', accessToken: 'token-1b', sessionVersion: '1' },
    },
    {
      method: 'clearCloudSession',
      reason: 'signed_out',
      accountUserId: 'user-1',
      sessionVersion: '1',
      credentialGeneration: 2,
      helperInstanceId: 'helper-b',
    },
  ]);
});

test('expiry keeps the identity so the later sign-out clears it with the same fence', async () => {
  const { calls, sessionSync, helperManager } = createStubs();
  const controller = createLocalBackendAuthContextController({ sessionSync, helperManager, getConnections, onRuntimeUnavailable });

  await controller.applyAuthContextChange(PRESENT);
  await controller.applyAuthContextChange({
    ...ABSENT,
    expiredIdentity: { userId: 'user-1', sessionVersion: '1' },
  });
  await controller.applyAuthContextChange(ABSENT);

  const clears = calls.filter((call) => call.method === 'clearCloudSession');
  assert.deepStrictEqual(
    clears.map((call) => [call.reason, call.accountUserId, call.credentialGeneration]),
    [
      ['expired', 'user-1', 1],
      ['signed_out', 'user-1', 1],
    ]
  );
});

test('a startup with an expired stored session configures the helper as expired', async () => {
  const { calls, sessionSync, helperManager } = createStubs();
  const controller = createLocalBackendAuthContextController({ sessionSync, helperManager, getConnections, onRuntimeUnavailable });

  await controller.applyAuthContextChange({
    ...ABSENT,
    expiredIdentity: { userId: 'user-1', sessionVersion: '7' },
  });

  assert.deepStrictEqual(calls[1], {
    method: 'configure',
    cloudSession: { state: 'expired', accountUserId: 'user-1', sessionVersion: '7' },
  });
});

test('sign-out falls back to helper-side termination and reconfigures the next helper', async () => {
  const stubs = createStubs({
    clearCloudSession: async () => {
      throw new Error('clear failed');
    },
  });
  const controller = createLocalBackendAuthContextController(stubs);

  await controller.applyAuthContextChange(PRESENT);
  const result = await controller.clearForSignOut();
  stubs.restartHelper('helper-b');
  await controller.applyAuthContextChange(ABSENT);

  assert.deepStrictEqual(result, { contained: true });
  assert.deepStrictEqual(
    stubs.calls.map((call) => call.method),
    ['ensureStarted', 'configure', 'clearCloudSession', 'terminateCurrentHelper', 'ensureStarted', 'configure']
  );
});

test('sign-out reports containment failure when clear and helper termination both fail', async () => {
  const stubs = createStubs({
    clearCloudSession: async () => {
      throw new Error('clear failed');
    },
    terminateCurrentHelper: async () => {
      throw new Error('terminate failed');
    },
  });
  const controller = createLocalBackendAuthContextController(stubs);

  await controller.applyAuthContextChange(PRESENT);
  const result = await controller.clearForSignOut();

  assert.deepStrictEqual(result, { contained: false, reason: 'terminate failed' });
  await assert.rejects(() => controller.applyAuthContextChange(ABSENT), /terminate failed/);
});

test('sign-out with nothing installed in the helper is a no-op', async () => {
  const { calls, sessionSync, helperManager } = createStubs();
  const controller = createLocalBackendAuthContextController({ sessionSync, helperManager, getConnections, onRuntimeUnavailable });

  assert.deepStrictEqual(await controller.clearForSignOut(), { contained: true });
  assert.deepStrictEqual(calls, []);
});

test('signing out of an expired session clears the account the helper still holds', async () => {
  const { calls, sessionSync, helperManager } = createStubs();
  const controller = createLocalBackendAuthContextController({ sessionSync, helperManager, getConnections, onRuntimeUnavailable });

  await controller.applyAuthContextChange(PRESENT);
  await controller.applyAuthContextChange({
    ...ABSENT,
    expiredIdentity: { userId: 'user-1', sessionVersion: '1' },
  });

  assert.deepStrictEqual(await controller.clearForSignOut(), { contained: true });
  assert.deepStrictEqual(calls.at(-1), {
    method: 'clearCloudSession',
    reason: 'signed_out',
    accountUserId: 'user-1',
    sessionVersion: '1',
    credentialGeneration: 1,
    helperInstanceId: 'helper-a',
  });
});

test('a clear the helper rejects as stale terminates the helper instead of claiming containment', async () => {
  const stubs = createStubs({ staleClear: true });
  const controller = createLocalBackendAuthContextController(stubs);

  await controller.applyAuthContextChange(PRESENT);
  const result = await controller.clearForSignOut();

  assert.deepStrictEqual(result, { contained: true });
  assert.deepStrictEqual(
    stubs.calls.map((call) => call.method),
    ['ensureStarted', 'configure', 'clearCloudSession', 'terminateCurrentHelper']
  );
});

test('a set the helper may have applied terminates it so the next sign-out cannot skip the clear', async () => {
  const stubs = createStubs({
    setCloudSession: async () => {
      throw new Error('response lost');
    },
  });
  const controller = createLocalBackendAuthContextController(stubs);

  await controller.applyAuthContextChange(ABSENT);
  await assert.rejects(() => controller.applyAuthContextChange(PRESENT), /response lost/);

  assert.deepStrictEqual(
    stubs.calls.map((call) => call.method),
    ['ensureStarted', 'configure', 'ensureStarted', 'setCloudSession', 'terminateCurrentHelper']
  );

  // The discarded helper is replaced by a configured one on the next change.
  stubs.restartHelper('helper-b');
  await controller.applyAuthContextChange(PRESENT);
  assert.deepStrictEqual(stubs.calls.at(-1), {
    method: 'configure',
    cloudSession: {
      state: 'present',
      accountUserId: 'user-1',
      accessToken: 'token-1',
      sessionVersion: '1',
    },
  });
});

test('a helper that cannot be terminated stays uncontained so sign-out fails closed', async () => {
  let terminateFails = true;
  const stubs = createStubs({
    setCloudSession: async () => {
      throw new Error('response lost');
    },
    terminateCurrentHelper: async () => {
      if (terminateFails) throw new Error('terminate failed');
    },
  });
  const controller = createLocalBackendAuthContextController(stubs);

  await controller.applyAuthContextChange(ABSENT);
  await assert.rejects(() => controller.applyAuthContextChange(PRESENT), /response lost/);

  // The helper may hold the credential, so nothing may report containment.
  assert.deepStrictEqual(await controller.clearForSignOut(), {
    contained: false,
    reason: 'terminate failed',
  });
  assert.deepStrictEqual(await controller.clearForSignOut(), {
    contained: false,
    reason: 'terminate failed',
  });
  await assert.rejects(
    () => controller.applyAuthContextChange(ABSENT),
    /Failed to contain the local backend helper/
  );

  terminateFails = false;
  assert.deepStrictEqual(await controller.clearForSignOut(), { contained: true });
});

test('runtime containment stops the helper without signing out its cloud identity', async () => {
  let unavailable = 0;
  const stubs = createStubs({ onRuntimeUnavailable: () => { unavailable += 1; } });
  const controller = createLocalBackendAuthContextController(stubs);
  await controller.applyAuthContextChange(PRESENT);

  assert.deepStrictEqual(await controller.containRuntime(), { contained: true });
  assert.equal(unavailable, 1);
  assert.equal(stubs.calls.some((call) => call.method === 'clearCloudSession'), false);
  stubs.restartHelper('helper-b');
  await controller.updateConnections(() => {});

  assert.deepStrictEqual(stubs.calls.at(-1), {
    method: 'configure',
    cloudSession: { state: 'present', accountUserId: 'user-1', accessToken: 'token-1', sessionVersion: '1' },
  });
});

test('sign-out after a lost configure response cannot reinstall the old cloud token', async () => {
  const stubs = createStubs();
  const configure = stubs.sessionSync.configure;
  let fail = true;
  stubs.sessionSync.configure = async (snapshot) => {
    const result = await configure(snapshot);
    if (fail) throw new Error('response lost');
    return result;
  };
  const controller = createLocalBackendAuthContextController(stubs);
  await assert.rejects(controller.applyAuthContextChange(PRESENT), /response lost/);
  assert.deepStrictEqual(await controller.clearForSignOut(), { contained: true });
  fail = false;

  await controller.updateConnections(() => {});

  assert.deepStrictEqual(stubs.calls.at(-1), {
    method: 'configure',
    cloudSession: { state: 'absent' },
  });
});

test('failed runtime containment prevents reusing the helper until it is stopped', async () => {
  let fail = true;
  const stubs = createStubs({
    terminateCurrentHelper: async () => {
      if (fail) throw new Error('stop failed');
    },
  });
  const controller = createLocalBackendAuthContextController(stubs);
  await controller.applyAuthContextChange(PRESENT);
  assert.equal((await controller.containRuntime()).contained, false);
  stubs.calls.length = 0;

  await assert.rejects(controller.updateConnections(() => {}), /Failed to contain/);
  assert.deepStrictEqual(stubs.calls, [{ method: 'terminateCurrentHelper' }]);
  fail = false;
  await controller.updateConnections(() => {});
  assert.equal(stubs.calls.at(-1).method, 'configure');
  assert.equal(stubs.calls.at(-1).cloudSession.state, 'present');
});
