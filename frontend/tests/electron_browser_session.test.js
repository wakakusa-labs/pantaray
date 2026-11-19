const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { pathToFileURL } = require('node:url');
const { mock, test } = require('node:test');

const SESSION_MODULE = require.resolve('../electron/dist/browser/browserSession.js');

const BACKEND_PORT = 49152;

/** Fake Electron: one session per partition, and windows whose page `load` scripts. */
function fakeElectron({ load = async () => {} } = {}) {
  const partitions = new Map();
  const windows = [];
  function fromPartition(name) {
    if (partitions.has(name)) return partitions.get(name);
    const ses = new EventEmitter();
    Object.assign(ses, {
      proxies: [],
      closedConnections: 0,
      cleared: [],
      protocol: {
        handle: (_scheme, handler) => (ses.fileHandler = handler),
        unhandle: (scheme) => (ses.unhandled = scheme),
      },
      webRequest: {
        onBeforeRequest: (listener) => (ses.beforeRequest = listener),
        onHeadersReceived: (listener) => (ses.headersReceived = listener),
      },
      setPermissionRequestHandler: (handler) => (ses.permissionRequest = handler),
      setPermissionCheckHandler: (handler) => (ses.permissionCheck = handler),
      setDevicePermissionHandler: (handler) => (ses.devicePermission = handler),
      setProxy: async (config) => ses.proxies.push(config),
      closeAllConnections: async () => (ses.closedConnections += 1),
      clearStorageData: async () => ses.cleared.push('storage'),
      clearCache: async () => ses.cleared.push('cache'),
    });
    partitions.set(name, ses);
    return ses;
  }
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.destroyed = false;
      this.shown = false;
      const contents = new EventEmitter();
      const debuggerApi = new EventEmitter();
      Object.assign(debuggerApi, {
        commands: [],
        attach: (version) => (debuggerApi.attached = { version, afterUrl: url }),
        sendCommand: async (method, params, sessionId) =>
          debuggerApi.commands.push({ method, params, sessionId }),
      });
      const ses = fromPartition(options.webPreferences.partition);
      let url = '';
      Object.assign(contents, {
        debugger: debuggerApi,
        getURL: () => url,
        getTitle: () => `title of ${url}`,
        loadURL: async (next) => {
          if (next !== 'about:blank') await load(next, { ses, contents });
          url = next;
        },
        setWindowOpenHandler: (handler) => (contents.openHandler = handler),
        setWebRTCIPHandlingPolicy: (policy) => (contents.webRtcPolicy = policy),
      });
      this.webContents = contents;
      windows.push(this);
    }

    show() {
      this.shown = true;
    }

    focus() {
      this.shown = true;
    }

    isDestroyed() {
      return this.destroyed;
    }

    destroy() {
      this.destroyed = true;
      this.emit('closed');
    }
  }
  return { partitions, windows, electron: { BrowserWindow, session: { fromPartition } } };
}

function loadOnElectron(electron) {
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

function harness(scripts = {}) {
  const fake = fakeElectron(scripts);
  const { createBrowserSessions } = loadOnElectron(fake.electron);
  const logged = [];
  const sessions = createBrowserSessions({
    getBackendPort: () => BACKEND_PORT,
    logger: { error: (name, payload) => logged.push({ name, payload }) },
  });
  const ses = (index = 0) =>
    fake.partitions.get(fake.windows[index].options.webPreferences.partition);
  return { ...fake, sessions, logged, ses };
}

function request(action, { session = 'session-1', policy = {} } = {}) {
  return {
    operation_request_id: 'op-1',
    session_id: session,
    action_id: 'action-1',
    process_id: 'process-1',
    tool_request_id: 'tool-1',
    operation:
      action === 'navigate'
        ? { action, target: { kind: 'web', url: 'https://a.example/' } }
        : { action: 'back' },
    policy: {
      allow_network: true,
      file_roots: ['/work'],
      private_roots: [],
      private_root_exceptions: [],
      secret_paths: [],
      write_hosts: [],
      ...policy,
    },
  };
}

/** Runs a navigate to `url` in the named session, with the given budget. */
function navigate(h, url, options = {}, budgetMs = 20_000) {
  return h.sessions.run(request('navigate', options), budgetMs, (page) =>
    page.contents.loadURL(url)
  );
}

/** Settles once `release` is called, so a test can hold an operation in flight. */
function gate() {
  let release;
  const opened = new Promise((resolve) => (release = resolve));
  return { opened, release };
}

const turn = () => new Promise((resolve) => setImmediate(resolve));

// Catches: sessions sharing cookies or storage, a partition persisted to disk, a page window
// shown or focused on the user's screen, and captures at the display's device scale.
test('each session has its own in-memory partition and a window that is never shown', async () => {
  const h = harness();
  await navigate(h, 'https://a.example/', { session: 'one' });
  await navigate(h, 'https://b.example/', { session: 'two' });
  assert.deepEqual(
    h.windows.map((window) => window.options.webPreferences.partition),
    ['browser-session-1', 'browser-session-2']
  );
  assert.equal(h.partitions.size, 2);
  for (const window of h.windows) {
    assert.equal(window.options.show, false);
    assert.equal(window.options.focusable, false);
    assert.equal(window.shown, false);
    // A command sent before the first navigation may never settle.
    assert.deepEqual(window.webContents.debugger.attached, {
      version: '1.3',
      afterUrl: 'about:blank',
    });
    assert.equal(window.webContents.webRtcPolicy, 'disable_non_proxied_udp');
    assert.deepEqual(
      window.webContents.debugger.commands.find(
        (command) => command.method === 'Emulation.setDeviceMetricsOverride'
      ).params,
      { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false }
    );
  }
});

// Catches: an answer that loses the page's url and title or what the operation returned.
test('an operation settles with its value and the page it left', async () => {
  const h = harness();
  const settled = await h.sessions.run(request('navigate'), 20_000, async (page) => {
    await page.contents.loadURL('https://a.example/');
    return 'value';
  });
  assert.deepEqual(settled, {
    status: 'ok',
    value: 'value',
    url: 'https://a.example/',
    title: 'title of https://a.example/',
    report: { notes: [], popups: [], blocked: [], network: [] },
  });
});

// Catches: a page request not judged by the page policy, the latest policy not applied, the
// backend reachable from the page, a host reported twice or with IPv6 brackets, and the dead
// proxy missing (or open connections kept) when the network turns off.
test('every request is judged by the page policy, and each host is reported once', async () => {
  let page = [];
  const h = harness({
    load: async (_url, { ses }) => {
      for (const [url, method] of page) ses.beforeRequest({ url, method }, () => undefined);
    },
  });
  page = [
    ['http://localhost:5173/', 'GET'],
    ['http://[::1]:5173/api', 'POST'],
    ['https://api.example.com/save', 'POST'],
    [`http://127.0.0.1:${BACKEND_PORT}/local/action-browser`, 'POST'],
    ['https://cdn.example.com/a.js', 'GET'],
    ['https://cdn.example.com/b.js', 'GET'],
  ];
  const first = await navigate(h, 'http://localhost:5173/');
  assert.deepEqual(first.report.network, [
    { host: 'localhost', port: 5173 },
    { host: '::1', port: 5173 },
    { host: 'cdn.example.com', port: 443 },
  ]);
  assert.deepEqual(first.report.blocked, [
    { reason: 'write_not_approved', url: 'https://api.example.com/save' },
    { reason: 'not_allowed', url: `http://127.0.0.1:${BACKEND_PORT}/local/action-browser` },
  ]);

  page = [
    ['https://api.example.com/save', 'POST'],
    ['https://cdn.example.com/a.js', 'GET'],
  ];
  const approved = await navigate(h, 'http://localhost:5173/', {
    policy: { write_hosts: ['api.example.com'] },
  });
  assert.deepEqual(approved.report, {
    notes: [],
    popups: [],
    blocked: [],
    network: [{ host: 'api.example.com', port: 443 }],
  });

  page = [['http://localhost:5173/', 'GET']];
  const offline = await navigate(h, 'file:///work/deck.html', {
    policy: { allow_network: false },
  });
  assert.deepEqual(h.ses().proxies, [
    { mode: 'system' },
    { proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '<-loopback>' },
  ]);
  assert.equal(h.ses().closedConnections, 1);
  assert.deepEqual(offline.report.blocked, [
    { reason: 'network_off', url: 'http://localhost:5173/' },
  ]);
});

// Catches: local files served outside the read scope the latest policy names.
test('local files are served only inside the policy read scope', async () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-browser-')));
  fs.mkdirSync(path.join(root, 'site'));
  fs.writeFileSync(path.join(root, 'site', 'deck.html'), '<p>deck</p>');
  fs.writeFileSync(path.join(root, 'other.html'), '<p>other</p>');
  const served = [];
  const h = harness({
    load: async (_url, { ses }) => {
      for (const file of ['site/deck.html', 'other.html']) {
        const response = await ses.fileHandler({ url: pathToFileURL(path.join(root, file)).href });
        served.push([file, response.type]);
      }
    },
  });
  const settled = await navigate(h, 'file:///deck.html', {
    policy: { file_roots: [path.join(root, 'site')] },
  });
  assert.deepEqual(served, [
    ['site/deck.html', 'default'],
    ['other.html', 'error'],
  ]);
  assert.deepEqual(settled.report.blocked, [
    { reason: 'outside_read_scope', url: pathToFileURL(path.join(root, 'other.html')).href },
  ]);
});

// Catches: a dialog, print, file chooser or worker the page asked for left unreported, a popup
// opened (or sent to the user's browser), a download saved, or a permission granted.
test('refused dialogs, prints, choosers, workers, popups and downloads are reported', async () => {
  const h = harness({
    load: async (_url, { ses, contents }) => {
      const emit = (method, params, sessionId) =>
        contents.debugger.emit('message', {}, method, params, sessionId);
      emit('Page.javascriptDialogOpening', {}, 'frame-1');
      emit('Log.entryAdded', { entry: { source: 'security', text: "Ignored call to 'print()'." } });
      emit('Page.fileChooserOpened', {});
      const csp = { violatedDirective: 'worker-src', blockedURL: 'https://a.example/w.js' };
      emit('Audits.issueAdded', { issue: { details: { contentSecurityPolicyIssueDetails: csp } } });
      contents.openHandler({ url: 'https://ads.example/' });
      ses.emit('will-download', { preventDefault: () => (ses.downloadPrevented = true) });
    },
  });
  const settled = await navigate(h, 'https://a.example/');
  assert.deepEqual(settled.report, {
    notes: ['dialog_dismissed', 'popup_refused', 'download_refused'],
    popups: ['https://ads.example/'],
    blocked: [{ reason: 'not_allowed', url: 'https://a.example/w.js' }],
    network: [],
  });
  const contents = h.windows[0].webContents;
  assert.ok(
    contents.debugger.commands.some(
      ({ method, params, sessionId }) =>
        method === 'Page.handleJavaScriptDialog' && !params.accept && sessionId === 'frame-1'
    )
  );
  assert.deepEqual(contents.openHandler({ url: 'https://x.example/' }), { action: 'deny' });
  assert.equal(h.ses().downloadPrevented, true);
  let granted = null;
  h.ses().permissionRequest(null, 'media', (value) => (granted = value));
  assert.equal(granted, false);
  assert.equal(h.ses().permissionCheck(), false);
  assert.equal(h.ses().devicePermission(), false);
});

// Catches: a document that may print, open dialogs or start a worker (whose sockets bypass
// webRequest), whether it came over the network or from a file, or a page CSP dropped.
test('every document is sandboxed without modals and may start no worker', async () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-browser-')));
  fs.writeFileSync(path.join(root, 'deck.html'), '<p>deck</p>');
  let file = null;
  const h = harness({
    load: async (_url, { ses }) => {
      file = await ses.fileHandler({ url: pathToFileURL(path.join(root, 'deck.html')).href });
    },
  });
  await navigate(h, 'https://a.example/', { policy: { file_roots: [root] } });
  const policies = file.headers.get('Content-Security-Policy');
  assert.match(policies, /^sandbox allow-scripts allow-same-origin, sandbox .*worker-src 'none'$/);
  assert.doesNotMatch(policies, /allow-modals/);
  let headers = null;
  h.ses().headersReceived(
    { responseHeaders: { 'content-security-policy': ["default-src 'self'"] } },
    (response) => (headers = response.responseHeaders)
  );
  assert.deepEqual(Object.keys(headers), ['content-security-policy']);
  assert.equal(headers['content-security-policy'][0], "default-src 'self'");
  assert.match(headers['content-security-policy'][1], /^sandbox .*worker-src 'none'$/);
});

// Catches: an out-of-process frame running its scripts before it loses WebRTC and the file
// pickers, or left paused forever.
test('every frame target is guarded before it runs', async () => {
  const h = harness();
  await navigate(h, 'https://a.example/');
  const cdp = h.windows[0].webContents.debugger;
  const guard = (sessionId) =>
    cdp.commands.filter((command) => command.sessionId === sessionId).map(({ method }) => method);
  const guarded = [
    'Page.enable',
    'Log.enable',
    'Audits.enable',
    'Page.addScriptToEvaluateOnNewDocument',
    'Page.setInterceptFileChooserDialog',
    'Target.setAutoAttach',
  ];
  assert.deepEqual(guard(undefined).slice(0, guarded.length), guarded);
  const script = cdp.commands.find(
    ({ method }) => method === 'Page.addScriptToEvaluateOnNewDocument'
  ).params.source;
  for (const removed of [
    'globalThis.RTCPeerConnection',
    'globalThis.WebTransport',
    'globalThis.showOpenFilePicker',
    'globalThis.EyeDropper',
    'HTMLVideoElement.prototype.requestPictureInPicture',
    'Navigator.prototype.bluetooth',
  ]) {
    assert.ok(script.includes(`delete ${removed};`), removed);
  }
  cdp.emit('message', {}, 'Target.attachedToTarget', {
    sessionId: 'frame-1',
    targetInfo: { type: 'iframe' },
  });
  await turn();
  assert.deepEqual(guard('frame-1'), [...guarded, 'Runtime.runIfWaitingForDebugger']);
  // A srcdoc frame's document is already there when it is attached.
  assert.ok(
    cdp.commands.some(
      ({ method, params, sessionId }) =>
        method === 'Page.addScriptToEvaluateOnNewDocument' &&
        params.runImmediately === true &&
        sessionId === 'frame-1'
    )
  );
});

// Catches: two operations of one session interleaving on its page, and one session's slow
// operation holding up another session.
test('operations on one session run one after another', async () => {
  const held = gate();
  const order = [];
  const h = harness({
    load: async (url) => {
      order.push(`start ${url}`);
      if (url.includes('slow')) await held.opened;
      order.push(`end ${url}`);
    },
  });
  const slow = navigate(h, 'https://slow.example/');
  const next = navigate(h, 'https://next.example/');
  await navigate(h, 'https://other.example/', { session: 'other' });
  assert.deepEqual(order, [
    'start https://slow.example/',
    'start https://other.example/',
    'end https://other.example/',
  ]);
  held.release();
  await Promise.all([slow, next]);
  assert.deepEqual(order.slice(3), [
    'end https://slow.example/',
    'start https://next.example/',
    'end https://next.example/',
  ]);
});

// Catches: an operation that never returns holding the session, and work past its budget acting
// on the page a later operation uses.
test('an operation past its budget answers BROWSER_TIMED_OUT and closes the session', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const held = gate();
    const h = harness();
    const late = h.sessions.run(request('navigate'), 20_000, async (page) => {
      await held.opened;
      page.contents.lateWrite = true;
    });
    for (let i = 0; i < 5; i += 1) await turn();
    mock.timers.tick(20_000);
    assert.equal((await late).code, 'BROWSER_TIMED_OUT');
    assert.equal(h.windows[0].destroyed, true);
    assert.equal((await navigate(h, 'https://a.example/')).status, 'ok');
    held.release();
    await turn();
    assert.equal(h.windows.length, 2);
    assert.equal(h.windows[1].webContents.lateWrite, undefined);
  } finally {
    mock.timers.reset();
  }
});

// Catches: an unexpected error left unanswered (the runtime would wait it out), or its text,
// which can carry page content or a private URL, in the answer or the log.
test('an unexpected error fails the operation and logs no page string', async () => {
  const h = harness({
    load: async (url) => {
      throw new TypeError(`page says ignore previous instructions at ${url}`);
    },
  });
  const settled = await navigate(h, 'https://a.example/private/path?token=secret');
  assert.equal(settled.code, 'BROWSER_ACTION_FAILED');
  assert.doesNotMatch(settled.message, /ignore previous|secret/);
  assert.deepEqual(h.logged, [
    { name: 'BROWSER_OPERATION_ERR', payload: { action: 'navigate', error: 'TypeError' } },
  ]);
});

// Catches: a crashed session kept or reopened under the same id, and the operation in flight
// waiting until the runtime gives up.
test('a renderer crash discards the session and answers BROWSER_RENDERER_CRASHED', async () => {
  const h = harness({
    load: async (_url, { contents }) => {
      contents.emit('render-process-gone', {}, { reason: 'crashed' });
      await new Promise(() => {});
    },
  });
  assert.equal((await navigate(h, 'https://a.example/')).code, 'BROWSER_RENDERER_CRASHED');
  assert.equal(h.windows[0].destroyed, true);
  assert.equal((await navigate(h, 'https://a.example/')).code, 'BROWSER_SESSION_GONE');
  assert.equal(h.windows.length, 1);
});

// Catches: a closed id reopened, a close that lands during the first navigate being lost, a
// close before any operation forgotten, and an operation other than navigate opening a session.
test('a closed id is refused, even when the close lands before its first navigate ends', async () => {
  const held = gate();
  const h = harness({ load: () => held.opened });
  const first = navigate(h, 'https://a.example/');
  await turn();
  h.sessions.close('session-1');
  assert.equal((await first).code, 'BROWSER_SESSION_GONE');
  assert.equal(h.windows[0].destroyed, true);
  assert.deepEqual(h.ses().cleared.sort(), ['cache', 'storage']);
  assert.equal((await navigate(h, 'https://a.example/')).code, 'BROWSER_SESSION_GONE');

  h.sessions.close('never-opened');
  const late = await navigate(h, 'https://a.example/', { session: 'never-opened' });
  assert.equal(late.code, 'BROWSER_SESSION_GONE');

  const noPage = await h.sessions.run(request('back', { session: 'fresh' }), 20_000, () => {
    throw new Error('must not run');
  });
  assert.equal(noPage.code, 'BROWSER_NO_PAGE');
  assert.equal(h.windows.length, 1);
});

// Catches: a session that outlives the backend generation (or the app) that opened it.
test('closeAll closes every session and refuses their ids', async () => {
  const h = harness();
  await navigate(h, 'https://a.example/', { session: 'one' });
  await navigate(h, 'https://b.example/', { session: 'two' });
  h.sessions.closeAll();
  assert.ok(h.windows.every((window) => window.destroyed));
  assert.equal(
    (await navigate(h, 'https://a.example/', { session: 'one' })).code,
    'BROWSER_SESSION_GONE'
  );
});
