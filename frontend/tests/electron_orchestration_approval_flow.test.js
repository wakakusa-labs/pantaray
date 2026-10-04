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
      const idx = timeouts.findIndex((timeout) => timeout.id === id);
      if (idx >= 0) timeouts.splice(idx, 1);
    },
  };
}

function createManagerHarness(overrides = {}) {
  const timers = createFakeTimers();
  const overlayPayloads = new Map();
  const liveUpdates = [];
  let wsHooks = null;

  const manager = createOrchestrationManager({
    getRuntimeBackendUrl: () => 'http://example.test',
    notificationWindow: {
      setActionLiveSnapshotGetter: () => {},
      setOverlaySnapshot: (id, payload) => {
        overlayPayloads.set(String(id), payload);
      },
      sendToAllOverlays: () => {},
      sendResetToAllOverlays: () => {},
      dispatchEventToOverlay: () => false,
      sendToOverlay: (_id, channel, payload) => {
        if (channel === 'action:conversationUpdated') liveUpdates.push(payload);
      },
      registerProcessAssociation: () => {},
      adoptActionAssociation: () => {},
      cleanupMappingsForProcess: () => {},
      cleanupMappingsForAction: () => {},
      resolveOverlayId: ({ actionId }) => (actionId === 'act-1' ? 'sug-1' : null),
    },
    createOrchestrationWS: (opts) => {
      wsHooks = opts;
      return {
        connect: () => {},
        send: () => {},
        disconnect: () => {},
        resumeProcess: () => true,
      };
    },
    getMainWindow: () => null,
    getLocalApiToken: () => 'local-api-token',
    getOwnerId: () => 'user-1',
    getRuntimeState: () => ({ status: 'ready', message: null }),
    getUiLanguage: () => 'en',
    readLatestActionConversationPage: async (actionId) => ({
      action: {
        action_id: actionId,
        suggestion_id: 'sug-1',
        status: 'processing',
        latest_run_id: 'proc-1',
        approved_suggestion: null,
        resumable: false,
      },
      runs: [
        {
          run_id: 'proc-1',
          status: 'approval_pending',
          started_at: '2026-03-08T00:00:01Z',
          completed_at: null,
          completion_event_id: null,
          entries: [],
          final_output: null,
          error: null,
        },
      ],
      unadopted_messages: [],
      next_cursor: null,
    }),
    resolveOverlayBootstrap: overrides.resolveOverlayBootstrap,
    timers,
  });

  return {
    manager,
    liveUpdates,
    overlayPayloads,
    getWsHooks: () => wsHooks,
  };
}

function buildPendingApprovalBootstrap() {
  return {
    suggestionId: 'sug-1',
    snapshot: {
      suggestionId: 'sug-1',
      commandId: 'cmd-1',
      interactionContract: 'action_offer',
      interventionMode: 'Executor',
      suggestionText: 'Need approval',
      reactionState: 'accepted',
      reactionTimestamp: '2026-03-08T00:00:00Z',
      actionPhase: 'processing',
      actionStatus: 'processing',
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      processId: 'proc-1',
      actionId: 'act-1',
      updatedAt: '2026-03-08T00:00:03Z',
      lastSequence: 10,
      isLive: true,
    },
    lastSequence: 10,
    liveResume: {
      kind: 'action',
      processId: 'proc-1',
      actionId: 'act-1',
      commandId: 'cmd-1',
      acceptedAt: '2026-03-08T00:00:00Z',
    },
  };
}

test('OrchestrationManager: approval pending は process_paused payload から直接 overlay へ届く', async () => {
  let refreshCalls = 0;
  const harness = createManagerHarness({
    resolveOverlayBootstrap: async () => {
      refreshCalls += 1;
      return buildPendingApprovalBootstrap();
    },
  });

  const snapshot = await harness.manager.acceptAction({
    suggestionId: 'sug-1',
    commandId: null,
    supplement: null,
    supplementProjectRefs: [],
    approvalMode: 'prompt_each_time',
    images: [],
  });
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

  hooks.forwardEventToRenderers({
    event: 'process_paused',
    data: {
      kind: 'action',
      process_id: 'proc-1',
      status: 'processing',
      reason: 'approval_pending',
      suggestion_id: 'sug-1',
      action_id: 'act-1',
      command_id: snapshot.commandId,
      completed_at: '2026-03-08T00:00:03Z',
      approval_blockers: [
        {
          process_id: 'proc-1',
          action_id: 'act-1',
          approval_session_id: 'approval-1',
          tool_request_id: 'tool-request-1',
          tool_id: 'bash',
          intent_class: 'process_exec_local',
          command_summary: {
            kind: 'bash',
            command: 'pytest -q',
          },
        },
        {
          process_id: 'proc-1-subagent',
          action_id: 'act-1',
          approval_session_id: 'approval-2',
          tool_request_id: 'tool-request-2',
          tool_id: 'bash',
          intent_class: 'process_exec_local',
          command_summary: {
            kind: 'bash',
            command: 'ruff check',
          },
        },
      ],
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

  assert.equal(refreshCalls, 0);
  const overlay = harness.overlayPayloads.get('sug-1');
  assert.ok(overlay);
  assert.equal(overlay.snapshot.actionPhase, 'processing');
  assert.equal(overlay.snapshot.actionStatus, 'processing');
  const live = harness.liveUpdates.at(-1);
  // relay meta は root 固定なので、子 blocker の physical target は blocker 自身が持つ。
  assert.deepStrictEqual(
    live.snapshot.approvalBlockers.map((blocker) => [
      blocker.processId,
      blocker.toolRequestId,
      blocker.toolId,
    ]),
    [
      ['proc-1', 'tool-request-1', 'bash'],
      ['proc-1-subagent', 'tool-request-2', 'bash'],
    ]
  );
});
