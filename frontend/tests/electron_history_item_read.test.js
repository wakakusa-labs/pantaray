const assert = require('node:assert/strict');
const { test } = require('node:test');

const { createLocalBackendClient } = require('../electron/dist/localBackend/client.js');
const { createHistoryItemReader } = require('../electron/dist/history/historyItemRead.js');

const ROW = {
  kind: 'conversation',
  action_id: 'action-1',
  title: 'Q3 report',
  updated_at: '2026-10-11T00:00:00.000Z',
  status: 'idle',
  latest_completion_event_id: null,
  status_line: null,
  artifact_names: [],
};

function readerOver(respond) {
  const requested = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    requested.push({ url: String(url), method: options.method });
    return respond(String(url));
  };
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });
  return {
    read: createHistoryItemReader({ requestJson: client.requestJson }),
    requested,
    restore: () => {
      globalThis.fetch = originalFetch;
    },
  };
}

test('a row reaches the reader through the real client allowlist, the id as one path segment', async () => {
  const { read, requested, restore } = readerOver(() => new Response(JSON.stringify(ROW)));
  try {
    assert.deepEqual(await read({ kind: 'suggestion', id: 'a/b?c' }), ROW);
    assert.deepEqual(requested, [
      {
        url: 'http://127.0.0.1:61131/api/agent/history/items/suggestion/a%2Fb%3Fc',
        method: 'GET',
      },
    ]);
  } finally {
    restore();
  }
});

test('404 is no row; any other failure or shape rejects', async () => {
  const notFound = readerOver(
    () => new Response(JSON.stringify({ detail: 'history item not found' }), { status: 404 })
  );
  try {
    assert.equal(await notFound.read({ kind: 'conversation', id: 'gone' }), null);
  } finally {
    notFound.restore();
  }
  const failing = readerOver(() => new Response('{"detail":"boom"}', { status: 500 }));
  try {
    await assert.rejects(() => failing.read({ kind: 'conversation', id: 'action-1' }));
  } finally {
    failing.restore();
  }
  const malformed = readerOver(() => new Response(JSON.stringify({ ...ROW, title: ' ' })));
  try {
    await assert.rejects(() => malformed.read({ kind: 'conversation', id: 'action-1' }));
  } finally {
    malformed.restore();
  }
});
