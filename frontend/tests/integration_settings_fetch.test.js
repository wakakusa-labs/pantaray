const assert = require('assert');
const { test } = require('node:test');

const { createLocalBackendClient } = require('../electron/dist/localBackend/client.js');
const {
  createIntegrationSettingsFetcher,
} = require('../electron/dist/settings/integrationSettingsFetch.js');

const ACCOUNT = { toolkit: 'gmail', account: 'acct-1' };
const ACCOUNT_PATH = '/v1/agents/users/owner-1/integrations/gmail/accounts/acct-1';

// Through the real client, so its route allowlist is checked too.
async function callBackend(response, run) {
  const originalFetch = globalThis.fetch;
  const sent = [];
  globalThis.fetch = async (url, options) => {
    sent.push(`${options.method} ${new URL(url).pathname}`);
    return response();
  };
  const { requestJson } = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });
  try {
    await run(createIntegrationSettingsFetcher({ requestJson, getUserId: () => 'owner-1' }));
  } finally {
    globalThis.fetch = originalFetch;
  }
  return sent;
}

test('a refused access option comes back as its typed reason', async () => {
  const reason = { source: 'account_grant', capability: 'tool', detail: 'gmail.send' };
  const sent = await callBackend(
    () => Response.json({ detail: { reason } }, { status: 409 }),
    async (fetcher) =>
      assert.deepEqual(await fetcher.setAccountAccess({ ...ACCOUNT, access: 'read_write' }), {
        kind: 'not_allowed',
        reason,
      })
  );
  assert.deepEqual(sent, [`PUT ${ACCOUNT_PATH}/access`]);
});

test('the tool list and both block calls reach the backend', async () => {
  const sent = await callBackend(
    () => Response.json([]),
    async (fetcher) => {
      await fetcher.listAccountTools(ACCOUNT);
      await fetcher.setToolBlocked({ ...ACCOUNT, slug: 'SEND', blocked: true });
      await fetcher.setToolBlocked({ ...ACCOUNT, slug: 'SEND', blocked: false });
    }
  );
  assert.deepEqual(sent, [
    `GET ${ACCOUNT_PATH}/tools`,
    `PUT ${ACCOUNT_PATH}/tools/SEND/block`,
    `DELETE ${ACCOUNT_PATH}/tools/SEND/block`,
  ]);
});

test('connect, reconnect, disconnect and the connection state reach their endpoints', async () => {
  const started = {
    redirect_url: 'https://connect.composio.dev/link/abc',
    expires_at: '2026-10-18T10:00:00+00:00',
    connection_id: 'conn-1',
  };
  const answers = [
    () => Response.json(started),
    () => Response.json(started),
    () => new Response(null, { status: 204 }),
    () => Response.json({ state: 'active' }),
  ];
  const sent = await callBackend(
    () => answers.shift()(),
    async (fetcher) => {
      assert.deepEqual(await fetcher.connect({ toolkit: 'gmail' }), {
        kind: 'started',
        ...started,
      });
      assert.equal((await fetcher.reconnect(ACCOUNT)).kind, 'started');
      assert.equal(await fetcher.disconnect(ACCOUNT), undefined);
      assert.equal(await fetcher.getConnectionState({ connection_id: 'conn-1' }), 'active');
    }
  );
  assert.deepEqual(sent, [
    'POST /v1/agents/users/owner-1/integrations/gmail/connect',
    `POST ${ACCOUNT_PATH}/reconnect`,
    `DELETE ${ACCOUNT_PATH}`,
    'GET /v1/agents/users/owner-1/integrations/connections/conn-1',
  ]);
});

test('a refused connect comes back as its typed reason', async () => {
  const reason = { source: 'connection_method', capability: 'connect', detail: null };
  await callBackend(
    () => Response.json({ detail: { reason } }, { status: 409 }),
    async (fetcher) =>
      assert.deepEqual(await fetcher.connect({ toolkit: 'gmail' }), {
        kind: 'not_allowed',
        reason,
      })
  );
});

test('a path segment with a slash or no characters is refused before it reaches main', () => {
  const {
    IntegrationAccountRefSchema,
    IntegrationConnectionRefSchema,
  } = require('../electron/dist/settings/integrationSettingsFetch.js');
  for (const ref of [
    { toolkit: 'gmail/../x', account: 'a' },
    { toolkit: 'gmail', account: '' },
  ]) {
    assert.equal(IntegrationAccountRefSchema.safeParse(ref).success, false);
  }
  assert.equal(IntegrationConnectionRefSchema.safeParse({ connection_id: 'a/b' }).success, false);
  assert.equal(IntegrationConnectionRefSchema.safeParse({ connection_id: 'conn-1' }).success, true);
});
