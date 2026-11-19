const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const { fakeBrowserElectron } = require('./helpers/browser_electron_fakes');

const DIST = path.join(__dirname, '../electron/dist/browser');
const BACKEND_PORT = 49152;
/** An operation that never settles fails its test instead of hanging the run. */
const SETTLES = { timeout: 5_000 };

function load(electron) {
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    return request === 'electron' ? electron : originalLoad.call(this, request, parent, isMain);
  };
  try {
    return {
      ...require(`${DIST}/browserSession.js`),
      ...require(`${DIST}/browserResponder.js`),
      ...require(`${DIST}/userBrowserSession.js`),
    };
  } finally {
    Module._load = originalLoad;
  }
}
const app = fakeBrowserElectron();
const { createBrowserSessions, createBrowserResponder, createUserBrowserSessions } = load(
  app.electron
);

/**
 * A stand-in for the user's browser behind `UserBrowserConnection`: one CDP session per tab that
 * records commands and plays a page. `site[url]` lists the document's redirect hops after the
 * first and its subresources as `[url, method]`; each paused request waits for the session's
 * Fetch answer.
 */
function fakeUserBrowser({ site = {}, command = () => undefined, closeTab = async () => {} } = {}) {
  const commands = [];
  const listeners = [];
  const answers = new Map();
  const history = [{ id: 0, url: 'about:blank', title: '' }];
  const state = { tabsOpened: 0, tabsClosed: 0, connectionClosed: false, onPopup: null };
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
  async function load(url) {
    const { redirects = [], resources = [] } = site[url] ?? {};
    for (const hop of [url, ...redirects]) {
      const doc = { request: { url: hop, method: 'GET' }, resourceType: 'Document' };
      if ((await pause(doc)) === 'Fetch.failRequest') {
        return { errorText: 'net::ERR_BLOCKED_BY_CLIENT' };
      }
    }
    for (const [resource, method] of resources) {
      await pause({ request: { url: resource, method }, resourceType: 'Fetch' });
    }
    history.push({ id: history.length, url, title: `title of ${url}` });
    const loaderId = `loader-${history.length}`;
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
      if (method === 'Page.navigate') return load(params.url);
      if (method === 'Page.getNavigationHistory') {
        return { currentIndex: history.length - 1, entries: history };
      }
      return {};
    },
    on: (_event, listener) => listeners.push(listener),
  };
  let closeConnection = () => undefined;
  const connection = {
    // As the real connection does, sends nothing for a tab whose signal aborted.
    async openTab(onPopup, signal) {
      state.tabsOpened += 1;
      state.onPopup = onPopup;
      return {
        cdp: {
          ...cdp,
          sendCommand: (...args) =>
            signal.aborted ? Promise.reject(signal.reason) : cdp.sendCommand(...args),
        },
        close: () => {
          state.tabsClosed += 1;
          return closeTab();
        },
      };
    },
    closed: new Promise((resolve) => (closeConnection = resolve)),
    close() {
      state.connectionClosed = true;
      closeConnection();
    },
  };
  const sent = (method, sessionId) =>
    commands.filter((c) => c.method === method && c.sessionId === sessionId);
  return { commands, sent, emit, pause, state, connection };
}

function harness(options = {}) {
  const browser = fakeUserBrowser(options);
  const userSessions = createUserBrowserSessions({
    connect: options.connect ?? (async () => browser.connection),
    getBackendPort: () => BACKEND_PORT,
  });
  const answers = [];
  const respond = createBrowserResponder({
    sessions: createBrowserSessions({ getBackendPort: () => BACKEND_PORT }),
    userSessions,
    localArtifactRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-user-browser-')),
    postAnswer: async (body) => answers.push(body),
  });
  let nextOperation = 0;
  const run = async (
    operation,
    { readHosts = ['a.example'], writeHosts = [], app = false } = {}
  ) => {
    nextOperation += 1;
    const id = `op-${nextOperation}`;
    await respond({
      operation_request_id: id,
      session_id: 'session-1',
      operation,
      policy: {
        allow_network: true,
        file_roots: [],
        private_roots: [],
        private_root_exceptions: [],
        secret_paths: [],
        write_hosts: writeHosts,
      },
      user_browser: app ? null : { browser: 'chrome', read_hosts: readHosts },
    });
    return answers.find((answer) => answer.operation_request_id === id).result;
  };
  const navigate = (url, options) =>
    run({ action: 'navigate', target: { kind: 'web', url } }, options);
  return { ...browser, userSessions, run, navigate };
}

// Catches: the tab judged by stale or missing hosts: an unapproved site opened, a host the run
// approved since refused, writes judged without the request's write hosts, or a refused
// main-frame load answered as a failed navigation instead of a blocked request.
test(
  "each answer reports the tab's refusals under the request's latest hosts",
  SETTLES,
  async () => {
    const h = harness({
      site: {
        'https://a.example/': { resources: [['https://api.example.com/save', 'POST']] },
        'https://a.example/out': { redirects: ['https://b.example/landing'] },
      },
    });
    const first = await h.navigate('https://a.example/');
    assert.equal(first.status, 'ok');
    assert.equal(first.page_data.url, 'https://a.example/');
    assert.equal(first.opened_host, 'a.example');
    assert.deepEqual(first.page_data.blocked, [
      { reason: 'write_not_approved', url: 'https://api.example.com/save' },
    ]);
    assert.deepEqual(first.network, [{ host: 'a.example', port: 443 }]);

    const redirected = await h.navigate('https://a.example/out');
    assert.equal(redirected.status, 'ok');
    assert.deepEqual(redirected.page_data.blocked, [
      { reason: 'read_not_approved', url: 'https://b.example/landing' },
    ]);
    const hosts = { readHosts: ['a.example', 'b.example'], writeHosts: ['api.example.com'] };
    const approved = await h.navigate('https://a.example/', hosts);
    assert.deepEqual(approved.page_data.blocked, []);
    assert.deepEqual(approved.network, [{ host: 'api.example.com', port: 443 }]);
    assert.deepEqual((await h.navigate('https://a.example/out', hosts)).page_data.blocked, []);
  }
);

// Catches: a user-browser request served by the app's hidden page (its partition, window or
// print), which is not signed in as the user and follows other rules.
test("a user-browser request never touches the app's page", SETTLES, async () => {
  const h = harness();
  const windowsBefore = app.windows.length;
  const partitionsBefore = app.partitions.size;
  assert.equal((await h.navigate('https://a.example/')).status, 'ok');
  const printed = await h.run({ action: 'print' });
  assert.equal(printed.code, 'BROWSER_ACTION_FAILED');
  const file = await h.run({ action: 'navigate', target: { kind: 'file', path: '/etc/hosts' } });
  assert.equal(file.code, 'BROWSER_ACTION_FAILED');
  assert.equal(app.windows.length, windowsBefore);
  assert.equal(app.partitions.size, partitionsBefore);
  assert.ok(!h.sent('Page.navigate').some((c) => c.params.url.startsWith('file:')));
});

// Catches: a close (the run stopped) that leaves the wait for the user's Allow running, so the
// connection opens later with no run to close it, or a later operation served anyway.
test('closing the session aborts a connection still waiting for the user', SETTLES, async () => {
  let signal;
  const h = harness({
    connect: (_id, abortSignal) => {
      signal = abortSignal;
      return new Promise((_resolve, reject) =>
        abortSignal.addEventListener('abort', () => reject(abortSignal.reason))
      );
    },
  });
  const pending = h.navigate('https://a.example/');
  await new Promise((resolve) => setImmediate(resolve));
  h.userSessions.close('session-1');
  assert.equal(signal.aborted, true);
  assert.equal((await pending).code, 'BROWSER_SESSION_GONE');
  assert.equal((await h.navigate('https://a.example/')).code, 'BROWSER_SESSION_GONE');
  assert.equal(h.state.tabsOpened, 0);
});

// Catches: the user's time on the Allow prompt counted against the operation's budget, a stalled
// tab left running past it, or a timeout that drops the allowed connection, so the user is asked
// again for the next page.
test(
  'the budget starts once the connection is open, and a timeout closes only the tab',
  SETTLES,
  async () => {
    let stall = false;
    const browser = fakeUserBrowser({
      // A page takes a while to load; a stalled one never does.
      command: (method) =>
        method === 'Page.navigate'
          ? new Promise((resolve) => !stall && setTimeout(resolve, 20))
          : undefined,
    });
    let connects = 0;
    const sessions = createUserBrowserSessions({
      connect: async () => {
        connects += 1;
        await new Promise((resolve) => setTimeout(resolve, 150));
        return browser.connection;
      },
      getBackendPort: () => BACKEND_PORT,
    });
    const request = (url) => ({
      session_id: 's',
      operation: { action: 'navigate', target: { kind: 'web', url } },
      policy: { allow_network: true, write_hosts: [] },
    });
    const target = { browser: 'chrome', read_hosts: ['a.example'] };
    const go = (page) => page.navigate('https://a.example/');
    assert.equal((await sessions.run(request('https://a.example/'), target, 50, go)).status, 'ok');
    stall = true;
    const timedOut = await sessions.run(request('https://a.example/'), target, 50, go);
    assert.equal(timedOut.code, 'BROWSER_TIMED_OUT');
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(browser.state.tabsClosed, 1);
    assert.equal(browser.state.connectionClosed, false);
    stall = false;
    assert.equal((await sessions.run(request('https://a.example/'), target, 50, go)).status, 'ok');
    assert.deepEqual([connects, browser.state.tabsOpened], [1, 2]);
    sessions.close('s');
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(browser.state.connectionClosed, true);
  }
);

// Catches: an unguarded tab used when a guard cannot be installed, or a session kept open on it.
test('a tab that cannot be guarded is closed and the session ends', SETTLES, async () => {
  const h = harness({
    command: (method) => {
      if (method === 'Fetch.enable') throw new Error('refused');
      return undefined;
    },
  });
  const failed = await h.navigate('https://a.example/');
  assert.equal(failed.code, 'BROWSER_USER_UNAVAILABLE');
  assert.equal(failed.message, 'Google Chrome could not be used.');
  assert.equal(h.sent('Page.navigate').length, 0);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual([h.state.tabsClosed > 0, h.state.connectionClosed], [true, true]);
  assert.equal((await h.navigate('https://a.example/')).code, 'BROWSER_SESSION_GONE');
});

/** Settles once `method` was sent and holds its answer until `release` is called. */
function heldCommand(method) {
  let release = () => undefined;
  let onSent = () => undefined;
  const sent = new Promise((resolve) => (onSent = resolve));
  const command = (name, _params, sessionId) => {
    if (name !== method || sessionId !== undefined) return undefined;
    onSent();
    return new Promise((resolve) => (release = () => resolve({})));
  };
  return { command, sent, release: () => release() };
}

// Catches: a close (the run stopped) while the tab's guards are being installed that lets the
// operation go on once they finish, so a finished run drives the user's browser.
test('a close while the tab is being guarded sends it nothing more', SETTLES, async () => {
  const held = heldCommand('Fetch.enable');
  const h = harness({ command: held.command });
  const pending = h.navigate('https://a.example/');
  await held.sent;
  h.userSessions.close('session-1');
  assert.equal(h.state.connectionClosed, true);
  assert.equal((await pending).code, 'BROWSER_SESSION_GONE');
  held.release();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(h.sent('Page.navigate').length, 0);
  assert.equal(h.sent('Runtime.enable').length, 0);
});

// Catches: a close that waits for the browser to close the tab before closing the connection,
// so a browser that never answers keeps the run's connection open.
test('a close never waits on the browser to close the connection', SETTLES, async () => {
  const h = harness({ closeTab: () => new Promise(() => {}) });
  assert.equal((await h.navigate('https://a.example/')).status, 'ok');
  h.userSessions.closeAll();
  assert.equal(h.state.connectionClosed, true);
});

// Catches: work started on a tab whose guards finished only after the operation's budget ran
// out, acting on the user's browser for an operation already answered as timed out.
test('an operation past its budget while the tab is guarded never runs', SETTLES, async () => {
  const held = heldCommand('Fetch.enable');
  const browser = fakeUserBrowser({ command: held.command });
  const sessions = createUserBrowserSessions({
    connect: async () => browser.connection,
    getBackendPort: () => BACKEND_PORT,
  });
  let worked = false;
  const settled = await sessions.run(
    {
      session_id: 's',
      operation: { action: 'navigate', target: { kind: 'web', url: 'https://a.example/' } },
      policy: { allow_network: true, write_hosts: [] },
    },
    { browser: 'chrome', read_hosts: ['a.example'] },
    20,
    async () => (worked = true)
  );
  assert.equal(settled.code, 'BROWSER_TIMED_OUT');
  held.release();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(worked, false);
  assert.equal(browser.sent('Runtime.enable').length, 0);
  assert.deepEqual([browser.state.tabsClosed, browser.state.connectionClosed], [1, false]);
});
