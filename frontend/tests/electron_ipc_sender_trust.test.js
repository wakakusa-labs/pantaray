const assert = require('assert');
const { test } = require('node:test');

const {
  createIpcSenderSecurity,
  IpcSenderRejectedError,
} = require('../electron/dist/ipc/senderTrust.js');
const { createIpcRegistrar } = require('../electron/dist/ipc/registrar.js');
const { runAuditedMutation } = require('../electron/dist/ipc/handlers/auditedMutation.js');

function createSender(url, id) {
  const mainFrame = { url };
  return { sender: { id, mainFrame }, senderFrame: mainFrame };
}

function createSecurityHarness({ isDev = true } = {}) {
  const events = [];
  const mainEvent = createSender(
    isDev ? 'http://127.0.0.1:3001/#/settings' : 'file:///app/dist/index.html#/settings',
    1
  );
  const security = createIpcSenderSecurity({
    getMainWindow: () => ({ isDestroyed: () => false, webContents: mainEvent.sender }),
    isDevRuntime: () => isDev,
    frontendDevOrigin: 'http://127.0.0.1:3001',
    frontendDistIndex: '/app/dist/index.html',
    recordSecurityEvent: (name, metadata, level) => events.push({ name, metadata, level }),
  });
  return { events, mainEvent, security };
}

test('IPC sender guard allows main-only privacy operations from the main top frame', () => {
  const { mainEvent, security } = createSecurityHarness();
  assert.equal(security.authorize('privacy:updateCaptureSettings', mainEvent), 'main');
});

test('IPC sender guard allows overlay capabilities but rejects privacy operations', () => {
  const { security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html?item=1', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  assert.equal(security.authorize('actionFile:open', overlayEvent), 'overlay');
  assert.throws(
    () => security.authorize('privacy:updateCaptureSettings', overlayEvent),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
  );
});

test('Overlay reads the workspace approval default but cannot write it', () => {
  const { mainEvent, security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  assert.equal(
    security.authorize('approval:getWorkspaceEditCommandPreference', overlayEvent),
    'overlay'
  );
  assert.equal(security.authorize('approval:setWorkspaceEditCommandPreference', mainEvent), 'main');
  assert.throws(
    () => security.authorize('approval:setWorkspaceEditCommandPreference', overlayEvent),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
  );
});

test('Overlay reads workspace projects but cannot change them', () => {
  const { mainEvent, security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  assert.equal(security.authorize('workspaceSettings:get', overlayEvent), 'overlay');
  assert.equal(security.authorize('workspaceSettings:get', mainEvent), 'main');
  for (const channel of [
    'workspaceSettings:createProject',
    'workspaceSettings:deleteProject',
    'workspaceSettings:createFolder',
    'workspaceSettings:updateReadAccessScope',
  ]) {
    assert.throws(
      () => security.authorize(channel, overlayEvent),
      (error) =>
        error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
    );
  }
});

test('Conversation submission and read receipts belong to Overlay; History opens belong to main', () => {
  const { mainEvent, security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  assert.equal(security.authorize('action:submitMessage', overlayEvent), 'overlay');
  const attachmentChannels = [
    'action:attachImage',
    'actionImage:reveal',
    'action:attachFile',
    'action:discardAttachment',
  ];
  for (const channel of attachmentChannels) {
    assert.equal(security.authorize(channel, overlayEvent), 'overlay');
    assert.throws(
      () => security.authorize(channel, mainEvent),
      (error) =>
        error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
    );
  }
  assert.throws(
    () => security.authorize('action:submitMessage', mainEvent),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
  );
  assert.equal(security.authorize('action:readConversationPage', mainEvent), 'main');
  assert.equal(security.authorize('action:readConversationPage', overlayEvent), 'overlay');
  for (const channel of ['history:openNewConversation', 'history:deleteItem']) {
    assert.equal(security.authorize(channel, mainEvent), 'main');
    assert.throws(
      () => security.authorize(channel, overlayEvent),
      (error) =>
        error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
    );
  }
  assert.equal(security.authorize('auth:getState', overlayEvent), 'overlay');
  assert.equal(security.authorize('history:markCompletionViewed', overlayEvent), 'overlay');
  assert.throws(
    () => security.authorize('history:markCompletionViewed', mainEvent),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
  );
});

// A send-channel rejection is dropped silently, so a missing grant would leave the
// approval panel's settings link doing nothing.
test('Only the Overlay asks main to open the workspace settings page', () => {
  const { mainEvent, security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  assert.equal(security.authorize('overlay:openWorkspaceSettings', overlayEvent), 'overlay');
  assert.throws(
    () => security.authorize('overlay:openWorkspaceSettings', mainEvent),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
  );
});

test('Only the main window reads and acts on the update ready notice', () => {
  const { mainEvent, security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  for (const channel of ['update:getReadyNotice', 'update:restartToUpdate']) {
    assert.equal(security.authorize(channel, mainEvent), 'main');
    assert.throws(
      () => security.authorize(channel, overlayEvent),
      (error) =>
        error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
    );
  }
});

test('IPC sender guard rejects an unregistered WebContents with a trusted overlay URL', () => {
  const { security } = createSecurityHarness();
  const unregistered = createSender('http://127.0.0.1:3001/notification.html', 2);

  assert.throws(
    () => security.authorize('overlay:submitApprovalDecision', unregistered),
    (error) => error instanceof IpcSenderRejectedError && error.code === 'window_role_mismatch'
  );
});

test('IPC sender guard rejects remote documents without logging their URL or query', () => {
  const { events, security } = createSecurityHarness();
  const remoteEvent = createSender('https://attacker.example.test/?token=secret', 3);

  assert.throws(() => security.authorize('auth:getState', remoteEvent), IpcSenderRejectedError);
  assert.equal(events.length, 1);
  assert.ok(!JSON.stringify(events[0]).includes('attacker.example.test'));
  assert.ok(!JSON.stringify(events[0]).includes('secret'));
});

test('IPC sender guard rejects subframes even under the trusted origin', () => {
  const { mainEvent, security } = createSecurityHarness();
  const subframeEvent = {
    sender: mainEvent.sender,
    senderFrame: { url: 'http://127.0.0.1:3001/embedded.html' },
  };

  assert.throws(
    () => security.authorize('auth:getState', subframeEvent),
    (error) => error instanceof IpcSenderRejectedError && error.code === 'top_frame_required'
  );
});

test('IPC sender guard distinguishes packaged main and overlay documents', () => {
  const { mainEvent, security } = createSecurityHarness({ isDev: false });
  const overlayEvent = createSender('file:///app/dist/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);

  assert.equal(security.authorize('auth:getState', mainEvent), 'main');
  assert.equal(security.authorize('overlay:submitApprovalDecision', overlayEvent), 'overlay');
});

test('IPC sender guard revokes auxiliary capabilities when the window is unregistered', () => {
  const { security } = createSecurityHarness();
  const overlayEvent = createSender('http://127.0.0.1:3001/notification.html', 2);
  security.registerWindow('overlay', overlayEvent.sender);
  security.unregisterWindow(overlayEvent.sender);

  assert.throws(
    () => security.authorize('notification-hide', overlayEvent),
    (error) => error instanceof IpcSenderRejectedError && error.code === 'window_role_mismatch'
  );
});

test('privacy/capture audit records only operation and outcome', () => {
  const { events, security } = createSecurityHarness();
  security.auditMutation('screenshot.start', 'succeeded');
  assert.deepEqual(events, [
    {
      name: 'IPC_MUTATION',
      metadata: { operation: 'screenshot.start', outcome: 'succeeded' },
      level: 'info',
    },
  ]);
});

test('IPC send listener drops a rejected sender without throwing into the main process', () => {
  const { security } = createSecurityHarness();
  let registeredListener = null;
  let listenerCalls = 0;
  const registrar = createIpcRegistrar(
    {
      handle: () => {},
      on: (_channel, listener) => {
        registeredListener = listener;
      },
      removeHandler: () => {},
      removeListener: () => {},
    },
    security
  );
  registrar.on('ws:send', () => {
    listenerCalls += 1;
  });

  assert.doesNotThrow(() =>
    registeredListener(createSender('https://attacker.example.test/?token=secret', 3))
  );
  assert.equal(listenerCalls, 0);
});

test('privacy/capture mutation wrapper audits success and failure without swallowing errors', async () => {
  const outcomes = [];
  const context = {
    security: {
      auditMutation: (operation, outcome) => outcomes.push({ operation, outcome }),
    },
  };

  assert.equal(await runAuditedMutation(context, 'screenshot.start', () => true), true);
  assert.equal(await runAuditedMutation(context, 'screenshot.start', () => false), false);
  await assert.rejects(
    () =>
      runAuditedMutation(context, 'privacy.updateCaptureSettings', () => {
        throw new Error('persistence failed');
      }),
    /persistence failed/
  );
  assert.deepEqual(outcomes, [
    { operation: 'screenshot.start', outcome: 'succeeded' },
    { operation: 'screenshot.start', outcome: 'failed' },
    { operation: 'privacy.updateCaptureSettings', outcome: 'failed' },
  ]);
});
