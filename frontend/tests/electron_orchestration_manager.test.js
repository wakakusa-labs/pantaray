const assert = require('assert');
const { test } = require('node:test');

const {
  createOrchestrationManager,
} = require('../electron/dist/orchestration/orchestrationManager.js');

function createFakeTimers() {
  /** @type {Array<{ id: number, ms: number, fn: Function }>} */
  const timeouts = [];
  let nextId = 1;
  return {
    timeouts,
    setTimeout: (fn, ms) => {
      const id = nextId++;
      timeouts.push({ id, ms: Number(ms) || 0, fn });
      return id;
    },
    clearTimeout: (id) => {
      const idx = timeouts.findIndex((t) => t.id === id);
      if (idx >= 0) timeouts.splice(idx, 1);
    },
    runNextTimeout: async () => {
      const next = timeouts.shift();
      if (!next) return;
      await next.fn();
    },
  };
}

function conversationPage(actionId, status = 'processing') {
  return {
    action: {
      action_id: actionId,
      suggestion_id: 'sug-1',
      status,
      latest_run_id: 'run-1',
      approved_suggestion: null,
      resumable: false,
    },
    runs: [],
    unadopted_messages: [],
    next_cursor: null,
  };
}

function actionRequest(supplement = null, commandId = null) {
  return {
    suggestionId: 'sug-1',
    commandId,
    supplement,
    supplementProjectRefs: [],
    approvalMode: 'prompt_each_time',
    images: [],
  };
}

function createManagerHarness(overrides = {}) {
  const timers = createFakeTimers();
  const overlayPayloads = new Map();
  const sentMessages = [];
  const connectCalls = [];
  const mainWindowMessages = [];
  const overlayWindowMessages = [];
  /** @type {{ forwardEventToRenderers?: Function, forwardStatusToRenderers?: Function } | null} */
  let wsHooks = null;

  const manager = createOrchestrationManager({
    getRuntimeBackendUrl: () => overrides.runtimeBackendUrl ?? 'http://example.test',
    notificationWindow: {
      setActionLiveSnapshotGetter: () => {},
      setOverlaySnapshot: (id, payload) => {
        overlayPayloads.set(String(id), payload);
      },
      sendToAllOverlays: (channel, payload) => overlayWindowMessages.push({ channel, payload }),
      sendResetToAllOverlays: (channel, payload) =>
        overlayWindowMessages.push({ channel, payload }),
      dispatchEventToOverlay: () => false,
      sendToOverlay: (id, channel, payload) => {
        overlayWindowMessages.push({ id, channel, payload });
        overrides.onSendToOverlay?.(id, channel);
      },
      registerProcessAssociation: () => {},
      adoptActionAssociation: () => {},
      cleanupMappingsForProcess: (id) => overrides.onCleanupProcess?.(id),
      cleanupMappingsForAction: (id) => overrides.onCleanupAction?.(id),
      clearActionAssociations: () => overrides.onClearActionAssociations?.(),
      resolveOverlayId: overrides.resolveOverlayId || (() => null),
    },
    createOrchestrationWS: (opts) => {
      wsHooks = opts;
      if (overrides.createOrchestrationWS) return overrides.createOrchestrationWS(opts);
      return {
        connect: (url, headers) => {
          connectCalls.push({ url, headers });
        },
        send: (message) => {
          sentMessages.push(message);
        },
        disconnect: () => {},
        resumeProcess: (req) => {
          overrides.onResumeProcess?.(req);
          return true;
        },
      };
    },
    getMainWindow:
      overrides.getMainWindow ||
      (() => ({
        isDestroyed: () => false,
        webContents: {
          send: (channel, payload) => {
            mainWindowMessages.push({ channel, payload });
          },
        },
      })),
    getLocalApiToken: overrides.getLocalApiToken || (() => 'local-api-token'),
    getOwnerId: overrides.getOwnerId || (() => 'user-1'),
    getRuntimeState: overrides.getRuntimeState || (() => ({ status: 'ready', message: null })),
    getUiLanguage: overrides.getUiLanguage || (() => 'en'),
    readLatestActionConversationPage: async (actionId) => {
      return overrides.readLatestActionConversationPage
        ? await overrides.readLatestActionConversationPage(actionId)
        : conversationPage(actionId);
    },
    timers,
  });

  return {
    manager,
    timers,
    overlayPayloads,
    sentMessages,
    connectCalls,
    mainWindowMessages,
    overlayWindowMessages,
    getWsHooks: () => wsHooks,
  };
}

test('OrchestrationManager: reconnect replaces an unfinished handshake with current owner credentials', (t) => {
  const { createOrchestrationWS } = require('../electron/ws_orchestration');
  const {
    createFakeWebSocketClass,
    createFakeTimers: socketTimers,
  } = require('./helpers/electron_ws_fakes');
  const previousPort = process.env.FRONTEND_PORT;
  process.env.FRONTEND_PORT = '3001';
  t.after(() => {
    if (previousPort === undefined) delete process.env.FRONTEND_PORT;
    else process.env.FRONTEND_PORT = previousPort;
  });
  let owner = 'owner-a';
  let token = 'token-a';
  const { FakeWebSocket, instances } = createFakeWebSocketClass({ withPing: false });
  const harness = createManagerHarness({
    getOwnerId: () => owner,
    getLocalApiToken: () => token,
    createOrchestrationWS: (opts) =>
      createOrchestrationWS({
        ...opts,
        WebSocketImpl: FakeWebSocket,
        timers: socketTimers(),
        now: () => 1000,
      }),
  });
  harness.manager.ensureConnected();
  const first = instances[0];
  owner = 'owner-b';
  token = 'token-b';
  harness.manager.reconnect();
  assert.equal(instances.length, 2);
  assert.equal(first.readyState, FakeWebSocket.CLOSED);
  assert.equal(instances[1].url.endsWith('/users/owner-b/orchestrations'), true);
  assert.equal(instances[1].options.headers.Authorization, 'Bearer token-b');
  first.emit('open');
  assert.equal(harness.manager.isConnected(), false);
  instances[1].readyState = FakeWebSocket.OPEN;
  instances[1].emit('open');
  assert.equal(harness.manager.isConnected(), true);
  token = 'token-b-refreshed';
  harness.manager.reconnect();
  assert.equal(instances.length, 3);
  assert.equal(instances[1].readyState, FakeWebSocket.CLOSED);
  assert.equal(instances[2].options.headers.Authorization, 'Bearer token-b-refreshed');
  assert.equal(harness.manager.isConnected(), false);
  harness.manager.disconnect();
  assert.equal(
    harness.mainWindowMessages.filter((message) => message.channel === 'ws:status').at(-1).payload
      .status,
    'closed'
  );
});

test('OrchestrationManager: local backend URL から loopback WS endpoint を組み立てる', () => {
  const harness = createManagerHarness({
    runtimeBackendUrl: 'http://localhost:8005',
  });

  harness.manager.ensureConnected();

  assert.equal(harness.connectCalls.length, 1);
  assert.equal(
    harness.connectCalls[0].url,
    'ws://127.0.0.1:8005/v1/agents/users/user-1/orchestrations'
  );
});

test('OrchestrationManager: runtime backend URL getter の最新値で接続する', () => {
  let runtimeBackendUrl = 'http://127.0.0.1:8005';
  const harness = createManagerHarness({
    get runtimeBackendUrl() {
      return runtimeBackendUrl;
    },
  });

  runtimeBackendUrl = 'http://127.0.0.1:49152';
  harness.manager.ensureConnected();

  assert.equal(harness.connectCalls.length, 1);
  assert.equal(
    harness.connectCalls[0].url,
    'ws://127.0.0.1:49152/v1/agents/users/user-1/orchestrations'
  );
});

test('OrchestrationManager: submit success requests the existing canonical Action refresh', async () => {
  const reads = [];
  const harness = createManagerHarness({
    resolveOverlayId: () => 'sug-1',
    readLatestActionConversationPage: async (actionId) => {
      reads.push(actionId);
      return conversationPage(actionId);
    },
  });

  assert.equal(harness.manager.refreshActionConversation('action-1'), undefined);
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(reads, ['action-1']);
  assert.equal(
    harness.overlayWindowMessages.some(
      (message) =>
        message.id === 'sug-1' &&
        message.channel === 'action:conversationUpdated' &&
        message.payload.snapshot.actionId === 'action-1'
    ),
    true
  );
});

test('OrchestrationManager: subject reset clears the private command and replay timer', async () => {
  const cleaned = [];
  const harness = createManagerHarness({
    onClearActionAssociations: () => cleaned.push('actions'),
  });
  await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  hooks.forwardStatusToRenderers({ status: 'connected' });
  assert.equal(harness.timers.timeouts.length, 1);

  harness.manager.resetActionLive();
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'after-reset' });

  assert.deepEqual(cleaned, ['actions']);
  assert.equal(harness.manager.getOverlaySnapshot('sug-1'), null);
  assert.equal(harness.timers.timeouts.length, 0);
  assert.equal(harness.sentMessages.length, 1);
});

test('OrchestrationManager: acceptAction は送信失敗時に snapshot をロールバックする', async () => {
  const harness = createManagerHarness({
    getLocalApiToken: () => null,
    getOwnerId: () => null,
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });

  await assert.rejects(
    () => harness.manager.acceptAction(actionRequest()),
    /Local owner is unavailable/
  );

  assert.equal(harness.overlayPayloads.get('sug-1').snapshot.suggestionId, 'sug-1');
  assert.equal(harness.sentMessages.length, 0);
});

test('OrchestrationManager: runtimeBackendUrl が無い場合は暗黙フォールバックせず接続しない', () => {
  const timers = createFakeTimers();
  const connectCalls = [];
  const manager = createOrchestrationManager({
    getRuntimeBackendUrl: () => null,
    notificationWindow: {
      setActionLiveSnapshotGetter: () => {},
      setOverlaySnapshot: () => {},
      sendToAllOverlays: () => {},
      dispatchEventToOverlay: () => false,
      sendToOverlay: () => {},
      registerProcessAssociation: () => {},
      adoptActionAssociation: () => {},
      cleanupMappingsForProcess: () => {},
      cleanupMappingsForAction: () => {},
      resolveOverlayId: () => null,
    },
    createOrchestrationWS: () => ({
      connect: (url, headers) => {
        connectCalls.push({ url, headers });
      },
      send: () => {},
      disconnect: () => {},
      resumeProcess: () => true,
    }),
    getMainWindow: () => null,
    getLocalApiToken: () => 'local-api-token',
    getOwnerId: () => 'user-1',
    getRuntimeState: () => ({ status: 'ready', message: null }),
    getUiLanguage: () => 'en',
    readLatestActionConversationPage: async (actionId) => conversationPage(actionId),
    timers,
  });

  manager.ensureConnected();
  assert.equal(connectCalls.length, 0);
});

test('OrchestrationManager: runtime が ready でない場合は接続せず既存 socket を切断する', () => {
  let disconnectCount = 0;
  const connectCalls = [];
  const manager = createOrchestrationManager({
    getRuntimeBackendUrl: () => 'http://example.test',
    notificationWindow: {
      setActionLiveSnapshotGetter: () => {},
      setOverlaySnapshot: () => {},
      sendToAllOverlays: () => {},
      dispatchEventToOverlay: () => false,
      sendToOverlay: () => {},
      registerProcessAssociation: () => {},
      adoptActionAssociation: () => {},
      cleanupMappingsForProcess: () => {},
      cleanupMappingsForAction: () => {},
      resolveOverlayId: () => null,
    },
    createOrchestrationWS: () => ({
      connect: (url, headers) => {
        connectCalls.push({ url, headers });
      },
      send: () => {},
      disconnect: () => {
        disconnectCount += 1;
      },
      resumeProcess: () => true,
    }),
    getMainWindow: () => null,
    getLocalApiToken: () => 'local-api-token',
    getOwnerId: () => 'user-1',
    getRuntimeState: () => ({ status: 'degraded', message: 'sync failed' }),
    getUiLanguage: () => 'en',
    readLatestActionConversationPage: async (actionId) => conversationPage(actionId),
    timers: createFakeTimers(),
  });

  manager.ensureConnected();

  assert.equal(connectCalls.length, 0);
  assert.equal(disconnectCount, 1);
  assert.equal(manager.isConnected(), false);
});

test('OrchestrationManager: Action conversation reader未注入ではfail-fastする', () => {
  assert.throws(
    () =>
      createOrchestrationManager({
        getRuntimeBackendUrl: () => 'http://example.test',
        notificationWindow: {
          setActionLiveSnapshotGetter: () => {},
          setOverlaySnapshot: () => {},
          sendToAllOverlays: () => {},
          dispatchEventToOverlay: () => false,
          sendToOverlay: () => {},
          registerProcessAssociation: () => {},
          adoptActionAssociation: () => {},
          cleanupMappingsForProcess: () => {},
          cleanupMappingsForAction: () => {},
          resolveOverlayId: () => null,
        },
        createOrchestrationWS: () => ({
          connect: () => {},
          send: () => {},
          disconnect: () => {},
          resumeProcess: () => true,
        }),
        getMainWindow: () => null,
        getLocalApiToken: () => 'local-api-token',
        getOwnerId: () => 'user-1',
        getRuntimeState: () => ({ status: 'ready', message: null }),
        getUiLanguage: () => 'en',
      }),
    /Action conversation read/
  );
});

test('OrchestrationManager: acceptAction は send 例外時にも snapshot をロールバックする', async () => {
  const timers = createFakeTimers();
  const overlayPayloads = new Map();
  const manager = createOrchestrationManager({
    getRuntimeBackendUrl: () => 'http://example.test',
    notificationWindow: {
      setActionLiveSnapshotGetter: () => {},
      setOverlaySnapshot: (id, payload) => {
        overlayPayloads.set(String(id), payload);
      },
      sendToAllOverlays: () => {},
      dispatchEventToOverlay: () => false,
      sendToOverlay: () => {},
      registerProcessAssociation: () => {},
      adoptActionAssociation: () => {},
      cleanupMappingsForProcess: () => {},
      cleanupMappingsForAction: () => {},
      resolveOverlayId: () => null,
    },
    createOrchestrationWS: () => ({
      connect: () => {},
      send: () => {
        throw new Error('send boom');
      },
      disconnect: () => {},
      resumeProcess: () => true,
    }),
    getMainWindow: () => null,
    getLocalApiToken: () => 'local-api-token',
    getOwnerId: () => 'user-1',
    getRuntimeState: () => ({ status: 'ready', message: null }),
    getUiLanguage: () => 'en',
    readLatestActionConversationPage: async (actionId) => conversationPage(actionId),
    timers,
  });

  await assert.rejects(() => manager.acceptAction(actionRequest()), /send boom/);
  assert.equal(overlayPayloads.get('sug-1').snapshot.suggestionId, 'sug-1');
});

test('OrchestrationManager: acceptAction は commandId 未設定 snapshot に新しい UUID を採番する', async () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  const hooksAfterConnect = harness.getWsHooks();
  assert.ok(hooksAfterConnect);
  hooksAfterConnect.forwardEventToRenderers({
    event: 'process_completed',
    data: {
      kind: 'suggestion',
      process_id: 'proc-sug-1',
      suggestion_id: 'sug-1',
      status: 'success',
      interaction_contract: 'action_offer',
      intervention_mode: 'Executor',
    },
    meta: {
      kind: 'suggestion',
      process_id: 'proc-sug-1',
      suggestion_id: 'sug-1',
    },
  });

  const snapshot = harness.manager.getOverlaySnapshot('sug-1');
  assert.ok(snapshot);
  assert.equal(snapshot.commandId, null);

  const acceptedSnapshot = await harness.manager.acceptAction(actionRequest());
  assert.ok(acceptedSnapshot);
  assert.match(String(acceptedSnapshot.commandId), /^[0-9a-f-]{36}$/i);
  assert.equal(harness.sentMessages.length, 1);
  assert.equal(harness.sentMessages[0].data.command_id, acceptedSnapshot.commandId);
});

test('OrchestrationManager: command allocation時のexact envelopeだけを再送する', async () => {
  let language = 'ja';
  const harness = createManagerHarness({ getUiLanguage: () => language });

  const images = [
    { kind: 'image', storage_path: 'user-1/2026-09-11/11111111-1111-4111-8111-111111111111.png' },
  ];
  const files = [
    { attachment_id: '22222222-2222-4222-8222-222222222222', name: 'plan.pdf', byte_size: 42 },
  ];
  const snapshot = await harness.manager.acceptAction({
    ...actionRequest('Keep this condition'),
    approvalMode: 'always_allow',
    images,
    files,
  });
  assert.ok(snapshot);
  const executeEnvelope = {
    event: 'execute_action',
    data: {
      suggestion_id: 'sug-1',
      command_id: snapshot.commandId,
      language: 'ja',
      supplement: 'Keep this condition',
      supplement_project_refs: [],
      approval_mode: 'always_allow',
      images,
      files,
    },
  };
  assert.deepEqual(harness.sentMessages, [executeEnvelope]);
  assert.doesNotMatch(
    JSON.stringify(Array.from(harness.overlayPayloads.values())),
    /Keep this condition/
  );

  const hooks = harness.getWsHooks();
  assert.ok(hooks);

  language = 'en';
  await harness.manager.acceptAction(actionRequest('changed', snapshot.commandId));
  hooks.forwardStatusToRenderers({ status: 'connected' });
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-1' });
  await harness.timers.runNextTimeout();
  assert.deepEqual(harness.sentMessages, Array(4).fill(executeEnvelope));

  hooks.forwardEventToRenderers({
    event: 'action_requested',
    data: {
      suggestion_id: 'sug-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      committed_at: '2026-03-08T00:00:00Z',
    },
    meta: {
      suggestion_id: 'sug-1',
      kind: 'action',
      command_id: snapshot.commandId,
    },
  });
  assert.equal(harness.timers.timeouts.length, 1);

  await harness.timers.runNextTimeout();
  assert.deepEqual(harness.sentMessages, Array(5).fill(executeEnvelope));

  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  });

  assert.equal(harness.timers.timeouts.length, 0);
});

test('OrchestrationManager: private recordのないsame-command retryは送信しない', async () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  await assert.rejects(
    () => harness.manager.acceptAction(actionRequest(null, 'cmd-1')),
    /execute envelope is unavailable/
  );
  const hooks = harness.getWsHooks();
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-1' });
  assert.equal(harness.sentMessages.length, 0);
  assert.equal(harness.timers.timeouts.length, 0);
});

test('OrchestrationManager: processing snapshot は session_started で same command replay しない', async () => {
  const harness = createManagerHarness();
  const snapshot = await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  assert.ok(snapshot);
  assert.ok(hooks);

  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  });

  const sentBeforeReplay = harness.sentMessages.length;
  hooks.forwardStatusToRenderers({ status: 'connected' });
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-1' });

  assert.equal(harness.sentMessages.length, sentBeforeReplay);
  assert.equal(harness.timers.timeouts.length, 0);
});

test('OrchestrationManager: action process_paused は履歴を一度だけ無効化する', () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  assert.ok(hooks);

  hooks.forwardEventToRenderers({
    event: 'process_paused',
    data: {
      kind: 'action',
      process_id: 'proc-1',
      status: 'processing',
      reason: 'approval_pending',
      suggestion_id: 'sug-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
      completed_at: '2026-03-08T00:00:02Z',
      approval_blockers: [
        {
          process_id: 'proc-1',
          action_id: 'act-1',
          approval_session_id: 'approval-1',
          tool_request_id: 'tool-request-1',
          tool_id: 'shell',
          intent_class: 'workspace_write',
          command_summary: { command: 'touch output.txt' },
        },
      ],
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
    },
  });

  assert.deepEqual(
    harness.mainWindowMessages.filter((entry) => entry.channel === 'history:changed'),
    [
      {
        channel: 'history:changed',
        payload: {
          source: 'orchestration_event',
          event: 'process_paused',
          suggestion_id: 'sug-1',
        },
      },
    ]
  );
});

test('OrchestrationManager: Action本文eventをlegacy rendererへ渡さずDB snapshotだけをtarget配信する', async () => {
  const harness = createManagerHarness({ resolveOverlayId: () => 'sug-1' });
  const snapshot = await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  assert.ok(snapshot);
  assert.ok(hooks);

  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  });
  await new Promise(setImmediate);
  hooks.forwardEventToRenderers({
    event: 'completion_chunk',
    data: { content: 'failure text' },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
    },
  });

  const overlay = harness.overlayPayloads.get('sug-1');
  assert.ok(overlay);
  assert.equal(overlay.snapshot.actionPhase, 'processing');
  assert.equal(overlay.snapshot.actionStatus, 'processing');
  assert.deepEqual(
    [harness.mainWindowMessages, harness.overlayWindowMessages].map((messages) =>
      messages.some((x) => x.channel === 'action:conversationUpdated')
    ),
    [true, true]
  );
  assert.equal(
    harness.mainWindowMessages.some((x) => x.payload.event === 'completion_chunk'),
    false
  );
});

test('OrchestrationManager: terminal DB snapshotのtarget配信後にAction mappingを削除する', async () => {
  const notificationCalls = [];
  let readCount = 0;
  let resolveTerminalPage;
  const terminalPage = new Promise((resolve) => {
    resolveTerminalPage = resolve;
  });
  const harness = createManagerHarness({
    getMainWindow: () => ({
      isDestroyed: () => false,
      webContents: {
        send: () => {
          throw new Error('main renderer closed');
        },
      },
    }),
    resolveOverlayId: () => 'sug-1',
    onCleanupProcess: () => notificationCalls.push('cleanup_process'),
    onCleanupAction: () => notificationCalls.push('cleanup_action'),
    onSendToOverlay: (_id, channel) => {
      if (channel === 'action:conversationUpdated') notificationCalls.push('send_overlay');
    },
    readLatestActionConversationPage: async (actionId) => {
      readCount += 1;
      return readCount === 1 ? conversationPage(actionId) : await terminalPage;
    },
  });
  await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
    },
  });
  await new Promise(setImmediate);
  notificationCalls.length = 0;

  hooks.forwardEventToRenderers({
    event: 'process_completed',
    data: {
      kind: 'action',
      process_id: 'proc-1',
      status: 'success',
      suggestion_id: 'sug-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
    },
  });
  assert.deepEqual(notificationCalls, ['send_overlay', 'cleanup_process']);

  resolveTerminalPage(conversationPage('act-1', 'success'));
  await new Promise(setImmediate);

  assert.deepEqual(notificationCalls, [
    'send_overlay',
    'cleanup_process',
    'send_overlay',
    'cleanup_action',
  ]);
});

test('OrchestrationManager: terminal DB read失敗時はAction mappingを維持する', async () => {
  let readCount = 0;
  let actionMappingCleaned = false;
  const harness = createManagerHarness({
    onCleanupAction: () => {
      actionMappingCleaned = true;
    },
    readLatestActionConversationPage: async (actionId) => {
      readCount += 1;
      if (readCount === 2) throw new Error('state read failed');
      return conversationPage(actionId, readCount === 3 ? 'success' : 'processing');
    },
  });
  const snapshot = await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  assert.ok(snapshot);
  assert.ok(hooks);

  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  });
  await new Promise(setImmediate);
  hooks.forwardEventToRenderers({
    event: 'process_completed',
    data: {
      kind: 'action',
      process_id: 'proc-1',
      status: 'success',
      suggestion_id: 'sug-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  });

  await new Promise((resolve) => setImmediate(resolve));

  const overlay = harness.overlayPayloads.get('sug-1');
  assert.ok(overlay);
  assert.equal(overlay.snapshot.actionPhase, 'terminal');
  assert.equal(overlay.snapshot.actionStatus, 'success');
  hooks.forwardStatusToRenderers({ status: 'session_resumed', process_id: 'proc-1', missing: 1 });
  assert.deepEqual([actionMappingCleaned, readCount], [false, 2]);
  hooks.forwardStatusToRenderers({ status: 'resume_requested', action_id: 'act-1' });
  await new Promise(setImmediate);
  assert.deepEqual([actionMappingCleaned, readCount], [true, 3]);
  assert.deepEqual(
    harness.mainWindowMessages.filter((entry) => entry.channel === 'history:changed'),
    [
      {
        channel: 'history:changed',
        payload: {
          source: 'orchestration_event',
          event: 'process_started',
          suggestion_id: 'sug-1',
        },
      },
      {
        channel: 'history:changed',
        payload: {
          source: 'orchestration_event',
          event: 'process_completed',
          suggestion_id: 'sug-1',
        },
      },
    ]
  );
});

test('OrchestrationManager: subject resetはmappingとin-flight pageを破棄する', async () => {
  let resolveRead;
  const pendingRead = new Promise((resolve) => {
    resolveRead = resolve;
  });
  const cleanup = [];
  const harness = createManagerHarness({
    resolveOverlayId: () => 'sug-1',
    onCleanupProcess: (id) => cleanup.push(['process', id]),
    onClearActionAssociations: () => cleanup.push(['actions']),
    readLatestActionConversationPage: () => pendingRead,
  });
  const snapshot = await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  assert.ok(snapshot);
  assert.ok(hooks);

  const processEvent = {
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  };
  hooks.forwardEventToRenderers(processEvent);
  await new Promise(setImmediate);
  hooks.forwardEventToRenderers({
    ...processEvent,
    event: 'process_completed',
    data: { ...processEvent.data, status: 'success' },
  });
  harness.mainWindowMessages.length = 0;
  harness.manager.resetActionLive();
  resolveRead(conversationPage('act-1'));
  await new Promise(setImmediate);

  assert.deepEqual(cleanup, [['process', 'proc-1'], ['actions']]);
  const updates = harness.mainWindowMessages.filter(
    (entry) => entry.channel === 'action:conversationUpdated'
  );
  assert.deepEqual(
    updates.map((entry) => entry.payload.kind),
    ['reset']
  );
  assert.equal(
    harness.overlayWindowMessages.some(
      (entry) => entry.channel === 'action:conversationUpdated' && entry.payload.kind === 'reset'
    ),
    true
  );
});

test('OrchestrationManager: 再接続後の session_started で live action process の resume を再送する', async () => {
  const resumeCalls = [];
  const harness = createManagerHarness({
    onResumeProcess: (req) => resumeCalls.push(req),
  });
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  assert.ok(hooks);
  hooks.forwardStatusToRenderers({ status: 'connected' });
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-1' });

  // standalone HTTP submit の live attach 後に届く process_started(suggestion 紐付けなし)
  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      process_id: 'proc-1',
      suggestion_id: null,
      action_id: 'act-1',
      command_id: 'cmd-1',
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: null,
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: 'cmd-1',
    },
  });
  await new Promise(setImmediate);
  assert.deepEqual(resumeCalls, []);

  // 認証トークン更新による再接続 → 新しい session_started
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-2' });

  assert.deepEqual(resumeCalls, [
    { kind: 'action', processId: 'proc-1', actionId: 'act-1', fromStart: false },
  ]);

  // さらに次の再接続でも live な限り再送される(sticky)
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-3' });
  assert.equal(resumeCalls.length, 2);
});

test('OrchestrationManager: process_completed 済みの action process は再接続後に resume を再送しない', async () => {
  const resumeCalls = [];
  const harness = createManagerHarness({
    onResumeProcess: (req) => resumeCalls.push(req),
  });
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  assert.ok(hooks);
  hooks.forwardStatusToRenderers({ status: 'connected' });
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-1' });

  const identity = {
    kind: 'action',
    process_id: 'proc-1',
    suggestion_id: null,
    action_id: 'act-1',
    command_id: 'cmd-1',
  };
  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      ...identity,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: identity,
  });
  await new Promise(setImmediate);
  hooks.forwardEventToRenderers({
    event: 'process_completed',
    data: { ...identity, status: 'success' },
    meta: identity,
  });
  await new Promise(setImmediate);

  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-2' });

  assert.deepEqual(resumeCalls, []);
});

test('OrchestrationManager: reconnect/gapでActionのDB snapshotを再取得する', async () => {
  let readCount = 0;
  const harness = createManagerHarness({
    readLatestActionConversationPage: async (actionId) => {
      readCount += 1;
      return conversationPage(actionId);
    },
  });
  const snapshot = await harness.manager.acceptAction(actionRequest());
  const hooks = harness.getWsHooks();
  assert.ok(snapshot);
  assert.ok(hooks);

  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
    },
  });
  await new Promise(setImmediate);

  hooks.forwardStatusToRenderers({
    status: 'resume_requested',
    action_id: 'act-1',
  });
  await new Promise(setImmediate);

  assert.equal(readCount, 2);
  hooks.forwardStatusToRenderers({ status: 'session_resumed', process_id: 'proc-1', missing: 1 });
  await new Promise(setImmediate);
  assert.equal(readCount, 3);
});

test('OrchestrationManager: 履歴から開いた未終了の会話は root process の live resume を要求する', async () => {
  const resumed = [];
  const harness = createManagerHarness({
    onResumeProcess: (request) => resumed.push(request),
    readLatestActionConversationPage: async (actionId) =>
      conversationPage(actionId, actionId === 'action-terminal' ? 'success' : 'processing'),
  });
  harness.manager.ensureConnected();

  // A restart leaves no live attachment, so a nonterminal page resumes the
  // Action root process the latest run names.
  harness.manager.refreshAndResumeActionConversation('action-1');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(resumed, [
    { kind: 'action', actionId: 'action-1', processId: 'run-1', fromStart: true },
  ]);

  harness.manager.refreshAndResumeActionConversation('action-terminal');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(resumed.length, 1);
});

test('OrchestrationManager: 会話の読み込みに失敗したら resume せず失敗を返す', async (t) => {
  t.mock.method(console, 'error', () => {});
  const resumed = [];
  const harness = createManagerHarness({
    onResumeProcess: (request) => resumed.push(request),
    readLatestActionConversationPage: async () => {
      throw new Error('backend unavailable');
    },
  });
  harness.manager.ensureConnected();

  assert.equal(await harness.manager.refreshAndResumeActionConversation('action-1'), 'failed');
  assert.deepEqual(resumed, []);
});

test('OrchestrationManager: すでに live 中の Action は再 resume しない', async () => {
  const resumed = [];
  const harness = createManagerHarness({
    onResumeProcess: (request) => resumed.push(request),
  });
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  hooks.forwardEventToRenderers({
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'action-1',
      command_id: 'cmd-1',
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: {
      kind: 'action',
      suggestion_id: 'sug-1',
      process_id: 'proc-1',
      action_id: 'action-1',
      command_id: 'cmd-1',
    },
  });

  harness.manager.refreshAndResumeActionConversation('action-1');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(resumed, []);
});

test('standalone terminal notification is delivered to the bound Overlay even if its read fails', async () => {
  const harness = createManagerHarness({
    resolveOverlayId: ({ actionId }) => (actionId === 'act-direct' ? 'overlay-direct' : null),
    readLatestActionConversationPage: async () => {
      throw new Error('state read failed');
    },
  });
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  const identity = {
    kind: 'action',
    suggestion_id: null,
    action_id: 'act-direct',
    process_id: 'proc-direct',
    command_id: 'cmd-direct',
  };
  hooks.forwardEventToRenderers({
    event: 'process_completed',
    meta: identity,
    data: { ...identity, status: 'error' },
  });
  const update = harness.overlayWindowMessages.find(
    (entry) => entry.channel === 'action:conversationUpdated'
  );
  assert.equal(update.id, 'overlay-direct');
  assert.equal(update.payload.snapshot.actionId, 'act-direct');
  assert.deepStrictEqual(update.payload.snapshot.lifecycle, {
    processId: 'proc-direct',
    status: 'error',
  });
  await new Promise(setImmediate);
  assert.deepStrictEqual(harness.overlayWindowMessages.at(-1).payload.snapshot.lifecycle, {
    processId: 'proc-direct',
    status: 'error',
  });
});

test('OrchestrationManager: an existing transport cannot send or queue work while the local owner is unpublished', async () => {
  let owner = 'alice';
  const resumes = [];
  const h = createManagerHarness({
    getOwnerId: () => owner,
    onResumeProcess: (req) => resumes.push(req),
  });
  h.manager.ensureConnected();
  owner = null;
  await assert.rejects(
    h.manager.sendFromRenderer({ event: 'stop_process', data: { process_id: 'old-run' } }),
    /owner is unavailable/
  );
  assert.throws(
    () => h.manager.enqueueResumeRequest({ kind: 'action', processId: 'old-run' }),
    /owner is unavailable/
  );
  assert.deepEqual(h.sentMessages, []);
  assert.deepEqual(resumes, []);
});

test('OrchestrationManager: an owner change discards unsent resumes before connecting the next owner', () => {
  let owner = 'alice';
  const resumes = [];
  const h = createManagerHarness({
    getOwnerId: () => owner,
    onResumeProcess: (req) => resumes.push(req),
  });
  h.manager.enqueueResumeRequest({ kind: 'action', processId: 'old-run' });
  assert.deepEqual(resumes, []);
  owner = null;
  h.manager.resetActionLive();
  owner = 'bob';
  h.manager.ensureConnected();
  assert.deepEqual(resumes, []);
  h.manager.enqueueResumeRequest({ kind: 'action', processId: 'new-run' });
  assert.deepEqual(
    resumes.map((req) => req.processId),
    ['new-run']
  );
});

test('OrchestrationManager: owner loss during an asynchronous refresh abandons its internal resume', async () => {
  let ownerId = 'owner-a';
  let resolvePage;
  const pendingPage = new Promise((resolve) => {
    resolvePage = resolve;
  });
  const resumed = [];
  const harness = createManagerHarness({
    getOwnerId: () => ownerId,
    onResumeProcess: (request) => resumed.push(request),
    readLatestActionConversationPage: () => pendingPage,
  });
  harness.manager.ensureConnected();
  harness.manager.refreshAndResumeActionConversation('action-a');
  ownerId = null;
  resolvePage(conversationPage('action-a'));
  await new Promise(setImmediate);
  assert.deepEqual(resumed, []);
  assert.throws(
    () =>
      harness.manager.enqueueResumeRequest({
        kind: 'action',
        actionId: 'action-a',
        processId: 'run-1',
      }),
    /Local owner is unavailable/
  );
});

test('OrchestrationManager: a chat item reaches only the main window, parsed', () => {
  const harness = createManagerHarness({ resolveOverlayId: () => 'sug-1' });
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  const item = {
    sequence: 3,
    item_id: 'item-3',
    created_at: '2026-10-08T01:02:03.456Z',
    content: {
      kind: 'action_event',
      action_id: 'act-1',
      event: 'completed',
      final_answer_excerpt: 'Done.',
    },
  };

  hooks.forwardEventToRenderers({ event: 'chat_item_appended', data: { item }, event_id: 'evt-1' });

  assert.deepEqual(harness.mainWindowMessages, [{ channel: 'chat:itemAppended', payload: item }]);
  assert.deepEqual(harness.overlayWindowMessages, []);
  assert.throws(
    () =>
      hooks.forwardEventToRenderers({
        event: 'chat_item_appended',
        data: { item: { ...item, content: { kind: 'unknown' } } },
      }),
    /chat item response is invalid/
  );
  assert.equal(harness.mainWindowMessages.length, 1);

  hooks.forwardEventToRenderers({ event: 'chat_turn_state', data: { running: true } });
  assert.deepEqual(harness.mainWindowMessages[1], {
    channel: 'chat:turnState',
    payload: { running: true },
  });
  assert.throws(() =>
    hooks.forwardEventToRenderers({ event: 'chat_turn_state', data: { running: 'yes' } })
  );
  assert.deepEqual(harness.overlayWindowMessages, []);
});

test('main keeps the chat turn state for a main window that loads again mid-turn', () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  const hooks = harness.getWsHooks();
  assert.equal(harness.manager.getChatTurnState(), null);

  hooks.forwardEventToRenderers({ event: 'chat_turn_state', data: { running: true } });
  assert.deepEqual(harness.manager.getChatTurnState(), { running: true });
  hooks.forwardStatusToRenderers({ status: 'session_resumed' });
  assert.deepEqual(harness.manager.getChatTurnState(), { running: true });

  // A new session sends the state again; until then nothing is known.
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 's-2' });
  assert.equal(harness.manager.getChatTurnState(), null);
  hooks.forwardEventToRenderers({ event: 'chat_turn_state', data: { running: false } });
  assert.deepEqual(harness.manager.getChatTurnState(), { running: false });
  hooks.forwardStatusToRenderers({ status: 'closed' });
  assert.equal(harness.manager.getChatTurnState(), null);
});

function persistedSnapshot(overrides = {}) {
  return {
    suggestionId: 'sug-1',
    commandId: null,
    interactionContract: 'action_offer',
    suggestionText: 'Draft the reply',
    reactionState: null,
    reactionTimestamp: null,
    actionPhase: 'idle',
    actionStatus: null,
    actionErrorCode: null,
    actionFailureStage: null,
    actionFailureMessagePublic: null,
    processId: null,
    actionId: null,
    updatedAt: '2026-10-10T00:00:00Z',
    lastSequence: 3,
    isLive: false,
    ...overrides,
  };
}

function suggestionSnapshotsSentToMain(harness) {
  return harness.mainWindowMessages
    .filter((message) => message.channel === 'suggestion:snapshot')
    .map((message) => message.payload.snapshot);
}

test('the main window hears each suggestion record change, and the dismissal before it is cleared', () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  const stored = harness.manager.adoptSuggestionSnapshot(persistedSnapshot());

  assert.deepEqual(suggestionSnapshotsSentToMain(harness), [stored]);
  // The Overlay gets the same payload shape from the same store.
  assert.deepEqual(harness.overlayPayloads.get('sug-1').snapshot, stored);

  harness.getWsHooks().forwardEventToRenderers({
    event: 'suggestion_reaction_committed',
    sequence: 4,
    data: { suggestion_id: 'sug-1', reaction: 'rejected', committed_at: '2026-10-10T00:01:00Z' },
    meta: { suggestion_id: 'sug-1' },
  });

  const dismissed = suggestionSnapshotsSentToMain(harness).at(-1);
  assert.equal(dismissed.reactionState, 'rejected');
  assert.equal(dismissed.suggestionText, 'Draft the reply');
  assert.equal(harness.manager.getOverlaySnapshot('sug-1'), null);
});

test('a persisted suggestion read never replaces newer live state or a pending command', async () => {
  const harness = createManagerHarness();
  harness.manager.adoptSuggestionSnapshot(persistedSnapshot({ lastSequence: 3 }));
  const stale = harness.manager.adoptSuggestionSnapshot(
    persistedSnapshot({ lastSequence: 2, suggestionText: 'older' })
  );
  assert.equal(stale.suggestionText, 'Draft the reply');
  assert.equal(stale.lastSequence, 3);

  // Accepted from the chat before main read it: the record has no text and an unsent command.
  const accepted = createManagerHarness();
  const pending = await accepted.manager.acceptAction(actionRequest());
  const read = accepted.manager.adoptSuggestionSnapshot(persistedSnapshot({ lastSequence: 5 }));
  assert.equal(read.commandId, pending.commandId);
  assert.equal(read.actionPhase, 'requesting');
  assert.equal(read.suggestionText, 'Draft the reply');

  // The command is still replayed on the next session, so the accept is not lost.
  const hooks = accepted.getWsHooks();
  hooks.forwardStatusToRenderers({ status: 'session_started', session_id: 'sess-1' });
  assert.equal(accepted.sentMessages.length, 2);
  assert.deepEqual(accepted.sentMessages[1], accepted.sentMessages[0]);
});

test('a suggestion read that left before a dismissal does not answer it again', () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  // The read is in flight when a panel dismisses the suggestion main holds no record of.
  harness.getWsHooks().forwardEventToRenderers({
    event: 'suggestion_reaction_committed',
    sequence: 4,
    data: { suggestion_id: 'sug-1', reaction: 'rejected', committed_at: '2026-10-10T00:01:00Z' },
    meta: { suggestion_id: 'sug-1' },
  });

  const read = harness.manager.adoptSuggestionSnapshot(persistedSnapshot({ lastSequence: 3 }));

  assert.equal(read.reactionState, 'rejected');
  assert.equal(read.suggestionText, 'Draft the reply');
  assert.equal(harness.manager.getOverlaySnapshot('sug-1'), null);
  assert.deepEqual(suggestionSnapshotsSentToMain(harness), []);
  assert.equal(harness.overlayPayloads.has('sug-1'), false);

  // The dismissal belongs to the owner it happened under.
  harness.manager.resetActionLive();
  assert.equal(harness.manager.adoptSuggestionSnapshot(persistedSnapshot()).reactionState, null);
});

test('a same-sequence read fills the body of a suggestion accepted in a History panel', async () => {
  const harness = createManagerHarness();
  // The History panel kept its bootstrap to itself, so main's record starts without a body.
  const accepted = await harness.manager.acceptAction(actionRequest());
  harness.getWsHooks().forwardEventToRenderers({
    event: 'action_requested',
    sequence: 5,
    data: {
      suggestion_id: 'sug-1',
      command_id: accepted.commandId,
      accepted_at: '2026-10-10T00:00:00Z',
      committed_at: '2026-10-10T00:00:00Z',
    },
    meta: { suggestion_id: 'sug-1', kind: 'action', command_id: accepted.commandId },
  });

  const read = harness.manager.adoptSuggestionSnapshot(persistedSnapshot({ lastSequence: 5 }));

  assert.equal(read.suggestionText, 'Draft the reply');
  assert.equal(read.interactionContract, 'action_offer');
  assert.equal(read.reactionState, 'accepted');
  assert.equal(read.actionPhase, 'accepted_pending_start');
  assert.equal(read.commandId, accepted.commandId);
  assert.deepEqual(harness.manager.getOverlaySnapshot('sug-1'), read);
  assert.deepEqual(suggestionSnapshotsSentToMain(harness).at(-1), read);
});

test('an older read still fills the body of an accepted suggestion the events never carry', async () => {
  const harness = createManagerHarness();
  const accepted = await harness.manager.acceptAction(actionRequest());
  // The read fetched its bootstrap at sequence 3; action_requested at 5 reaches main first.
  harness.getWsHooks().forwardEventToRenderers({
    event: 'action_requested',
    sequence: 5,
    data: {
      suggestion_id: 'sug-1',
      command_id: accepted.commandId,
      accepted_at: '2026-10-10T00:00:00Z',
      committed_at: '2026-10-10T00:00:00Z',
    },
    meta: { suggestion_id: 'sug-1', kind: 'action', command_id: accepted.commandId },
  });

  const read = harness.manager.adoptSuggestionSnapshot(persistedSnapshot({ lastSequence: 3 }));

  assert.equal(read.suggestionText, 'Draft the reply');
  assert.equal(read.interactionContract, 'action_offer');
  assert.equal(read.reactionState, 'accepted');
  assert.equal(read.actionPhase, 'accepted_pending_start');
  assert.equal(read.commandId, accepted.commandId);
  assert.equal(read.lastSequence, 5);
  assert.deepEqual(harness.manager.getOverlaySnapshot('sug-1'), read);
});

test('an event resent after a dismissal does not bring the suggestion back', () => {
  const harness = createManagerHarness();
  harness.manager.ensureConnected();
  harness.manager.adoptSuggestionSnapshot(persistedSnapshot());
  const hooks = harness.getWsHooks();
  hooks.forwardEventToRenderers({
    event: 'suggestion_reaction_committed',
    sequence: 4,
    data: { suggestion_id: 'sug-1', reaction: 'rejected', committed_at: '2026-10-10T00:01:00Z' },
    meta: { suggestion_id: 'sug-1' },
  });
  const sentBeforeResend = suggestionSnapshotsSentToMain(harness).length;

  hooks.forwardEventToRenderers({
    event: 'suggestion_chunk',
    sequence: 5,
    data: { content: 'resent' },
    meta: { suggestion_id: 'sug-1', process_id: 'proc-s1', kind: 'suggestion' },
  });

  assert.equal(harness.manager.getOverlaySnapshot('sug-1'), null);
  assert.equal(suggestionSnapshotsSentToMain(harness).length, sentBeforeResend);
  assert.doesNotMatch(harness.overlayPayloads.get('sug-1').snapshot.suggestionText, /resent/);
  assert.equal(
    harness.manager.adoptSuggestionSnapshot(persistedSnapshot()).reactionState,
    'rejected'
  );
});
