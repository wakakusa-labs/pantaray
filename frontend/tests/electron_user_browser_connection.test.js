const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const { WebSocketServer } = require('ws');
const { fakeBrowserElectron } = require('./helpers/browser_electron_fakes');

const DIST = path.join(__dirname, '../electron/dist/browser');
const BROWSER_PATH = '/devtools/browser/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0';

function load() {
  const fake = fakeBrowserElectron();
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    return request === 'electron'
      ? fake.electron
      : originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require(`${DIST}/userBrowserConnection.js`);
  } finally {
    Module._load = originalLoad;
  }
}
const { CdpCommandError, connectUserBrowser } = load();

/**
 * A stand-in for the browser's debugging endpoint: `mode` holds every handshake ('hold'),
 * answers it with an HTTP status (a number), or accepts it and hands the socket to `mode`.
 */
async function browserEndpoint(t, mode) {
  const upgrades = [];
  const server = http.createServer();
  const wss = new WebSocketServer({ noServer: true });
  server.on('upgrade', (request, socket, head) => {
    // The server keeps its side half open, so the client's close shows as the stream's end.
    const upgrade = { request, socket, closed: new Promise((r) => socket.once('end', r)) };
    upgrades.push(upgrade);
    if (mode === 'hold') return;
    if (typeof mode === 'number') {
      socket.end(`HTTP/1.1 ${mode} Refused\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
      return;
    }
    wss.handleUpgrade(request, socket, head, mode);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    for (const { socket } of upgrades) socket.destroy();
    for (const client of wss.clients) client.terminate();
    await new Promise((resolve) => server.close(resolve));
  });
  return { port: server.address().port, upgrades };
}

/** `~/Library/Application Support` with Chrome's user-data-dir as a running Chrome leaves it. */
function applicationSupport(t, { port, lockPid = process.pid, portFile } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-user-browser-'));
  t.after(() => {
    fs.chmodSync(path.join(root, 'Google/Chrome'), 0o700);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const userDataDir = path.join(root, 'Google/Chrome');
  fs.mkdirSync(userDataDir, { recursive: true });
  if (lockPid !== null)
    fs.symlinkSync(`host.local-${lockPid}`, path.join(userDataDir, 'SingletonLock'));
  const text = portFile ?? (port === undefined ? null : `${port}\n${BROWSER_PATH}\n`);
  if (text !== null) fs.writeFileSync(path.join(userDataDir, 'DevToolsActivePort'), text);
  return { root, userDataDir };
}

/** lsof lists `lsof` (exiting 1 when it lists nothing); codesign exits `codesign`. */
function fakeRunner({ lsof = 'p4242\nf20\n', codesign = 0 } = {}) {
  const calls = [];
  const runner = async (file, args) => {
    calls.push({ file, args });
    if (file === '/usr/sbin/lsof') return { exitCode: lsof === '' ? 1 : 0, stdout: lsof };
    return { exitCode: codesign, stdout: '' };
  };
  return { runner, calls };
}

function fakeNotice() {
  const notice = { shown: [], closed: 0 };
  notice.show = (browser) => {
    notice.shown.push(browser.name);
    return () => (notice.closed += 1);
  };
  return notice;
}

function connect(root, { runner = fakeRunner().runner, notice = fakeNotice(), signal } = {}) {
  return connectUserBrowser({
    browser: 'chrome',
    applicationSupportDir: root,
    runner,
    showNotice: notice.show,
    signal: signal ?? new AbortController().signal,
  });
}

const failsWith = (code, message) => (error) => {
  assert.equal(error.code, code);
  if (message !== undefined) assert.match(error.message, message);
  return true;
};

test('the wait for Allow has no time limit, and stopping it closes the socket and the notice', async (t) => {
  const endpoint = await browserEndpoint(t, 'hold');
  const { root } = applicationSupport(t, { port: endpoint.port });
  const notice = fakeNotice();
  const controller = new AbortController();
  let settled = false;
  const connecting = connect(root, { notice, signal: controller.signal });
  connecting.then(
    () => (settled = true),
    () => (settled = true)
  );

  while (endpoint.upgrades.length === 0) await new Promise((r) => setTimeout(r, 10));
  const [{ request, closed }] = endpoint.upgrades;
  assert.equal(request.url, BROWSER_PATH);
  // The browser refuses at once a handshake that carries an Origin.
  assert.equal(request.headers.origin, undefined);
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(settled, false);
  assert.deepEqual(notice.shown, ['Google Chrome']);
  assert.equal(notice.closed, 0);

  const reason = new Error('the run stopped');
  controller.abort(reason);
  await assert.rejects(connecting, (error) => error === reason);
  await closed;
  assert.equal(notice.closed, 1);
});

test("the user's refusal is told apart from any other failed handshake", async (t) => {
  for (const [status, code] of [
    [403, 'BROWSER_USER_DENIED'],
    [500, 'BROWSER_USER_UNAVAILABLE'],
  ]) {
    const endpoint = await browserEndpoint(t, status);
    const { root } = applicationSupport(t, { port: endpoint.port });
    const notice = fakeNotice();
    await assert.rejects(connect(root, { notice }), failsWith(code, /Google Chrome/));
    assert.equal(notice.closed, 1);
  }
});

test('each state of the browser and its files gets the code the user can act on', async (t) => {
  const deadPid = spawnSync(process.execPath, ['-e', '']).pid;
  const cases = [
    ['no lock', { lockPid: null, port: 9222 }, {}, 'BROWSER_USER_NOT_RUNNING', /not running/],
    ['lock of a quit browser', { lockPid: deadPid, port: 9222 }, {}, 'BROWSER_USER_NOT_RUNNING'],
    ['no port file', {}, {}, 'BROWSER_USER_DEBUGGING_OFF', /chrome:\/\/inspect\/#remote-debugging/],
    ['malformed port file', { portFile: 'x\n/elsewhere\n' }, {}, 'BROWSER_USER_UNAVAILABLE'],
    ['debugging turned off, file left', { port: 9222 }, { lsof: '' }, 'BROWSER_USER_DEBUGGING_OFF'],
    ['two listeners', { port: 9222 }, { lsof: 'p1\nf3\np2\nf4\n' }, 'BROWSER_USER_UNAVAILABLE'],
  ];
  for (const [name, files, runs, code, message] of cases) {
    const { root } = applicationSupport(t, files);
    await assert.rejects(
      connect(root, { runner: fakeRunner(runs).runner }),
      failsWith(code, message),
      name
    );
  }

  const unreadable = applicationSupport(t, { port: 9222 });
  fs.chmodSync(path.join(unreadable.userDataDir, 'DevToolsActivePort'), 0o000);
  await assert.rejects(
    connect(unreadable.root),
    failsWith('BROWSER_USER_FILE_BLOCKED', /Files & Folders/)
  );
  const closedDir = applicationSupport(t, { port: 9222 });
  fs.chmodSync(closedDir.userDataDir, 0o000);
  await assert.rejects(connect(closedDir.root), failsWith('BROWSER_USER_FILE_BLOCKED'));
});

test('a listener that is not the signed browser never gets the socket', async (t) => {
  const endpoint = await browserEndpoint(t, 'hold');
  const { root } = applicationSupport(t, { port: endpoint.port });
  const { runner, calls } = fakeRunner({ codesign: 3 });
  const notice = fakeNotice();
  await assert.rejects(connect(root, { runner, notice }), failsWith('BROWSER_USER_UNAVAILABLE'));
  assert.equal(endpoint.upgrades.length, 0);
  assert.deepEqual(notice.shown, []);
  assert.ok(calls[0].args.includes(`-iTCP@127.0.0.1:${endpoint.port}`));
  const [flag, option, requirement, pid] = calls[1].args;
  assert.deepEqual([flag, option, pid], ['--verify', '-R', '4242']);
  assert.match(requirement, /leaf\[subject\.OU\] = "EQHXZ8M8AV"/);
  assert.match(requirement, /"com\.google\.Chrome" or identifier "com\.google\.Chrome\.helper"/);
});

test('a tab reaches only its own sessions, hears only them, and has its popups closed', async (t) => {
  const received = [];
  let browserSide;
  const endpoint = await browserEndpoint(t, (socket) => {
    browserSide = socket;
    socket.on('message', (data) => {
      const message = JSON.parse(data);
      received.push(message);
      const result =
        message.method === 'Target.createTarget'
          ? { targetId: 'own' }
          : message.method === 'Target.attachToTarget'
            ? { sessionId: 'own-page' }
            : {};
      socket.send(JSON.stringify({ id: message.id, result }));
    });
  });
  const { root } = applicationSupport(t, { port: endpoint.port });
  const connection = await connect(root);
  const popups = [];
  const tab = await connection.openTab((url) => popups.push(url));
  const events = [];
  tab.cdp.on('message', (_event, method, _params, sessionId) => events.push([method, sessionId]));
  const refused = (method, params, sessionId) =>
    assert.rejects(tab.cdp.sendCommand(method, params, sessionId), CdpCommandError, method);

  // Chromium routes Target commands by the target they name, whichever session carries them.
  await refused('Target.getTargets', {});
  await refused('Target.attachToTarget', { targetId: 'users-tab', flatten: true });
  await refused('Target.closeTarget', { targetId: 'users-tab' });
  await refused('Storage.getCookies', {});
  await refused('Network.getAllCookies', {});
  await refused('Browser.setDownloadBehavior', { behavior: 'allow' });
  // An empty session id is the browser itself in Chromium.
  await refused('Page.navigate', { url: 'https://example.com' }, '');
  await refused('Page.navigate', { url: 'https://example.com' }, 'users-session');
  await tab.cdp.sendCommand('Page.navigate', { url: 'https://example.com' });

  const send = (method, params, sessionId) =>
    browserSide.send(JSON.stringify({ method, params, sessionId }));
  send('Target.targetCreated', {
    targetInfo: { targetId: 'users-tab', url: 'https://mail.example' },
  });
  send('Target.targetInfoChanged', { targetInfo: { targetId: 'users-tab' } });
  send('Runtime.consoleAPICalled', { type: 'log' }, 'users-session');
  send('Target.targetCreated', {
    targetInfo: { targetId: 'popup', openerId: 'own', url: 'https://popup.example/' },
  });
  send('Page.loadEventFired', { timestamp: 1 }, 'own-page');
  send(
    'Target.attachedToTarget',
    { sessionId: 'own-frame', targetInfo: { targetId: 'frame', type: 'iframe' } },
    'own-page'
  );
  send('Page.frameNavigated', {}, 'own-frame');
  while (events.length < 3) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events, [
    ['Page.loadEventFired', undefined],
    ['Target.attachedToTarget', undefined],
    ['Page.frameNavigated', 'own-frame'],
  ]);
  assert.deepEqual(popups, ['https://popup.example/']);
  await tab.cdp.sendCommand('Runtime.runIfWaitingForDebugger', {}, 'own-frame');
  await tab.close();

  assert.deepEqual(
    received.map(({ method, params, sessionId }) => [method, params.targetId, sessionId]),
    [
      ['Target.setDiscoverTargets', undefined, undefined],
      ['Target.createTarget', undefined, undefined],
      ['Target.attachToTarget', 'own', undefined],
      ['Page.navigate', undefined, 'own-page'],
      ['Target.closeTarget', 'popup', undefined],
      ['Runtime.runIfWaitingForDebugger', undefined, 'own-frame'],
      ['Target.closeTarget', 'own', undefined],
    ]
  );
  assert.deepEqual(received[1].params, { url: 'about:blank', hidden: true, background: true });

  connection.close();
  await connection.closed;
  await assert.rejects(tab.cdp.sendCommand('Page.enable', {}), /closed/);
});

test('stopping the run ends a check that never answers', async (t) => {
  const { root } = applicationSupport(t, { port: 9222 });
  const signals = [];
  const runner = (_file, _args, signal) => {
    signals.push(signal);
    return new Promise(() => undefined);
  };
  const controller = new AbortController();
  const connecting = connect(root, { runner, signal: controller.signal });
  while (signals.length === 0) await new Promise((r) => setTimeout(r, 10));
  const reason = new Error('the run stopped');
  controller.abort(reason);
  await assert.rejects(connecting, (error) => error === reason);
  // The runner gets the signal, so the real one kills its child.
  assert.equal(signals[0], controller.signal);
});
