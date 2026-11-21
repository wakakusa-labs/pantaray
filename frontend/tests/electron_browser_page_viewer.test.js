// The view-only screencast of an Action's app page, over the sessions' own hidden windows.
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');
const { test } = require('node:test');

const { fakeBrowserElectron } = require('./helpers/browser_electron_fakes');
const { createAppPageViewer } = require('../electron/dist/browser/pageViewer.js');
const { registerBrowserViewHandlers } = require('../electron/dist/ipc/handlers/browserView.js');
const { createIpcSenderSecurity } = require('../electron/dist/ipc/senderTrust.js');

const SESSION_MODULE = require.resolve('../electron/dist/browser/browserSession.js');
const POLICY = {
  allow_network: true,
  file_roots: [],
  private_roots: [],
  private_root_exceptions: [],
  secret_paths: [],
  write_hosts: [],
};
const SCREENCAST = { format: 'jpeg', maxWidth: 1280, maxHeight: 800 };

function loadSessions(electron) {
  delete require.cache[SESSION_MODULE];
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return electron;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require(SESSION_MODULE);
  } finally {
    Module._load = originalLoad;
    delete require.cache[SESSION_MODULE];
  }
}

/** A window that watches: what main sent it, and its destruction. */
function viewerWindow(id) {
  const window = new EventEmitter();
  return Object.assign(window, {
    id,
    updates: [],
    destroyed: false,
    isDestroyed: () => window.destroyed,
    send: (channel, update) => {
      assert.equal(channel, 'browserView:update');
      window.updates.push(update);
    },
    destroy: () => {
      window.destroyed = true;
      window.emit('destroyed');
    },
    of: (kind) => window.updates.filter((update) => update.kind === kind),
  });
}

function harness({ cdp } = {}) {
  const fake = fakeBrowserElectron({ cdp });
  const { createBrowserSessions } = loadSessions(fake.electron);
  const live = [];
  const logged = [];
  const viewer = createAppPageViewer({
    beginLivePage: (actionId) => {
      live.push(['begin', actionId]);
      return () => live.push(['end', actionId]);
    },
    logger: { error: (name, payload) => logged.push({ name, payload }) },
  });
  const sessions = createBrowserSessions({
    getBackendPort: () => 49152,
    pages: viewer.pageEvents,
    logger: { error: () => undefined },
  });
  const opened = new Map();
  /** Runs a navigate in `session` for `action`; its window is the page's. */
  const navigate = async (session, action, url = `https://${session}.example/`) => {
    const settled = await sessions.run(
      {
        session_id: session,
        action_id: action,
        operation: { action: 'navigate', target: { kind: 'web', url } },
        policy: POLICY,
      },
      20_000,
      (page) => page.contents.loadURL(url)
    );
    if (!opened.has(session)) opened.set(session, fake.windows.at(-1));
    return settled;
  };
  const contentsOf = (session) => opened.get(session).webContents;
  const cdpOf = (session) => contentsOf(session).debugger;
  const screencastCalls = (session) =>
    cdpOf(session)
      .commands.filter(({ method }) => /screencast/i.test(method))
      .map(({ method, params }) => (params === undefined ? method : [method, params]));
  // Electron names the page's own events by an empty session id.
  const frame = (session, sessionId, frameTarget = '') =>
    cdpOf(session).emit(
      'message',
      {},
      'Page.screencastFrame',
      {
        data: `jpeg-${session}-${sessionId}`,
        sessionId,
        metadata: { deviceWidth: 1280, deviceHeight: 800 },
      },
      frameTarget
    );
  return {
    fake,
    sessions,
    viewer,
    live,
    logged,
    navigate,
    contentsOf,
    cdpOf,
    screencastCalls,
    frame,
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// Catches: the stream started on another of the Action's pages or another Action's, frames
// broadcast to every window or a frame target's mistaken for the page's, the page's own frames
// (Electron's empty session id) dropped as a frame target's, a stream that stalls
// because frames are not acknowledged one by one, a screencast that changes the page's viewport
// or emulation, and a page update sent before the window can know its watch id.
test("a watch streams the Action's last-touched page to the asking window only", async () => {
  const h = harness();
  await h.navigate('s1', 'action-a');
  await h.navigate('s2', 'action-a');
  await h.navigate('s3', 'action-b');
  const before = h.cdpOf('s2').commands.length;
  const main = viewerWindow(1);
  const other = viewerWindow(2);

  const watching = h.viewer.watch('action-a', main);
  assert.equal(watching.kind, 'watching');
  assert.equal(main.updates.length, 0);
  h.viewer.watch('action-b', other);
  await settle();

  assert.deepEqual(h.screencastCalls('s2'), [['Page.startScreencast', SCREENCAST]]);
  assert.deepEqual(h.screencastCalls('s1'), []);
  assert.deepEqual(
    h
      .cdpOf('s2')
      .commands.slice(before)
      .map(({ method }) => method),
    ['Page.startScreencast']
  );
  assert.deepEqual(main.of('page'), [
    {
      kind: 'page',
      watchId: watching.watchId,
      url: 'https://s2.example/',
      title: 'title of https://s2.example/',
    },
  ]);

  h.frame('s2', 1);
  assert.deepEqual(h.screencastCalls('s2').at(-1), ['Page.screencastFrameAck', { sessionId: 1 }]);
  h.frame('s2', 2);
  h.frame('s2', 3, 'frame-target-1');
  h.frame('s1', 4);
  assert.deepEqual(main.of('frame'), [
    { kind: 'frame', watchId: watching.watchId, jpeg: 'jpeg-s2-1', width: 1280, height: 800 },
    { kind: 'frame', watchId: watching.watchId, jpeg: 'jpeg-s2-2', width: 1280, height: 800 },
  ]);
  assert.deepEqual(
    h.screencastCalls('s2').filter((call) => call[0] === 'Page.screencastFrameAck'),
    [
      ['Page.screencastFrameAck', { sessionId: 1 }],
      ['Page.screencastFrameAck', { sessionId: 2 }],
    ]
  );
  assert.deepEqual(other.of('frame'), []);
});

// Catches: a watch left on a page the Action no longer works on, two screencasts left running,
// and frames of the old page still shown under the watch.
test('the watch follows the Action to the page it touches next', async () => {
  const h = harness();
  await h.navigate('s1', 'action-a');
  await h.navigate('s2', 'action-a');
  const main = viewerWindow(1);
  const { watchId } = h.viewer.watch('action-a', main);
  await settle();

  await h.navigate('s1', 'action-a', 'https://s1.example/next');
  await settle();
  // Shown as the screencast moved, before the navigate committed; then as Electron reports it.
  assert.equal(main.of('page').at(-1).url, 'https://s1.example/');
  h.contentsOf('s1').emit('did-navigate');
  assert.deepEqual(h.screencastCalls('s2'), [
    ['Page.startScreencast', SCREENCAST],
    'Page.stopScreencast',
  ]);
  assert.deepEqual(h.screencastCalls('s1'), [['Page.startScreencast', SCREENCAST]]);
  assert.deepEqual(main.of('page').at(-1), {
    kind: 'page',
    watchId,
    url: 'https://s1.example/next',
    title: 'title of https://s1.example/next',
  });
  h.frame('s2', 9);
  h.frame('s1', 1);
  assert.deepEqual(
    main.of('frame').map((update) => [update.watchId, update.jpeg]),
    [[watchId, 'jpeg-s1-1']]
  );
});

// Catches: a screencast or its frames outliving the watch, the window that closed or the page,
// a crash shown as an ordinary end, a watch ended while the Action still has a page to show,
// and the run's end leaving the window on a frozen frame.
test('a watch ends with the window, on unwatch, on a crash and at the end of the work', async () => {
  const h = harness();
  await h.navigate('s1', 'action-a');
  const main = viewerWindow(1);

  const first = h.viewer.watch('action-a', main);
  await settle();
  h.viewer.unwatch('another-watch', main.id);
  h.viewer.unwatch(first.watchId, 2);
  assert.equal(h.screencastCalls('s1').length, 1);
  h.viewer.unwatch(first.watchId, main.id);
  assert.equal(h.screencastCalls('s1').at(-1), 'Page.stopScreencast');
  h.frame('s1', 1);
  assert.deepEqual(main.of('frame'), []);

  const gone = viewerWindow(3);
  h.viewer.watch('action-a', gone);
  await settle();
  gone.destroy();
  assert.equal(h.screencastCalls('s1').at(-1), 'Page.stopScreencast');
  h.frame('s1', 2);
  assert.deepEqual(gone.of('frame'), []);

  await h.navigate('s2', 'action-a');
  const ending = h.viewer.watch('action-a', main);
  await settle();
  h.sessions.close('s2');
  await settle();
  assert.deepEqual(main.of('ended'), []);
  assert.equal(h.screencastCalls('s1').at(-1)[0], 'Page.startScreencast');
  h.sessions.closeAll();
  assert.deepEqual(main.of('ended'), [
    { kind: 'ended', watchId: ending.watchId, reason: 'work_ended' },
  ]);

  await h.navigate('s3', 'action-a');
  const crashing = h.viewer.watch('action-a', main);
  await settle();
  h.fake.windows.at(-1).webContents.emit('render-process-gone', {}, { reason: 'crashed' });
  assert.deepEqual(main.of('ended').at(-1), {
    kind: 'ended',
    watchId: crashing.watchId,
    reason: 'page_crashed',
  });
  h.frame('s3', 1);
  assert.deepEqual(main.of('frame'), []);
});

// Catches: a window that asks for an Action with nothing to show still fed, and its screencast
// left running, by the Action it watched before.
test("a watch for an Action with no page still ends the window's earlier watch", async () => {
  const h = harness();
  await h.navigate('s1', 'action-a');
  const main = viewerWindow(1);
  h.viewer.watch('action-a', main);
  await settle();
  const listeners = h.cdpOf('s1').listenerCount('message');

  assert.deepEqual(h.viewer.watch('action-b', main), { kind: 'not_live' });
  assert.equal(h.screencastCalls('s1').at(-1), 'Page.stopScreencast');
  assert.equal(h.cdpOf('s1').listenerCount('message'), listeners - 1);
  h.frame('s1', 1);
  h.contentsOf('s1').emit('did-navigate');
  assert.deepEqual(main.of('frame'), []);
  assert.equal(main.of('page').length, 1);
});

// Catches: another Action's page, a page no Action owns (a Suggestion's, the HTML preview's) or
// a closed one shown on request, and the view button offered for a page no Action owns or kept
// after the Action's last page closed.
test('only an Action with a live page of its own can be watched', async () => {
  const h = harness();
  const main = viewerWindow(1);
  await h.navigate('s1', 'action-a');
  await h.navigate('preview', null);
  assert.deepEqual(h.viewer.watch('action-b', main), { kind: 'not_live' });
  await h.navigate('s2', 'action-a');
  assert.deepEqual(h.live, [
    ['begin', 'action-a'],
    ['begin', 'action-a'],
  ]);
  h.sessions.close('s1');
  h.sessions.close('s2');
  h.sessions.close('preview');
  assert.deepEqual(h.live.slice(2), [
    ['end', 'action-a'],
    ['end', 'action-a'],
  ]);
  assert.deepEqual(h.viewer.watch('action-a', main), { kind: 'not_live' });
  assert.deepEqual(main.updates, []);
});

// Catches: the viewer waiting in the session's operation queue, so a stalled screencast delays
// the Action, and a failed screencast failing the operation or leaving the window waiting.
test("a failed screencast ends the watch, never the Action's operation", async () => {
  let rejectStart;
  const h = harness({
    cdp: (method) =>
      method === 'Page.startScreencast'
        ? new Promise((_resolve, reject) => (rejectStart = reject))
        : {},
  });
  await h.navigate('s1', 'action-a');
  const main = viewerWindow(1);
  const { watchId } = h.viewer.watch('action-a', main);
  await settle();

  assert.equal((await h.navigate('s1', 'action-a', 'https://s1.example/2')).status, 'ok');
  rejectStart(new TypeError('target closed'));
  await settle();
  assert.deepEqual(main.of('ended'), [{ kind: 'ended', watchId, reason: 'work_ended' }]);
  assert.deepEqual(h.logged, [
    { name: 'BROWSER_VIEW_SCREENCAST_ERR', payload: { error: 'TypeError' } },
  ]);
  assert.equal((await h.navigate('s1', 'action-a', 'https://s1.example/3')).status, 'ok');
});

// Catches: the Overlay, or a page that is not the app's, watching an Action's page, a renderer
// choosing the page or passing a malformed id, and a watch bound to another window than the
// sender's.
test('only the main window watches, by a well-formed Action id', async () => {
  const handlers = new Map();
  const calls = [];
  registerBrowserViewHandlers(
    {
      browserView: {
        watch: (actionId, sender) => {
          calls.push(['watch', actionId, sender.id]);
          return { kind: 'not_live' };
        },
        unwatch: (watchId, senderId) => calls.push(['unwatch', watchId, senderId]),
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  const sender = { id: 7 };
  assert.deepEqual(handlers.get('browserView:watch')({ sender }, { actionId: 'action-a' }), {
    kind: 'not_live',
  });
  handlers.get('browserView:unwatch')({ sender }, { watchId: 'watch-1' });
  for (const [channel, invalid] of [
    ['browserView:watch', { actionId: ' action-a' }],
    ['browserView:watch', { actionId: 'action-a', sessionId: 's1' }],
    ['browserView:watch', null],
    ['browserView:unwatch', { watchId: '' }],
  ]) {
    assert.throws(
      () => handlers.get(channel)({ sender }, invalid),
      (error) => error?.name === 'IpcValidationError'
    );
  }
  assert.deepEqual(calls, [
    ['watch', 'action-a', 7],
    ['unwatch', 'watch-1', 7],
  ]);

  const mainFrame = { url: 'http://127.0.0.1:3001/' };
  const mainEvent = { sender: { id: 1, mainFrame }, senderFrame: mainFrame };
  const overlayFrame = { url: 'http://127.0.0.1:3001/notification.html' };
  const overlayEvent = { sender: { id: 2, mainFrame: overlayFrame }, senderFrame: overlayFrame };
  const security = createIpcSenderSecurity({
    getMainWindow: () => ({ isDestroyed: () => false, webContents: mainEvent.sender }),
    isDevRuntime: () => true,
    frontendDevOrigin: 'http://127.0.0.1:3001',
    frontendDistIndex: '/app/dist/index.html',
    recordSecurityEvent: () => undefined,
  });
  security.registerWindow('overlay', overlayEvent.sender);
  for (const channel of ['browserView:watch', 'browserView:unwatch']) {
    assert.equal(security.authorize(channel, mainEvent), 'main');
    assert.throws(
      () => security.authorize(channel, overlayEvent),
      (error) => error.code === 'channel_not_allowed_for_window'
    );
  }
});
