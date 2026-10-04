const assert = require('assert');
const { test } = require('node:test');

// NOTE:
// - Electron の ipcMain 実体は node からは使えないため、fake を用いて
//   「どのチャンネルが登録されるか」と「代表的な入力バリデーション」を回帰固定する。

const channels = require('../electron/dist/ipc/channels.js');
const { registerAllIpcHandlers } = require('../electron/dist/ipc/registerAll.js');

function createFakeIpcMain() {
  /** @type {Map<string, Function>} */
  const invokeHandlers = new Map();
  /** @type {Map<string, Function>} */
  const sendListeners = new Map();
  /** @type {string[]} */
  const removedHandlers = [];
  /** @type {Array<{ channel: string, listener: Function }>} */
  const removedListeners = [];

  return {
    invokeHandlers,
    sendListeners,
    removedHandlers,
    removedListeners,
    handle: (channel, handler) => {
      invokeHandlers.set(channel, handler);
    },
    on: (channel, listener) => {
      sendListeners.set(channel, listener);
    },
    removeHandler: (channel) => {
      removedHandlers.push(channel);
      invokeHandlers.delete(channel);
    },
    removeListener: (channel, listener) => {
      removedListeners.push({ channel, listener });
      // best-effort: this fake keeps only one listener per channel
      sendListeners.delete(channel);
    },
  };
}

test('IPC registration: registers all expected channels (invoke/send)', async () => {
  const fakeIpc = createFakeIpcMain();

  const overlayFactoryCalls = [];
  let openNewConversationCalls = 0;
  const openActionConversationCalls = [];
  const deleteItemCalls = [];
  const shortcutInputs = [];
  const updateCalls = [];
  const ctx = {
    ipcMain: fakeIpc,
    security: { authorize: () => 'main', auditMutation: () => {} },
    windows: {
      getMainWindow: () => ({
        isDestroyed: () => false,
        getPosition: () => [10, 20],
        setPosition: (_x, _y) => {},
        close: () => {},
      }),
      openNewConversationOverlay: () => {
        openNewConversationCalls += 1;
        return 'created';
      },
      openActionConversationOverlay: (actionId) => {
        openActionConversationCalls.push(actionId);
        return 'created';
      },
      showMainRoute: () => {},
    },
    auth: {
      saveLoginState: (_isLoggedIn) => {},
      getState: () => ({ isLoggedIn: false, user: null }),
      signOut: async () => ({ ok: true }),
      startBrowserLogin: async (_route) => ({
        ok: true,
        attempt_id: 'attempt',
        code_challenge: 'challenge',
        route: 'login',
      }),
    },
    history: {
      fetch: async (_params) => ({ data: [], error: null }),
      markCompletionViewed: () => {},
      deleteItem: async (request) => {
        deleteItemCalls.push(request);
        return { ok: true };
      },
    },
    ui: {
      getLanguage: () => 'en',
      setLanguage: (_lang) => 'ja',
    },
    update: {
      getReadyNotice: () => ({ version: '0.2.2' }),
      restartToUpdate: () => {
        updateCalls.push('restart');
      },
    },
    shortcut: {
      getState: () => ({ accelerator: 'Option+Space', failure: null }),
      setAccelerator: (accelerator) => {
        shortcutInputs.push(accelerator);
        return { ok: true, state: { accelerator, failure: null } };
      },
    },
    screenshot: {
      getStatus: () => false,
      start: () => true,
      stop: () => true,
    },
    privacy: {
      getCaptureSettings: () => ({ mode: 'allow_only', apps: [] }),
      updateCaptureSettings: (next) => next,
      getBrowserUrlRules: () => ({ mode: 'all_sites' }),
      getIdeFileRules: () => ({ mode: 'on' }),
      setCaptureEditing: (v) => Boolean(v),
      updateBrowserUrlRules: (next) => next,
      updateIdeFileRules: (next) => next,
    },
    externalUrl: {
      open: async (_rawUrl) => {},
    },
    ws: {
      send: async (_message) => {},
      acceptAction: async (_suggestionId) => null,
      getStatus: () => ({ status: 'connected' }),
    },
    actions: { refreshActionConversation: () => {} },
    overlay: {
      resumeLiveProcess: (_payload) => {},
      getActionApprovalMode: async () => ({
        action_id: 'act-1',
        approval_mode: 'prompt_each_time',
        source: 'user_default',
      }),
      setActionApprovalMode: async () => ({
        action_id: 'act-1',
        approval_mode: 'prompt_each_time',
        source: 'action',
      }),
      createNotificationIpcHandlers: (options) => {
        overlayFactoryCalls.push(options);
        return {
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
        };
      },
    },
  };

  const res = registerAllIpcHandlers(ctx);

  // allowlist に載っているチャンネルは必ず handler が登録されていること（未使用エントリ = 攻撃面）。
  const expectedInvoke = [...channels.validInvokeChannels];
  const expectedSend = [...channels.validSendChannels];

  assert.deepEqual(Array.from(fakeIpc.invokeHandlers.keys()).sort(), expectedInvoke.slice().sort());
  assert.deepEqual(Array.from(fakeIpc.sendListeners.keys()).sort(), expectedSend.slice().sort());

  // registrar側の返却値も一致すること
  assert.deepEqual(res.registered.invoke.slice().sort(), expectedInvoke.slice().sort());
  assert.deepEqual(res.registered.send.slice().sort(), expectedSend.slice().sort());

  // overlay factory が DI 経由で呼ばれていること（node環境で electron を require しないため）
  assert.equal(overlayFactoryCalls.length, 1);
  assert.equal(typeof overlayFactoryCalls[0].resumeLiveProcess, 'function');

  const openNewConversation = fakeIpc.invokeHandlers.get('history:openNewConversation');
  assert.equal(await openNewConversation({}), undefined);
  assert.equal(openNewConversationCalls, 1);
  ctx.windows.openNewConversationOverlay = () => {
    throw new Error('open failed');
  };
  await assert.rejects(async () => openNewConversation({}), /open failed/);

  const openConversation = fakeIpc.invokeHandlers.get('history:openConversation');
  // The open result reaches the renderer so a blocked open ('login') is not treated as viewed.
  assert.equal(await openConversation({}, { actionId: 'action-1' }), 'created');
  assert.deepEqual(openActionConversationCalls, ['action-1']);
  await assert.rejects(async () => openConversation({}, { actionId: ' ' }));

  const deleteItem = fakeIpc.invokeHandlers.get('history:deleteItem');
  assert.deepEqual(await deleteItem({}, { kind: 'suggestion', id: 'suggestion-1' }), { ok: true });
  for (const invalid of [
    { kind: 'process', id: 'process-1' },
    { kind: 'conversation', id: ' action-1' },
    { kind: 'conversation', id: 'action-1', extra: true },
  ]) {
    await assert.rejects(async () => deleteItem({}, invalid), /history:deleteItem/);
  }
  assert.deepEqual(deleteItemCalls, [{ kind: 'suggestion', id: 'suggestion-1' }]);

  const setShortcut = fakeIpc.invokeHandlers.get('shortcut:setAccelerator');
  assert.deepEqual(await setShortcut({}, '  Command+K  '), {
    ok: true,
    state: { accelerator: 'Command+K', failure: null },
  });
  assert.deepEqual(shortcutInputs, ['Command+K']);
  assert.doesNotThrow(() => setShortcut({}, "Command+'"));
  assert.throws(() => setShortcut({}, 'A'), /shortcut:setAccelerator/);
  assert.throws(() => setShortcut({}, 'F12'), /shortcut:setAccelerator/);
  assert.throws(() => setShortcut({}, '   '), /shortcut:setAccelerator/);
  assert.throws(() => setShortcut({}, 'A'.repeat(129)), /shortcut:setAccelerator/);
  assert.deepEqual(shortcutInputs, ['Command+K', "Command+'"]);

  assert.deepEqual(await fakeIpc.invokeHandlers.get('update:getReadyNotice')({}), {
    version: '0.2.2',
  });
  await fakeIpc.invokeHandlers.get('update:restartToUpdate')({});
  assert.deepEqual(updateCalls, ['restart']);

  // dispose が best-effort で動作すること（fake では map から消える）
  res.dispose();
  assert.equal(fakeIpc.invokeHandlers.size, 0);
  assert.equal(fakeIpc.sendListeners.size, 0);
});

test('IPC registration: window:move rejects invalid payload', async () => {
  const fakeIpc = createFakeIpcMain();

  let lastSet = null;
  const ctx = {
    ipcMain: fakeIpc,
    security: { authorize: () => 'main', auditMutation: () => {} },
    windows: {
      getMainWindow: () => ({
        isDestroyed: () => false,
        getPosition: () => [0, 0],
        setPosition: (x, y) => {
          lastSet = { x, y };
        },
        close: () => {},
      }),
      showMainRoute: () => {},
    },
    auth: {
      saveLoginState: (_isLoggedIn) => {},
      getState: () => ({ isLoggedIn: false, user: null }),
      signOut: async () => ({ ok: true }),
      startBrowserLogin: async (_route) => ({
        ok: true,
        attempt_id: 'attempt',
        code_challenge: 'challenge',
        route: 'login',
      }),
    },
    history: { fetch: async () => ({ data: [], error: null }), markCompletionViewed: () => {} },
    ui: { getLanguage: () => 'en', setLanguage: () => 'en' },
    screenshot: { getStatus: () => false, start: () => false, stop: () => true },
    privacy: {
      getCaptureSettings: () => ({}),
      updateCaptureSettings: (next) => next,
      getBrowserUrlRules: () => ({}),
      getIdeFileRules: () => ({}),
      setCaptureEditing: () => false,
      updateBrowserUrlRules: (next) => next,
      updateIdeFileRules: (next) => next,
    },
    externalUrl: { open: async () => {} },
    ws: { send: async () => {} },
    actions: { refreshActionConversation: () => {} },
    overlay: {
      resumeLiveProcess: () => {},
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
  };

  registerAllIpcHandlers(ctx);

  const move = fakeIpc.invokeHandlers.get('window:move');
  assert.ok(typeof move === 'function');

  // invalid payload
  assert.equal(await move({}, { x: '1', y: 2 }), false);
  assert.equal(lastSet, null);

  // valid payload
  assert.equal(await move({}, { x: 1, y: 2 }), true);
  assert.deepEqual(lastSet, { x: 1, y: 2 });
});
