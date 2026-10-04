const assert = require('assert');
const { test } = require('node:test');

const { createOrchestrationApi } = require('../electron/preload/orchestration_api');
const { createOverlayApi } = require('../electron/preload/overlay_api');
const { createPreloadApi } = require('../electron/preload/create_preload_api');
const { createActionsApi } = require('../electron/preload/actions_api');

function createIpcRenderer() {
  const listeners = new Map();
  const sent = [];
  const invoked = [];
  return {
    invoked,
    sent,
    on(channel, listener) {
      const current = listeners.get(channel) || [];
      current.push(listener);
      listeners.set(channel, current);
    },
    removeListener(channel, listener) {
      listeners.set(
        channel,
        (listeners.get(channel) || []).filter((candidate) => candidate !== listener)
      );
    },
    emit(channel, payload) {
      for (const listener of listeners.get(channel) || []) listener({}, payload);
    },
    send(...args) {
      sent.push(args);
    },
    invoke: async (...args) => {
      invoked.push(args);
      return args;
    },
  };
}

function actionUpdate(actionId, status) {
  return {
    kind: 'action_updated',
    snapshot: { actionId, page: { action: { action_id: actionId, status } }, lifecycle: null },
  };
}

const ipcPolicy = {
  assertValidInvokeChannel: () => {},
  isValidReceiveChannel: () => true,
  isValidSendChannel: () => true,
};

test('Action preload API forwards typed invokes and retains the latest update per Action', async () => {
  const ipcRenderer = createIpcRenderer();
  const api = createPreloadApi({
    initialUiLanguage: 'ja',
    ipcPolicy,
    ipcRenderer,
    logError: () => {},
    processRef: { platform: 'darwin', env: { NODE_ENV: 'test' } },
    windowRef: { location: { pathname: '/index.html' } },
  });
  const submit = { target: { kind: 'new' }, message: { message_id: 'm1' } };
  const page = { actionId: 'a1', cursor: null, limit: 25 };
  const output = { actionId: 'a1', stepId: 's1', cursor: null, limitBytes: 16_384 };

  await api.actions.submitMessage(submit);
  await api.actions.readConversationPage(page);
  await api.actions.readToolOutputPage(output);
  const file = { bytes: new ArrayBuffer(1), name: 'a.pdf' };
  await api.actions.attachFile(file);
  await api.actions.discardAttachment({ attachmentId: 'f1' });
  const viewed = { subjectId: 'u1', actionId: 'a1', completionEventId: 'c1' };
  await api.history.markCompletionViewed(viewed);
  await api.history.openNewConversation();
  await api.history.openConversation({ actionId: 'a1' });
  await api.history.deleteItem({ kind: 'conversation', id: 'a1' });

  assert.deepEqual(ipcRenderer.invoked, [
    ['action:submitMessage', submit],
    ['action:readConversationPage', page],
    ['action:readToolOutputPage', output],
    ['action:attachFile', file],
    ['action:discardAttachment', { attachmentId: 'f1' }],
    ['history:markCompletionViewed', viewed],
    ['history:openNewConversation'],
    ['history:openConversation', { actionId: 'a1' }],
    ['history:deleteItem', { kind: 'conversation', id: 'a1' }],
  ]);

  ipcRenderer.emit('action:conversationUpdated', actionUpdate('a1', 'queued'));
  ipcRenderer.emit('action:conversationUpdated', actionUpdate('a2', 'processing'));
  ipcRenderer.emit('action:conversationUpdated', actionUpdate('a1', 'processing'));
  const received = [];
  api.actions.onConversationUpdated((value) => received.push(value));
  // Each Action's latest update, oldest change first, so a late subscriber sees every Action.
  assert.deepEqual(received, [actionUpdate('a2', 'processing'), actionUpdate('a1', 'processing')]);
  ipcRenderer.emit('action:conversationUpdated', { kind: 'reset' });
  api.actions.onConversationUpdated(() => assert.fail('reset update was retained'));
});

test('Action preload keeps running Actions and only the latest finished ones for replay', () => {
  const ipcRenderer = createIpcRenderer();
  const { actions } = createActionsApi({ ipcRenderer });
  ipcRenderer.emit('action:conversationUpdated', actionUpdate('running', 'processing'));
  for (let index = 0; index < 50; index += 1) {
    ipcRenderer.emit('action:conversationUpdated', actionUpdate(`done-${index}`, 'processing'));
    ipcRenderer.emit('action:conversationUpdated', actionUpdate(`done-${index}`, 'success'));
  }
  // The completion event alone already ends a run whose page read is still pending.
  const completed = {
    kind: 'action_updated',
    snapshot: {
      actionId: 'fast',
      page: null,
      lifecycle: { processId: 'p-fast', status: 'error' },
    },
  };
  ipcRenderer.emit('action:conversationUpdated', completed);

  const replayed = [];
  actions.onConversationUpdated((update) => replayed.push(update));
  assert.deepEqual(
    replayed.map((update) => update.snapshot.actionId),
    ['running', ...Array.from({ length: 19 }, (_, index) => `done-${index + 31}`), 'fast']
  );
  // An Overlay that subscribes after its fast run finished still gets the terminal update.
  assert.deepEqual(replayed.at(-1), completed);
});

for (const hasFreshUpdate of [false, true]) {
  test(`Action preload delivers an early reset before a fresh update: ${hasFreshUpdate}`, () => {
    const ipcRenderer = createIpcRenderer();
    const { actions } = createActionsApi({ ipcRenderer });
    const reset = { kind: 'reset' };
    const latest = { kind: 'action_updated', snapshot: { actionId: 'new-account-action' } };
    ipcRenderer.emit('action:conversationUpdated', {
      kind: 'action_updated',
      snapshot: { actionId: 'old-account-action' },
    });
    ipcRenderer.emit('action:conversationUpdated', reset);
    if (hasFreshUpdate) ipcRenderer.emit('action:conversationUpdated', latest);

    const received = [];
    const unsubscribe = actions.onConversationUpdated((update) => received.push(update));
    assert.deepEqual(received, hasFreshUpdate ? [reset, latest] : [reset]);
    unsubscribe();

    // Re-subscribing must not clear positions created after the delivered reset.
    const replayed = [];
    actions.onConversationUpdated((update) => replayed.push(update));
    assert.deepEqual(replayed, hasFreshUpdate ? [latest] : []);
  });
}

test('overlay API retains snapshots until the renderer subscribes', () => {
  const ipcRenderer = createIpcRenderer();
  const api = createOverlayApi({ ipcRenderer, ipcPolicy, logError: () => {} });
  const payload = { snapshot: { suggestionId: 'S1' } };
  ipcRenderer.emit('overlay:snapshot', payload);
  const received = [];

  const unsubscribe = api.agentOverlay.onSnapshot((value) => received.push(value));
  unsubscribe();
  ipcRenderer.emit('overlay:snapshot', { snapshot: { suggestionId: 'S2' } });

  assert.deepEqual(received, [payload]);
});

test('notification orchestration flushes buffered events in order and deduplicates event IDs', () => {
  const ipcRenderer = createIpcRenderer();
  const api = createOrchestrationApi({
    ipcRenderer,
    windowRef: { location: { pathname: '/notification.html' } },
    logError: () => {},
  });
  ipcRenderer.emit('ws:event', { event_id: 'before-subscribe-1' });
  ipcRenderer.emit('ws:event', { event_id: 'before-subscribe-2' });
  const received = [];
  api.orchestration.onEvent((payload) => received.push(payload));

  ipcRenderer.emit('ws:event', { event_id: 'E1', value: 1 });
  ipcRenderer.emit('ws:event', { event_id: 'E1', value: 2 });

  assert.deepEqual(received, [
    { event_id: 'before-subscribe-1' },
    { event_id: 'before-subscribe-2' },
    { event_id: 'E1', value: 1 },
  ]);
});

test('notification orchestration drops the oldest event above the 200-event buffer limit', () => {
  const ipcRenderer = createIpcRenderer();
  const api = createOrchestrationApi({
    ipcRenderer,
    windowRef: { location: { pathname: '/notification.html' } },
    logError: () => {},
  });
  for (let index = 1; index <= 201; index += 1) {
    ipcRenderer.emit('ws:event', { event_id: `E${index}` });
  }
  const received = [];

  api.orchestration.onEvent((payload) => received.push(payload));

  assert.equal(received.length, 200);
  assert.equal(received[0].event_id, 'E2');
  assert.equal(received.at(-1).event_id, 'E201');
});

test('notification orchestration consumes buffered events across StrictMode resubscription', () => {
  const ipcRenderer = createIpcRenderer();
  const api = createOrchestrationApi({
    ipcRenderer,
    windowRef: { location: { pathname: '/notification.html' } },
    logError: () => {},
  });
  ipcRenderer.emit('ws:event', { event_id: 'before-first-subscribe' });
  const firstSetup = [];
  const unsubscribeFirst = api.orchestration.onEvent((payload) => firstSetup.push(payload));

  unsubscribeFirst();
  const secondSetup = [];
  const unsubscribeSecond = api.orchestration.onEvent((payload) => secondSetup.push(payload));
  ipcRenderer.emit('ws:event', { event_id: 'during-second-setup' });
  unsubscribeSecond();
  ipcRenderer.emit('ws:event', { event_id: 'between-setups' });
  const thirdSetup = [];
  api.orchestration.onEvent((payload) => thirdSetup.push(payload));

  assert.deepEqual(firstSetup, [{ event_id: 'before-first-subscribe' }]);
  assert.deepEqual(secondSetup, [{ event_id: 'during-second-setup' }]);
  assert.deepEqual(thirdSetup, [{ event_id: 'between-setups' }]);
});
