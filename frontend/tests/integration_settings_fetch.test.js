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
