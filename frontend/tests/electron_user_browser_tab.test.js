const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const { fakeBrowserElectron } = require('./helpers/browser_electron_fakes');

const DIST = path.join(__dirname, '../electron/dist/browser');
const BACKEND_PORT = 49152;
/** A page that never settles fails its test instead of hanging the run. */
const SETTLES = { timeout: 5_000 };

function load() {
  const { electron } = fakeBrowserElectron();
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    return request === 'electron' ? electron : originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require(`${DIST}/userBrowserTab.js`);
  } finally {
    Module._load = originalLoad;
  }
}
const { openGuardedTab } = load();

/**
 * A stand-in for the user's browser behind `UserBrowserConnection`: one CDP session per tab that
 * records commands and plays a page. `site[url]` lists the document's redirect hops after the
 * first and its subresources as `[url, method]`; each paused request waits for the tab's Fetch
 * answer. Going back restores the earlier page from the back-forward cache, which fires no load.
 */
function fakeUserBrowser({ site = {}, command = () => undefined } = {}) {
  const commands = [];
  const listeners = [];
  const answers = new Map();
  const history = [{ id: 0, url: 'about:blank', title: '' }];
  let current = 0;
  const state = { tabsClosed: 0, onPopup: null };
  let nextRequest = 0;
  const emit = (method, params, sessionId) =>
    listeners.forEach((listener) => listener({}, method, params, sessionId));
  const pause = (fields, sessionId) => {
    nextRequest += 1;
    const requestId = `r${nextRequest}`;
    const answered = new Promise((resolve) => answers.set(requestId, resolve));
    emit('Fetch.requestPaused', { requestId, frameId: 'main', ...fields }, sessionId);
    return answered;
  };
  async function loadPage(url) {
    const { redirects = [], resources = [], errorText } = site[url] ?? {};
    for (const hop of [url, ...redirects]) {
      const doc = { request: { url: hop, method: 'GET' }, resourceType: 'Document' };
      if ((await pause(doc)) === 'Fetch.failRequest') {
        return { errorText: 'net::ERR_BLOCKED_BY_CLIENT' };
      }
    }
    if (errorText !== undefined) return { errorText };
    for (const [resource, method] of resources) {
      await pause({ request: { url: resource, method }, resourceType: 'Fetch' });
    }
    current += 1;
    history.splice(current, Infinity, { id: current, url, title: `title of ${url}` });
    const loaderId = `loader-${nextRequest}`;
    emit('Page.frameNavigated', { frame: { id: 'main', loaderId }, type: 'Navigation' });
    emit('Page.loadEventFired', {});
    return { frameId: 'main', loaderId };
  }
  const cdp = {
    async sendCommand(method, params = {}, sessionId) {
      commands.push({ method, params, sessionId });
      const scripted = await command(method, params, sessionId);
      if (scripted !== undefined) return scripted;
      if (method === 'Fetch.continueRequest' || method === 'Fetch.failRequest') {
        answers.get(params.requestId)(method);
      }
      if (method === 'Page.getFrameTree') return { frameTree: { frame: { id: 'main' } } };
      if (method === 'Page.navigate') return loadPage(params.url);
      if (method === 'Page.getNavigationHistory')
        return { currentIndex: current, entries: history };
      if (method === 'Page.navigateToHistoryEntry') {
        current = history.findIndex((entry) => entry.id === params.entryId);
        setImmediate(() =>
          emit('Page.frameNavigated', {
            frame: { id: 'main', loaderId: `restored-${current}` },
            type: 'BackForwardCacheRestore',
          })
        );
      }
      return {};
    },
    on: (_event, listener) => listeners.push(listener),
  };
  const connection = {
    async openTab(onPopup) {
      state.onPopup = onPopup;
      return { cdp, close: async () => (state.tabsClosed += 1) };
    },
    closed: new Promise(() => undefined),
    close: () => undefined,
  };
  const sent = (method, sessionId) =>
    commands.filter((c) => c.method === method && c.sessionId === sessionId);
  return { commands, sent, emit, pause, state, connection };
}

function tabContext(fields = {}) {
  return {
    readHosts: ['a.example'],
    writeHosts: [],
    backendPort: BACKEND_PORT,
    contacted: [],
    unreported: [],
    notes: new Set(),
    popups: [],
    blocked: [],
    ...fields,
  };
}

async function opened(options) {
  const browser = fakeUserBrowser(options);
  const context = tabContext();
  const tab = await openGuardedTab(context, browser.connection, new AbortController().signal, null);
  return { ...browser, context, tab };
}

// Catches: the signed-in tab opening an unapproved site (directly or by a redirect from an
// approved one), reaching a local server or the backend, or a refused main-frame load reported
// as a failed navigation instead of a blocked request.
test(
  'every request of the tab is judged in Fetch, each redirect hop included',
  SETTLES,
  async () => {
    const h = await opened({
      site: {
        'https://a.example/': {
          resources: [
            ['https://cdn.example/app.js', 'GET'],
            ['http://localhost:5173/', 'GET'],
            [`http://127.0.0.1:${BACKEND_PORT}/local/action-browser`, 'GET'],
          ],
        },
        'https://a.example/out': { redirects: ['https://b.example/landing'] },
      },
    });
    await h.tab.page.navigate('https://a.example/');
    assert.deepEqual(await h.tab.location(), {
      url: 'https://a.example/',
      title: 'title of https://a.example/',
    });
    assert.deepEqual(h.context.blocked, [
      { reason: 'not_allowed', url: 'http://localhost:5173/' },
      { reason: 'not_allowed', url: `http://127.0.0.1:${BACKEND_PORT}/local/action-browser` },
    ]);
    assert.deepEqual(h.context.unreported, [
      { host: 'a.example', port: 443 },
      { host: 'cdn.example', port: 443 },
    ]);
    assert.equal(h.sent('Fetch.failRequest').length, 2);

    h.context.blocked = [];
    await h.tab.page.navigate('https://a.example/out');
    await h.tab.page.navigate('https://b.example/');
    assert.deepEqual(h.context.blocked, [
      { reason: 'read_not_approved', url: 'https://b.example/landing' },
      { reason: 'read_not_approved', url: 'https://b.example/' },
    ]);
    // Approved for the run later: the session hands the tab the latest hosts.
    h.context.blocked = [];
    h.context.readHosts = ['a.example', 'b.example'];
    await h.tab.page.navigate('https://b.example/');
    assert.deepEqual(h.context.blocked, []);
    assert.equal((await h.tab.location()).url, 'https://b.example/');
  }
);

// Catches: a write from the user's real account sent to a host the run was not approved to
// write to, or an approved one refused.
test("the tab's writes reach only the run's write hosts", SETTLES, async () => {
  const h = await opened({
    site: { 'https://a.example/': { resources: [['https://api.example.com/save', 'POST']] } },
  });
  await h.tab.page.navigate('https://a.example/');
  assert.deepEqual(h.context.blocked, [
    { reason: 'write_not_approved', url: 'https://api.example.com/save' },
  ]);
  h.context.blocked = [];
  h.context.writeHosts = ['api.example.com'];
  await h.tab.page.navigate('https://a.example/');
  assert.deepEqual(h.context.blocked, []);
  assert.ok(h.context.unreported.some((host) => host.host === 'api.example.com'));
});

// Catches: a load that failed on its own answered as success, and a back that never settles on
// a page restored from the back-forward cache or is tried with no earlier page.
test('a failed load names its error, and back settles on a restored page', SETTLES, async () => {
  const h = await opened({ site: { 'https://a.example/down': { errorText: 'net::ERR_FAILED' } } });
  await assert.rejects(h.tab.page.back(), { code: 'BROWSER_ACTION_FAILED' });
  await h.tab.page.navigate('https://a.example/');
  await h.tab.page.navigate('https://a.example/next');
  await h.tab.page.back();
  assert.equal((await h.tab.location()).url, 'https://a.example/');
  await assert.rejects(h.tab.page.navigate('https://a.example/down'), {
    code: 'BROWSER_NAVIGATION_FAILED',
    message: 'The page did not load (net::ERR_FAILED).',
  });
});

const GUARDS = [
  'Page.addScriptToEvaluateOnNewDocument',
  'Page.setInterceptFileChooserDialog',
  'Network.enable',
  'Network.setBypassServiceWorker',
  'Fetch.enable',
];
// Chrome ignores the service worker bypass until Network is enabled in that session.
const bypassesServiceWorkers = (methods) =>
  methods.includes('Network.enable') &&
  methods.indexOf('Network.enable') < methods.indexOf('Network.setBypassServiceWorker');

// Catches: a page or one of its frames or workers running before its guards are in place (so it
// can open a WebSocket, or have the site's installed service worker send requests Fetch never
// sees), a frame resumed after its guard failed, or a worker left paused forever by a guard
// command Chrome lacks on workers.
test(
  'the tab and every frame and worker it attaches are guarded before they run',
  SETTLES,
  async () => {
    const h = await opened({
      command: (method, _params, sessionId) => {
        if (sessionId === 'frame-bad' && method === 'Fetch.enable') throw new Error('refused');
        // As in Chrome: a dedicated worker has no Fetch domain.
        if (sessionId === 'worker-1' && method.startsWith('Fetch.')) {
          throw new Error("'Fetch.enable' wasn't found");
        }
        return undefined;
      },
    });
    await h.tab.page.navigate('https://a.example/');
    const methods = h.commands.filter((c) => c.sessionId === undefined).map((c) => c.method);
    const navigated = methods.indexOf('Page.navigate');
    for (const guard of GUARDS) assert.ok(methods.indexOf(guard) < navigated, guard);
    assert.ok(bypassesServiceWorkers(methods));
    const script = h.sent('Page.addScriptToEvaluateOnNewDocument')[0].params.source;
    for (const name of ['WebSocket', 'SharedWorker', 'serviceWorker', 'RTCPeerConnection']) {
      assert.match(script, new RegExp(name));
    }

    const attach = (sessionId, type) =>
      h.emit('Target.attachedToTarget', { sessionId, targetInfo: { type } });
    attach('frame-1', 'iframe');
    attach('worker-1', 'worker');
    attach('frame-bad', 'iframe');
    await new Promise((resolve) => setImmediate(resolve));
    const frame = h.commands.filter((c) => c.sessionId === 'frame-1').map((c) => c.method);
    for (const guard of GUARDS) assert.ok(frame.includes(guard), guard);
    assert.ok(bypassesServiceWorkers(frame));
    assert.equal(frame.at(-1), 'Runtime.runIfWaitingForDebugger');
    assert.equal(
      h.sent('Page.addScriptToEvaluateOnNewDocument', 'frame-1')[0].params.source,
      script
    );
    const worker = h.commands.filter((c) => c.sessionId === 'worker-1');
    assert.ok(bypassesServiceWorkers(worker.map((c) => c.method)));
    assert.match(
      worker.find((c) => c.method === 'Runtime.evaluate').params.expression,
      /WebSocket/
    );
    assert.equal(worker.at(-1).method, 'Runtime.runIfWaitingForDebugger');
    assert.equal(h.sent('Runtime.runIfWaitingForDebugger', 'frame-bad').length, 0);

    // A frame's requests are judged and answered in its own session.
    const answered = h.pause(
      { request: { url: 'http://localhost:5173/', method: 'GET' }, resourceType: 'Script' },
      'frame-1'
    );
    assert.equal(await answered, 'Fetch.failRequest');
    assert.equal(h.sent('Fetch.failRequest', 'frame-1').length, 1);
  }
);

// Catches: an unguarded tab handed out when a guard cannot be installed, or left open.
test('a tab that cannot be guarded is closed and never handed out', SETTLES, async () => {
  const browser = fakeUserBrowser({
    command: (method) => {
      if (method === 'Fetch.enable') throw new Error('refused');
      return undefined;
    },
  });
  await assert.rejects(
    openGuardedTab(tabContext(), browser.connection, new AbortController().signal, null)
  );
  assert.equal(browser.state.tabsClosed, 1);
  assert.equal(browser.sent('Page.navigate').length, 0);
});

// Catches: a download saved into the user's Downloads, a dialog left open in the user's browser,
// or a popup the tab opened left unreported.
test('downloads are refused, dialogs dismissed and popups reported', SETTLES, async () => {
  const h = await opened();
  const download = h.pause({
    request: { url: 'https://a.example/report.pdf', method: 'GET' },
    resourceType: 'Document',
    responseStatusCode: 200,
    responseHeaders: [{ name: 'Content-Disposition', value: 'attachment; filename=r.pdf' }],
  });
  assert.equal(await download, 'Fetch.failRequest');
  // A body the browser would not display is saved as a download, with or without the header;
  // a page, a redirect and a failed load continue.
  const answerTo = (fields) =>
    h.pause({
      request: { url: 'https://a.example/file', method: 'GET' },
      resourceType: 'Document',
      ...fields,
    });
  const typed = (status, type) => ({
    responseStatusCode: status,
    responseHeaders: type === null ? [] : [{ name: 'content-type', value: type }],
  });
  assert.equal(await answerTo(typed(200, 'application/octet-stream')), 'Fetch.failRequest');
  assert.equal(await answerTo(typed(200, null)), 'Fetch.failRequest');
  assert.equal(await answerTo(typed(200, 'Text/HTML; charset=utf-8')), 'Fetch.continueRequest');
  assert.equal(await answerTo(typed(200, 'application/pdf')), 'Fetch.continueRequest');
  assert.equal(await answerTo(typed(302, null)), 'Fetch.continueRequest');
  assert.equal(
    await answerTo({ responseErrorReason: 'ConnectionRefused' }),
    'Fetch.continueRequest'
  );
  h.emit('Page.javascriptDialogOpening', { type: 'alert', message: 'hi' });
  h.state.onPopup('https://pop.example/');
  assert.deepEqual([...h.context.notes].sort(), [
    'dialog_dismissed',
    'download_refused',
    'popup_refused',
  ]);
  assert.deepEqual(h.context.popups, ['https://pop.example/']);
  assert.deepEqual(h.sent('Page.handleJavaScriptDialog')[0].params, { accept: false });
});
