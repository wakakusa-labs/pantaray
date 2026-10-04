const assert = require('assert');
const { test } = require('node:test');

const { registerAllIpcHandlers } = require('../electron/dist/ipc/registerAll.js');

function createFakeIpcMain() {
  const invokeHandlers = new Map();
  const sendListeners = new Map();
  return {
    invokeHandlers,
    sendListeners,
    handle: (channel, handler) => invokeHandlers.set(channel, handler),
    on: (channel, listener) => sendListeners.set(channel, listener),
    removeHandler: (channel) => invokeHandlers.delete(channel),
    removeListener: (channel) => sendListeners.delete(channel),
  };
}

function spy(returnValue) {
  const calls = [];
  const fn = (...args) => {
    calls.push(args);
    return typeof returnValue === 'function' ? returnValue(...args) : returnValue;
  };
  fn.calls = calls;
  return fn;
}

function asyncSpy(returnValue) {
  const calls = [];
  const fn = async (...args) => {
    calls.push(args);
    return typeof returnValue === 'function' ? returnValue(...args) : returnValue;
  };
  fn.calls = calls;
  return fn;
}

function validCapturePrivacySettings() {
  return {
    version: 2,
    apps: { mode: 'exclude', entries: [{ name: 'TestApp', bundleId: 'com.example.test' }] },
    websites: { mode: 'exclude', hosts: ['example.com'] },
    ideFileRules: {
      mode: 'on',
      onFileNameUnavailable: 'allow',
      sensitivePresets: { blockEnvFiles: true },
    },
  };
}

function validIdeFileRules() {
  return {
    mode: 'on',
    onFileNameUnavailable: 'allow',
    sensitivePresets: { blockEnvFiles: true },
  };
}

function buildCtx(overrides = {}) {
  const ipcMain = createFakeIpcMain();
  const ctx = {
    ipcMain,
    security: {
      authorize: () => 'main',
      auditMutation: spy(undefined),
    },
    windows: {
      getMainWindow: () => null,
      showMainRoute: () => {},
    },
    auth: {
      saveLoginState: () => {},
      getState: () => ({ isLoggedIn: false, user: null }),
      signOut: async () => ({ ok: true }),
      startBrowserLogin: async () => ({
        ok: true,
        attempt_id: 'a',
        code_challenge: 'c',
        route: 'login',
      }),
    },
    history: {
      fetch: async () => ({ data: [], error: null }),
      markCompletionViewed: spy(undefined),
    },
    actionFiles: { open: spy(undefined) },
    ui: { getLanguage: () => 'en', setLanguage: () => 'en' },
    approval: {
      getWorkspaceEditCommandPreference: async () => ({}),
      setWorkspaceEditCommandPreference: async () => ({}),
    },
    workspaceSettings: {
      get: async () => ({}),
      getReadAccessScope: async () => ({}),
      getCommandNetwork: async () => ({ command_network_enabled: true }),
      updateCommandNetwork: asyncSpy({ command_network_enabled: false }),
      createOrganization: asyncSpy({ organization_id: 'o1', display_name: 'Org' }),
      createProject: asyncSpy({
        project_id: 'p1',
        display_name: 'Proj',
        sort_order: 0,
        organization_ids: [],
      }),
      createFolder: asyncSpy({
        folder_id: 'f1',
        display_name: 'F',
        real_path: '/tmp',
        canonical_real_path: '/tmp',
        organization_ids: [],
        project_ids: [],
      }),
      deleteOrganization: asyncSpy(undefined),
      deleteProject: asyncSpy(undefined),
      deleteFolder: asyncSpy(undefined),
      updateProjectLinks: asyncSpy({
        project_id: 'p1',
        display_name: 'Proj',
        sort_order: 0,
        organization_ids: [],
      }),
      reorderProjects: asyncSpy({ project_ids: ['p1'] }),
      updateFolderLinks: asyncSpy({
        folder_id: 'f1',
        display_name: 'F',
        real_path: '/tmp',
        canonical_real_path: '/tmp',
        organization_ids: [],
        project_ids: [],
      }),
      updateReadAccessScope: asyncSpy({ read_access_scope: 'workspace' }),
      selectFolder: async () => ({ canceled: true, path: null }),
    },
    screenshot: { getStatus: () => false, start: () => true, stop: () => true },
    privacy: {
      getCaptureSettings: () => validCapturePrivacySettings(),
      updateCaptureSettings: spy((next) => next),
      listInstalledApps: async () => [],
      getIdeFileRules: () => validIdeFileRules(),
      setCaptureEditing: spy((v) => v),
      updateIdeFileRules: spy((v) => v),
    },
    externalUrl: { open: async () => {} },
    ws: {
      send: asyncSpy(undefined),
      acceptAction: asyncSpy(null),
      getStatus: spy({ status: 'connected' }),
    },
    actions: { refreshActionConversation: () => {} },
    overlay: {
      resumeLiveProcess: () => {},
      resolveOverlayBootstrap: async () => null,
      submitApprovalDecision: asyncSpy(null),
      createNotificationIpcHandlers: () => ({
        onResizeNotificationWindow: () => {},
        onNotificationActionAccept: () => {},
        onNotificationActionReject: () => {},
        onNotificationHide: () => {},
        onNotificationStopAction: () => {},
        onOverlayInteraction: () => {},
        onOverlayDragStart: () => {},
        onOverlayDragMove: () => {},
        onOverlayDragEnd: () => {},
        onHistoryOpenOverlay: () => {},
      }),
    },
    ...overrides,
  };
  registerAllIpcHandlers(ctx);
  return { ctx, ipcMain };
}

function getInvoke(ipcMain, channel) {
  const handler = ipcMain.invokeHandlers.get(channel);
  assert.ok(typeof handler === 'function', `missing invoke handler for ${channel}`);
  // Mirror ipcMain.handle behavior: synchronous throws become rejections.
  return async (...args) => handler({}, ...args);
}

function getListener(ipcMain, channel) {
  const listener = ipcMain.sendListeners.get(channel);
  assert.ok(typeof listener === 'function', `missing send listener for ${channel}`);
  return (...args) => listener({}, ...args);
}

async function flushMicrotasks() {
  for (let i = 0; i < 4; i += 1) {
    await Promise.resolve();
  }
}

function rejectsValidation(channel) {
  return (error) => {
    assert.equal(error?.name, 'IpcValidationError', `${channel} should throw IpcValidationError`);
    assert.equal(error.channel, channel);
    assert.ok(Array.isArray(error.issues) && error.issues.length > 0);
    return true;
  };
}

// ─── privacy:updateCaptureSettings ────────────────────────────────────────────
test('privacy:updateCaptureSettings rejects missing apps and skips downstream', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateCaptureSettings');
  await assert.rejects(
    invoke({ mode: 'allow_only' }),
    rejectsValidation('privacy:updateCaptureSettings')
  );
  assert.equal(ctx.privacy.updateCaptureSettings.calls.length, 0);
});

test('ui:getLanguage propagates main-process failures', async () => {
  const { ipcMain } = buildCtx({
    ui: {
      getLanguage: () => {
        throw new Error('settings unavailable');
      },
      setLanguage: () => 'ja',
    },
  });
  const invoke = getInvoke(ipcMain, 'ui:getLanguage');

  await assert.rejects(invoke(), /settings unavailable/);
});

test('history:markCompletionViewed validates the exact subject-bound pair', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'history:markCompletionViewed');
  const request = { subjectId: 'user-1', actionId: 'action-1', completionEventId: 'event-1' };
  await invoke(request);
  assert.deepEqual(ctx.history.markCompletionViewed.calls, [[request]]);
  await assert.rejects(
    invoke({ ...request, completionEventId: ' ', extra: true }),
    rejectsValidation('history:markCompletionViewed')
  );
  assert.equal(ctx.history.markCompletionViewed.calls.length, 1);
});

test('ui:setLanguage propagates main-process failures', async () => {
  const { ipcMain } = buildCtx({
    ui: {
      getLanguage: () => 'en',
      setLanguage: () => {
        throw new Error('save failed');
      },
    },
  });
  const invoke = getInvoke(ipcMain, 'ui:setLanguage');

  await assert.rejects(invoke('ja'), /save failed/);
});

test('privacy:updateCaptureSettings accepts valid payload and forwards parsed value', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateCaptureSettings');
  const payload = validCapturePrivacySettings();
  const result = await invoke(payload);
  assert.deepEqual(result, payload);
  assert.equal(ctx.privacy.updateCaptureSettings.calls.length, 1);
  assert.deepEqual(ctx.privacy.updateCaptureSettings.calls[0][0], payload);
});

test('privacy:updateCaptureSettings accepts the dialog payload without IDE rules', async () => {
  // Mirrors `buildCaptureSettingsPayload`: the filter dialog saves apps and
  // websites only, and the SSOT keeps the stored IDE rules.
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateCaptureSettings');
  const payload = {
    version: 2,
    apps: { mode: 'include_only', entries: [{ name: 'Notes', bundleId: 'com.apple.Notes' }] },
    websites: { mode: 'include_only', hosts: ['github.com'] },
  };
  await invoke(payload);
  assert.equal(ctx.privacy.updateCaptureSettings.calls.length, 1);
  assert.deepEqual(ctx.privacy.updateCaptureSettings.calls[0][0], payload);
});

test('privacy:updateCaptureSettings rejects an unsupported filter mode', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateCaptureSettings');
  const payload = validCapturePrivacySettings();
  payload.apps.mode = 'allow_only';
  await assert.rejects(invoke(payload), rejectsValidation('privacy:updateCaptureSettings'));
  assert.equal(ctx.privacy.updateCaptureSettings.calls.length, 0);
});

test('privacy:updateCaptureSettings rejects oversized apps array', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateCaptureSettings');
  const payload = validCapturePrivacySettings();
  payload.apps.entries = Array.from({ length: 1001 }, (_, idx) => ({
    name: `App${idx}`,
    bundleId: `com.example.app${idx}`,
  }));
  await assert.rejects(invoke(payload), rejectsValidation('privacy:updateCaptureSettings'));
  assert.equal(ctx.privacy.updateCaptureSettings.calls.length, 0);
});

// ─── privacy:setCaptureEditing ────────────────────────────────────────────────
const editingSessionId = '7e1f1805-f2f5-4973-b692-c0cbe5ba5e9e';
for (const request of [
  { kind: 'begin', ownerId: '', sessionId: editingSessionId },
  { kind: 'begin', sessionId: editingSessionId },
  { kind: 'end', sessionId: 'invalid' },
  { kind: 'end', sessionId: editingSessionId, ownerId: 'unexpected' },
]) {
  test(`privacy:setCaptureEditing rejects invalid ${JSON.stringify(request)}`, async () => {
    const { ctx, ipcMain } = buildCtx();
    await assert.rejects(getInvoke(ipcMain, 'privacy:setCaptureEditing')(request), rejectsValidation('privacy:setCaptureEditing'));
    assert.equal(ctx.privacy.setCaptureEditing.calls.length, 0);
  });
}

for (const request of [
  { kind: 'begin', ownerId: 'alice', sessionId: editingSessionId },
  { kind: 'end', sessionId: editingSessionId },
]) {
  test(`capture preload and IPC preserve ${request.kind} editing identity`, async () => {
    const { ctx, ipcMain } = buildCtx();
    const { createCaptureApi } = require('../electron/preload/capture_api');
    const api = createCaptureApi({ ipcRenderer: { invoke: (channel, payload) => getInvoke(ipcMain, channel)(payload) } });
    await api.privacy.setCaptureEditing(request);
    assert.deepEqual(ctx.privacy.setCaptureEditing.calls, [[request]]);
  });
}

// ─── privacy:updateIdeFileRules ──────────────────────────────────────────────
test('privacy:updateIdeFileRules rejects missing required field', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateIdeFileRules');
  await assert.rejects(invoke({ mode: 'on' }), rejectsValidation('privacy:updateIdeFileRules'));
  assert.equal(ctx.privacy.updateIdeFileRules.calls.length, 0);
});

test('privacy:updateIdeFileRules accepts valid payload', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'privacy:updateIdeFileRules');
  const payload = validIdeFileRules();
  await invoke(payload);
  assert.deepEqual(ctx.privacy.updateIdeFileRules.calls[0][0], payload);
});

// ─── workspaceSettings:* ──────────────────────────────────────────────────────
test('workspaceSettings:createOrganization rejects missing displayName', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:createOrganization');
  await assert.rejects(invoke({}), rejectsValidation('workspaceSettings:createOrganization'));
  assert.equal(ctx.workspaceSettings.createOrganization.calls.length, 0);
});

test('workspaceSettings:createOrganization accepts displayName and trims', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:createOrganization');
  await invoke({ displayName: '  Acme Co  ' });
  assert.deepEqual(ctx.workspaceSettings.createOrganization.calls[0][0], {
    displayName: 'Acme Co',
  });
});

test('workspaceSettings:createProject rejects non-array organizationIds', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:createProject');
  await assert.rejects(
    invoke({ displayName: 'P', organizationIds: 'not-an-array' }),
    rejectsValidation('workspaceSettings:createProject')
  );
  assert.equal(ctx.workspaceSettings.createProject.calls.length, 0);
});

test('workspaceSettings:createFolder rejects empty realPath', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:createFolder');
  await assert.rejects(
    invoke({ displayName: 'F', realPath: '   ', organizationIds: [], projectIds: [] }),
    rejectsValidation('workspaceSettings:createFolder')
  );
  assert.equal(ctx.workspaceSettings.createFolder.calls.length, 0);
});

test('workspaceSettings:reorderProjects rejects invalid and duplicate ids', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:reorderProjects');
  await assert.rejects(
    invoke({ projectIds: 'not-an-array' }),
    rejectsValidation('workspaceSettings:reorderProjects')
  );
  await assert.rejects(
    invoke({ projectIds: ['project-a', 'project-a'] }),
    rejectsValidation('workspaceSettings:reorderProjects')
  );
  await assert.rejects(
    invoke({ projectIds: Array.from({ length: 1001 }, (_, index) => `project-${index}`) }),
    rejectsValidation('workspaceSettings:reorderProjects')
  );
  assert.equal(ctx.workspaceSettings.reorderProjects.calls.length, 0);
});

test('workspaceSettings:reorderProjects accepts a complete ordered id array', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:reorderProjects');
  await invoke({ projectIds: [] });
  await invoke({ projectIds: [' project-b ', 'project-a'] });
  assert.deepEqual(
    ctx.workspaceSettings.reorderProjects.calls.map((call) => call[0]),
    [{ projectIds: [] }, { projectIds: ['project-b', 'project-a'] }]
  );
});

test('workspaceSettings:deleteOrganization rejects numeric id (no silent coercion)', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:deleteOrganization');
  await assert.rejects(invoke(42), rejectsValidation('workspaceSettings:deleteOrganization'));
  assert.equal(ctx.workspaceSettings.deleteOrganization.calls.length, 0);
});

test('workspaceSettings:deleteOrganization accepts string id and forwards it', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:deleteOrganization');
  await invoke('org_42');
  assert.deepEqual(ctx.workspaceSettings.deleteOrganization.calls[0], ['org_42']);
});

test('workspaceSettings:updateProjectLinks validates both positional args', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:updateProjectLinks');
  // empty id rejects
  await assert.rejects(
    invoke('', { organizationIds: [] }),
    rejectsValidation('workspaceSettings:updateProjectLinks')
  );
  // valid id but invalid input
  await assert.rejects(
    invoke('p1', { organizationIds: 'x' }),
    rejectsValidation('workspaceSettings:updateProjectLinks')
  );
  assert.equal(ctx.workspaceSettings.updateProjectLinks.calls.length, 0);
});

test('workspaceSettings:updateReadAccessScope rejects unknown scope', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:updateReadAccessScope');
  await assert.rejects(
    invoke('admin'),
    rejectsValidation('workspaceSettings:updateReadAccessScope')
  );
  assert.equal(ctx.workspaceSettings.updateReadAccessScope.calls.length, 0);
});

test('workspaceSettings:updateReadAccessScope accepts workspace and full_access', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:updateReadAccessScope');
  await invoke('workspace');
  await invoke('full_access');
  assert.deepEqual(
    ctx.workspaceSettings.updateReadAccessScope.calls.map((c) => c[0]),
    ['workspace', 'full_access']
  );
});

// ─── ws:send (fire-and-forget) + ws:acceptAction ──────────────────────────────
test('ws:send drops malformed envelope, logs once, never calls ctx.ws.send', async () => {
  const { ctx, ipcMain } = buildCtx();
  const listener = getListener(ipcMain, 'ws:send');
  const errors = [];
  const originalError = console.error;
  console.error = (...args) => errors.push(args);
  try {
    listener({ event: 'unknown_event', data: {} });
    await flushMicrotasks();
  } finally {
    console.error = originalError;
  }
  assert.equal(ctx.ws.send.calls.length, 0);
  assert.equal(errors.length, 1);
  assert.ok(errors[0][0].includes('[ws:send] dropped invalid payload'));
});

test('ws:send forwards a valid dismiss_suggestion event', async () => {
  const { ctx, ipcMain } = buildCtx();
  const listener = getListener(ipcMain, 'ws:send');
  const payload = {
    event: 'dismiss_suggestion',
    data: { suggestion_id: 's1', reason: 'later' },
  };
  listener(payload);
  await flushMicrotasks();
  assert.equal(ctx.ws.send.calls.length, 1);
  assert.deepEqual(ctx.ws.send.calls[0][0], payload);
});

test('ws:send rejects a complete execute_action outside the dedicated IPC', async () => {
  const { ctx, ipcMain } = buildCtx();
  const listener = getListener(ipcMain, 'ws:send');
  const errors = [];
  const originalError = console.error;
  console.error = (...args) => errors.push(args);
  try {
    listener({
      event: 'execute_action',
      data: {
        suggestion_id: 's1',
        command_id: 'c1',
        language: 'en',
        supplement: null,
        approval_mode: 'prompt_each_time',
        images: [],
      },
    });
    await flushMicrotasks();
  } finally {
    console.error = originalError;
  }
  assert.equal(ctx.ws.send.calls.length, 0);
  assert.equal(errors.length, 1);
});

test('ws:acceptAction rejects invalid fields and Unicode overflow', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'ws:acceptAction');
  await assert.rejects(
    invoke({ approvalMode: 'prompt_each_time', images: [], suggestionId: 123, commandId: null, supplement: null, supplementProjectRefs: [] }),
    rejectsValidation('ws:acceptAction')
  );
  await assert.rejects(
    invoke({ approvalMode: 'prompt_each_time', images: [], suggestionId: 's_123', commandId: null, supplement: null, supplementProjectRefs: [], extra: true }),
    rejectsValidation('ws:acceptAction')
  );
  await assert.rejects(
    invoke({ approvalMode: 'prompt_each_time', images: [], suggestionId: 's_123', commandId: null, supplement: '😀'.repeat(8_001), supplementProjectRefs: [] }),
    rejectsValidation('ws:acceptAction')
  );
  for (const invalid of [
    { approvalMode: 'unknown' },
    { images: [{ kind: 'file', storage_path: 'private.pdf' }] },
    { images: Array(33).fill({ kind: 'image', storage_path: 'image.png' }) },
    { supplementProjectRefs: [{ project_id: 'p1', display_name: 'Demo', paths: [], start: -1, end: 4 }] },
    { files: [{ attachment_id: '../staged', name: 'a.pdf', byte_size: 1 }] },
  ]) {
    await assert.rejects(invoke({ approvalMode: 'prompt_each_time', images: [], suggestionId: 's_123', commandId: null, supplement: null, supplementProjectRefs: [], ...invalid }), rejectsValidation('ws:acceptAction'));
  }
  assert.equal(ctx.ws.acceptAction.calls.length, 0);
});

test('ws:acceptAction normalizes blank and accepts 8,000 Unicode code points', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'ws:acceptAction');
  const supplementProjectRefs = [{ project_id: 'p1', display_name: '😀', paths: ['/workspace/demo'], start: 0, end: 1 }];
  const files = [{ attachment_id: '22222222-2222-4222-8222-222222222222', name: 'plan.pdf', byte_size: 42 }];
  const request = { approvalMode: 'prompt_each_time', images: [], suggestionId: 's_123', commandId: null, supplement: '😀'.repeat(8_000), supplementProjectRefs, files };
  await invoke(request);
  await invoke({ approvalMode: 'prompt_each_time', images: [], suggestionId: 's_123', commandId: null, supplement: '   ', supplementProjectRefs: [] });
  assert.deepEqual(ctx.ws.acceptAction.calls, [
    [request],
    [{ approvalMode: 'prompt_each_time', images: [], suggestionId: 's_123', commandId: null, supplement: null, supplementProjectRefs: [] }],
  ]);
});

test('ws:getStatus returns the current websocket status', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'ws:getStatus');
  const result = await invoke();

  assert.deepEqual(result, { status: 'connected' });
  assert.equal(ctx.ws.getStatus.calls.length, 1);
});

// ─── overlay:submitApprovalDecision ───────────────────────────────────────────
test('overlay:submitApprovalDecision rejects missing toolRequestId', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'overlay:submitApprovalDecision');
  await assert.rejects(
    invoke({
      actionId: 'a',
      processId: 'p',
      approvalSessionId: 'as',
      decision: 'approved_once',
    }),
    rejectsValidation('overlay:submitApprovalDecision')
  );
  assert.equal(ctx.overlay.submitApprovalDecision.calls.length, 0);
});

test('overlay:submitApprovalDecision rejects wrong decision enum', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'overlay:submitApprovalDecision');
  await assert.rejects(
    invoke({
      actionId: 'a',
      processId: 'p',
      approvalSessionId: 'as',
      toolRequestId: 'tr',
      decision: 'maybe',
    }),
    rejectsValidation('overlay:submitApprovalDecision')
  );
  assert.equal(ctx.overlay.submitApprovalDecision.calls.length, 0);
});

test('overlay:submitApprovalDecision accepts every approval decision', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'overlay:submitApprovalDecision');
  const decisions = ['approved_once', 'approved_for_conversation', 'denied'];
  for (const decision of decisions) {
    const payload = {
      actionId: 'a1',
      processId: 'p1',
      approvalSessionId: 'as1',
      toolRequestId: 'tr1',
      decision,
    };
    await invoke(payload);
    assert.deepEqual(ctx.overlay.submitApprovalDecision.calls.at(-1)[0], payload);
  }
});

// ─── actionFile:open ──────────────────────────────────────────────────────────
test('actionFile:open rejects missing and relative paths', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'actionFile:open');
  for (const payload of [{}, { path: '' }, { path: 'docs/report.md' }, { path: '~/report.md' }]) {
    await assert.rejects(invoke(payload), rejectsValidation('actionFile:open'));
  }
  assert.equal(ctx.actionFiles.open.calls.length, 0);
});

test('actionFile:open forwards an absolute folder path without an action id', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'actionFile:open');
  await invoke({ path: '/Users/name/project folder' });
  assert.deepEqual(ctx.actionFiles.open.calls[0][0], { path: '/Users/name/project folder' });
});

test('workspaceSettings:updateCommandNetwork accepts booleans and rejects coercion', async () => {
  const { ctx, ipcMain } = buildCtx();
  const invoke = getInvoke(ipcMain, 'workspaceSettings:updateCommandNetwork');
  for (const invalid of ['false', 0, 1, null, undefined, { command_network_enabled: false }]) {
    await assert.rejects(
      invoke(invalid),
      rejectsValidation('workspaceSettings:updateCommandNetwork')
    );
  }
  assert.equal(ctx.workspaceSettings.updateCommandNetwork.calls.length, 0);
  await invoke(false);
  await invoke(true);
  assert.deepEqual(
    ctx.workspaceSettings.updateCommandNetwork.calls.map((call) => call[0]),
    [false, true]
  );
});
