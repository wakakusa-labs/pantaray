const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createLocalBackendClient } = require('../electron/dist/localBackend/client');
const { postWelcomeSuggestion } = require('../electron/dist/main_runtime/welcomeSuggestion');

// Goes through the real client, so the route allowlist and the token gate are exercised too.
function withFetch(t, respond) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url: String(url), method: options.method, body: options.body });
    return respond(requests.length);
  };
  t.after(() => { globalThis.fetch = originalFetch; });
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });
  return { requests, requestJson: client.requestJson };
}

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

test('the welcome reaches the runtime with its text', async t => {
  const f = withFetch(t, () => json(200, { created: true }));
  await postWelcomeSuggestion({ requestJson: f.requestJson, userId: 'user 1', answer: 'Hello' });
  assert.deepEqual(f.requests.map(r => [r.method, new URL(r.url).pathname, JSON.parse(r.body)]), [
    ['POST', '/v1/agents/users/user%201/suggestions/welcome', { answer: 'Hello' }],
  ]);
});

test('a welcome refused until the session is open gets another chance', async t => {
  const waits = [];
  const noSession = { detail: { error_code: 'WELCOME_NO_SESSION' } };
  const f = withFetch(t, n => (n < 3 ? json(503, noSession) : json(200, { created: true })));
  await postWelcomeSuggestion({
    requestJson: f.requestJson, userId: 'user-1', answer: 'Hello',
    retryDelaysMs: [1, 2, 3], sleep: async ms => { waits.push(ms); },
  });
  assert.equal(f.requests.length, 3);
  assert.deepEqual(waits, [1, 2]);
});

test('retries stop after the last delay and report the failure', async t => {
  const f = withFetch(t, () => json(503, { detail: 'busy' }));
  await assert.rejects(postWelcomeSuggestion({
    requestJson: f.requestJson, userId: 'user-1', answer: 'Hello',
    retryDelaysMs: [1, 2], sleep: async () => undefined,
  }));
  assert.equal(f.requests.length, 3);
});

test('a refusal is not retried: it would say the same thing again', async t => {
  const f = withFetch(t, () => json(403, { detail: 'owner mismatch' }));
  await assert.rejects(postWelcomeSuggestion({
    requestJson: f.requestJson, userId: 'user-1', answer: 'Hello',
    retryDelaysMs: [1, 2], sleep: async () => undefined,
  }));
  assert.equal(f.requests.length, 1);
});
