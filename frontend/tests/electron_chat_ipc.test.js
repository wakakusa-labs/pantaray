const assert = require('assert');
const { test } = require('node:test');

const { createChatFetcher } = require('../electron/dist/chat/chatFetch.js');
const { registerChatHandlers } = require('../electron/dist/ipc/handlers/chat.js');
const { registerOverlayHandlers } = require('../electron/dist/ipc/handlers/overlay.js');
const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory.js');
const { createLocalBackendClient } = require('../electron/dist/localBackend/client.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');

const USER_ITEM = {
  sequence: 7,
  item_id: 'item-7',
  created_at: '2026-10-08T01:02:03.456Z',
  content: {
    kind: 'user_message',
    text: 'Draft the proposal',
    quote_item_id: null,
    images: [{ kind: 'image', storage_path: 'user 1/2026-10-08/a.png' }],
    files: [],
  },
};

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** The chat fetcher over the real loopback client, with `fetch` answered by `respond`. */
async function withChatBackend(respond, run) {
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
      createChatFetcher({ requestJson: client.requestJson, getUserId: () => 'user 1' }),
      requests
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('sending posts the message to the user chat with the local API token', async () => {
  const request = {
    message_id: 'msg-1',
    text: 'Draft the proposal',
    quote_item_id: null,
    images: USER_ITEM.content.images,
    files: [],
  };
  await withChatBackend(
    () => jsonResponse(200, USER_ITEM),
    async (chat, requests) => {
      assert.deepEqual(await chat.sendMessage(request), { kind: 'sent', item: USER_ITEM });
      assert.equal(
        requests[0].url,
        'http://127.0.0.1:61131/v1/agents/users/user%201/chat/messages'
      );
      assert.equal(requests[0].options.method, 'POST');
      assert.equal(requests[0].options.headers.Authorization, 'Bearer local-api-token');
      assert.deepEqual(JSON.parse(requests[0].options.body), request);
    }
  );
});

test('listing reads one newest-first page by cursor', async () => {
  // A failure of Pantaray's own is an item the page must still carry.
  const failure = {
    ...USER_ITEM,
    sequence: 8,
    item_id: 'item-8',
    content: { kind: 'turn_failure', reason: 'internal' },
  };
  const page = { items: [failure, USER_ITEM], next_cursor: 7 };
  await withChatBackend(
    () => jsonResponse(200, page),
    async (chat, requests) => {
      assert.deepEqual(await chat.listItems({ before: null, limit: 50 }), page);
      await chat.listItems({ before: 7, limit: 50 });
      assert.deepEqual(
        requests.map(({ url, options }) => [options.method, url, options.headers.Authorization]),
        [
          [
            'GET',
            'http://127.0.0.1:61131/v1/agents/users/user%201/chat/items?limit=50',
            'Bearer local-api-token',
          ],
          [
            'GET',
            'http://127.0.0.1:61131/v1/agents/users/user%201/chat/items?before=7&limit=50',
            'Bearer local-api-token',
          ],
        ]
      );
    }
  );
});

test('a 400 names the rejected field; other failures and off-contract items throw', async () => {
  const request = { message_id: 'm', text: 't', quote_item_id: 'gone', images: [], files: [] };
  const responses = [
    jsonResponse(400, { type: 'ChatMessageRejected', field: 'quote_item_id' }),
    jsonResponse(400, { detail: 'bad' }),
    jsonResponse(409, { type: 'MessageIdentityConflict' }),
    jsonResponse(200, { ...USER_ITEM, content: { ...USER_ITEM.content, kind: 'note' } }),
  ];
  await withChatBackend(
    (count) => responses[count - 1],
    async (chat) => {
      assert.deepEqual(await chat.sendMessage(request), {
        kind: 'rejected',
        field: 'quote_item_id',
      });
      await assert.rejects(chat.sendMessage(request), (error) => error.status === 400);
      await assert.rejects(chat.sendMessage(request), (error) => error.status === 409);
      await assert.rejects(chat.sendMessage(request), /chat item response is invalid/);
    }
  );
});

test('a retry starts the failed turn again, or reports that another turn has ended since', async () => {
  const responses = [
    new Response(null, { status: 204 }),
    jsonResponse(409, { type: 'ChatTurnRetryStale' }),
    jsonResponse(500, { detail: 'down' }),
  ];
  await withChatBackend(
    (count) => responses[count - 1],
    async (chat, requests) => {
      const request = { failure_item_id: 'item-8' };
      assert.deepEqual(await chat.retryTurn(request), { kind: 'started' });
      assert.deepEqual(await chat.retryTurn(request), { kind: 'stale' });
      await assert.rejects(chat.retryTurn(request), (error) => error.status === 500);
      assert.deepEqual(
        [requests[0].options.method, requests[0].url, JSON.parse(requests[0].options.body)],
        ['POST', 'http://127.0.0.1:61131/v1/agents/users/user%201/chat/turns/retry', request]
      );
    }
  );
});

function chatHandlers() {
  const handlers = new Map();
  const calls = [];
  registerChatHandlers(
    {
      chat: {
        sendMessage: async (request) => {
          calls.push(['send', request]);
          return { kind: 'sent', item: USER_ITEM };
        },
        listItems: async (request) => {
          calls.push(['list', request]);
          return { items: [], next_cursor: null };
        },
        retryTurn: async (request) => {
          calls.push(['retry', request]);
          return { kind: 'started' };
        },
        getTurnState: () => ({ running: true }),
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  return {
    calls,
    invoke: (channel, payload) => handlers.get(channel)({ sender: { id: 1 } }, payload),
  };
}

test('chat IPC validates the renderer payload before anything reaches the backend', async () => {
  const ipc = chatHandlers();
  const valid = {
    message_id: 'msg-1',
    text: '  hello  ',
    quote_item_id: null,
    images: [],
    files: [],
  };

  await ipc.invoke('chat:sendMessage', valid);
  await ipc.invoke('chat:listItems', { before: null, limit: 200 });
  assert.deepEqual(ipc.calls, [
    ['send', { ...valid, text: 'hello' }],
    ['list', { before: null, limit: 200 }],
  ]);

  const tooManyImages = Array.from({ length: 33 }, () => ({
    kind: 'image',
    storage_path: 'p.png',
  }));
  for (const payload of [
    { ...valid, text: '   ' },
    { ...valid, message_id: '' },
    { ...valid, images: tooManyImages },
    { ...valid, files: [{ attachment_id: 'not-a-uuid', name: 'a.pdf', byte_size: 1 }] },
    { ...valid, user_id: 'someone-else' },
  ]) {
    await assert.rejects(ipc.invoke('chat:sendMessage', payload), IpcValidationError);
  }
  for (const payload of [{ before: 0, limit: 10 }, { before: null, limit: 201 }, { limit: 10 }]) {
    await assert.rejects(ipc.invoke('chat:listItems', payload), IpcValidationError);
  }
  await ipc.invoke('chat:retryTurn', { failure_item_id: 'item-8' });
  for (const payload of [{}, { failure_item_id: '' }, { failure_item_id: 'f', extra: 1 }]) {
    await assert.rejects(ipc.invoke('chat:retryTurn', payload), IpcValidationError);
  }
  assert.deepEqual(ipc.calls.at(-1), ['retry', { failure_item_id: 'item-8' }]);
  assert.equal(ipc.calls.length, 3);
  assert.deepEqual(await ipc.invoke('chat:getTurnState'), { running: true });
});

test('overlay:showChat validates the Action id and asks main to show it', async () => {
  const handlers = new Map();
  const shown = [];
  registerOverlayHandlers(
    {
      windows: { showChat: (actionId) => shown.push(actionId) },
      overlay: { createNotificationIpcHandlers: () => ({}) },
      actions: {},
    },
    { handle: (channel, handler) => handlers.set(channel, handler), on: () => {} }
  );
  const invoke = (payload) => handlers.get('overlay:showChat')({ sender: { id: 2 } }, payload);

  await invoke({ actionId: 'act-1' });
  for (const payload of [
    { actionId: ' act-1' },
    { actionId: '' },
    { actionId: 'a', extra: 1 },
    'act-1',
  ]) {
    await assert.rejects(async () => invoke(payload), IpcValidationError);
  }
  assert.deepEqual(shown, ['act-1']);
});

test('showChat brings the main window forward and tells it which Action to show', () => {
  const steps = [];
  let mainWindow = {
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => steps.push('restore'),
    isVisible: () => false,
    show: () => steps.push('show'),
    focus: () => steps.push('focus'),
    webContents: { send: (channel, payload) => steps.push([channel, payload]) },
  };
  const context = buildMainContext({
    getMainWindow: () => mainWindow,
    isDevRuntime: () => false,
    frontendDistIndex: '/tmp/index.html',
    recordSecurityEvent: () => {},
  });

  context.windows.showChat('act-1');
  assert.deepEqual(steps, [
    'restore',
    'show',
    'focus',
    ['history:showChat', { actionId: 'act-1' }],
  ]);

  mainWindow = null;
  context.windows.showChat('act-2');
  assert.equal(steps.length, 4);
});
