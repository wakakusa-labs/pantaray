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
const { bringUserBrowserForward } = require(`${DIST}/userBrowserForward.js`);

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
function applicationSupport(t, { port, lockPid = process.pid, portFile, blocked } = {}) {
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
  // macOS privacy settings answer EPERM; a mode of 0 answers EACCES, which reads the same.
  if (blocked !== undefined) fs.chmodSync(path.join(userDataDir, blocked), 0o000);
  return { root, userDataDir };
}

/**
 * lsof lists `state.lsof` (exiting 1 when it lists nothing); codesign exits `state.codesign`.
 * Both are read on each call, so a test can turn debugging on midway.
 */
function fakeRunner({ lsof = 'p4242\nf20\n', codesign = 0 } = {}) {
  const calls = [];
  const state = { lsof, codesign };
  const runner = async (file, args) => {
    calls.push({ file, args });
    if (file === '/usr/sbin/lsof') {
      return { exitCode: state.lsof === '' ? 1 : 0, stdout: state.lsof };
    }
    return { exitCode: state.codesign, stdout: '' };
  };
  return { runner, calls, state };
}

/** Each wait for the user that started, by kind, and whether its closer ran. */
function fakeWait() {
  const wait = { waits: [] };
  wait.begin = (kind) => {
    const begun = { kind, ended: false };
    wait.waits.push(begun);
    return () => (begun.ended = true);
  };
  return wait;
}

function connect(root, { runner = fakeRunner().runner, wait = fakeWait(), signal } = {}) {
  return connectUserBrowser({
    browser: 'chrome',
    applicationSupportDir: root,
    runner,
    waitForUser: wait.begin,
    signal: signal ?? new AbortController().signal,
  });
}

const failsWith = (code, message) => (error) => {
  assert.equal(error.code, code);
  if (message !== undefined) assert.match(error.message, message);
  return true;
};

test('the wait for Allow has no time limit, and stopping it closes the socket and the wait', async (t) => {
  const endpoint = await browserEndpoint(t, 'hold');
  const { root } = applicationSupport(t, { port: endpoint.port });
  const wait = fakeWait();
  const controller = new AbortController();
  let settled = false;
  const connecting = connect(root, { wait, signal: controller.signal });
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
  assert.deepEqual(wait.waits, [{ kind: 'allow', ended: false }]);

  const reason = new Error('the run stopped');
  controller.abort(reason);
  await assert.rejects(connecting, (error) => error === reason);
  await closed;
  assert.deepEqual(wait.waits, [{ kind: 'allow', ended: true }]);
});

test("the user's refusal is told apart from any other failed handshake", async (t) => {
  for (const [status, code] of [
    [403, 'BROWSER_USER_DENIED'],
    [500, 'BROWSER_USER_UNAVAILABLE'],
  ]) {
    const endpoint = await browserEndpoint(t, status);
    const { root } = applicationSupport(t, { port: endpoint.port });
    const wait = fakeWait();
    await assert.rejects(connect(root, { wait }), failsWith(code, /Google Chrome/));
    assert.deepEqual(wait.waits, [{ kind: 'allow', ended: true }]);
  }
});

test('each state of the browser and its files gets the code the user can act on', async (t) => {
  const deadPid = spawnSync(process.execPath, ['-e', '']).pid;
  const cases = [
    ['no lock', { lockPid: null, port: 9222 }, {}, 'BROWSER_USER_NOT_RUNNING', /not running/],
    ['lock of a quit browser', { lockPid: deadPid, port: 9222 }, {}, 'BROWSER_USER_NOT_RUNNING'],
    ['malformed port file', { portFile: 'x\n/elsewhere\n' }, {}, 'BROWSER_USER_UNAVAILABLE'],
    ['two listeners', { port: 9222 }, { lsof: 'p1\nf3\np2\nf4\n' }, 'BROWSER_USER_UNAVAILABLE'],
    [
      'port file blocked',
      { port: 9222, blocked: 'DevToolsActivePort' },
      {},
      'BROWSER_USER_FILE_BLOCKED',
      /Files & Folders/,
    ],
    ['lock blocked', { port: 9222, blocked: '.' }, {}, 'BROWSER_USER_FILE_BLOCKED'],
  ];
  for (const [name, files, runs, code, message] of cases) {
    const { root } = applicationSupport(t, files);
    await assert.rejects(
      connect(root, { runner: fakeRunner(runs).runner }),
      failsWith(code, message),
      name
    );
  }
});

test('a listener that is not the signed browser never gets the socket', async (t) => {
  const endpoint = await browserEndpoint(t, 'hold');
  const { root } = applicationSupport(t, { port: endpoint.port });
  const { runner, calls } = fakeRunner({ codesign: 3 });
  const wait = fakeWait();
  await assert.rejects(connect(root, { runner, wait }), failsWith('BROWSER_USER_UNAVAILABLE'));
  assert.equal(endpoint.upgrades.length, 0);
  assert.deepEqual(wait.waits, []);
  assert.ok(calls[0].args.includes(`-iTCP@127.0.0.1:${endpoint.port}`));
  const [flag, option, requirement, pid] = calls[1].args;
  assert.deepEqual([flag, option, pid], ['--verify', '-R', '4242']);
  assert.match(
    requirement,
    /OU\] = "EQHXZ8M8AV" and \(identifier "com\.google\.Chrome" or identifier "com\.google\.Chrome\.helper"\)$/
  );
});

const RESULTS = {
  'Target.createTarget': { targetId: 'own' },
  'Target.attachToTarget': { sessionId: 'own-page' },
};

test('a tab reaches only its own sessions and frames, hears only them, and has its popups closed', async (t) => {
  const received = [];
  let browserSide;
  const endpoint = await browserEndpoint(t, (socket) => {
    browserSide = socket;
    socket.on('message', (data) => {
      const message = JSON.parse(data);
      received.push(message);
      const result = RESULTS[message.method] ?? {};
      socket.send(JSON.stringify({ id: message.id, result }));
    });
  });
  const { root } = applicationSupport(t, { port: endpoint.port });
  const wait = fakeWait();
  const connection = await connect(root, { wait });
  // Allowed: the wait ended as the socket opened.
  assert.deepEqual(wait.waits, [{ kind: 'allow', ended: true }]);
  const popups = [];
  const tab = await connection.openTab((url) => popups.push(url), new AbortController().signal);
  const events = [];
  tab.cdp.on('message', (_event, method, _params, sessionId) => events.push([method, sessionId]));
  const refused = (method, params, sessionId) =>
    assert.rejects(tab.cdp.sendCommand(method, params, sessionId), CdpCommandError, method);

  // Chromium routes Target commands by the target they name, whichever session carries them;
  // auto-attach is the module's own.
  await refused('Target.getTargets', {});
  await refused('Target.closeTarget', { targetId: 'users-tab' });
  await refused('Target.setAutoAttach', { autoAttach: true, flatten: true });
  await refused('Storage.getCookies', {});
  // An empty session id is the browser itself in Chromium.
  await refused('Page.navigate', { url: 'https://example.com' }, '');
  // chrome://inspect lists the browser's other targets and can close them.
  await refused('Page.navigate', { url: 'chrome://inspect/#pages' });
  await tab.cdp.sendCommand('Page.navigate', { url: 'https://example.com' });

  const send = (method, params, sessionId) =>
    browserSide.send(JSON.stringify({ method, params, sessionId }));
  const attached = (sessionId, type) =>
    send(
      'Target.attachedToTarget',
      { sessionId, targetInfo: { targetId: type, type } },
      'own-page'
    );
  send('Target.targetCreated', {
    targetInfo: { targetId: 'users-tab', url: 'https://mail.example' },
  });
  send('Runtime.consoleAPICalled', { type: 'log' }, 'users-session');
  send('Target.targetCreated', {
    targetInfo: { targetId: 'popup', openerId: 'own', url: 'https://popup.example/' },
  });
  // A service or shared worker serves the profile's other tabs of its origin.
  attached('sw-session', 'service_worker');
  attached('shared-session', 'shared_worker');
  send('Runtime.consoleAPICalled', { type: 'log' }, 'sw-session');
  send('Page.loadEventFired', { timestamp: 1 }, 'own-page');
  attached('own-frame', 'iframe');
  send('Page.frameNavigated', {}, 'own-frame');
  while (events.length < 3) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events, [
    ['Page.loadEventFired', undefined],
    ['Target.attachedToTarget', undefined],
    ['Page.frameNavigated', 'own-frame'],
  ]);
  assert.deepEqual(popups, ['https://popup.example/']);
  await refused('Runtime.evaluate', { expression: 'clients.matchAll()' }, 'sw-session');
  await tab.cdp.sendCommand('Runtime.runIfWaitingForDebugger', {}, 'own-frame');
  await tab.close();

  assert.deepEqual(
    received.map((m) => [m.method, m.params.targetId ?? m.params.sessionId, m.sessionId]),
    [
      ['Target.setDiscoverTargets', undefined, undefined],
      ['Target.createTarget', undefined, undefined],
      ['Target.attachToTarget', 'own', undefined],
      ['Target.setAutoAttach', undefined, 'own-page'],
      ['Page.navigate', undefined, 'own-page'],
      ['Target.closeTarget', 'popup', undefined],
      ['Target.detachFromTarget', 'sw-session', 'own-page'],
      ['Target.detachFromTarget', 'shared-session', 'own-page'],
      ['Target.setAutoAttach', undefined, 'own-frame'],
      ['Runtime.runIfWaitingForDebugger', undefined, 'own-frame'],
      ['Target.closeTarget', 'own', undefined],
    ]
  );
  assert.deepEqual(received[1].params, { url: 'about:blank', hidden: true, background: true });
  assert.deepEqual(received[3].params.filter, [
    { type: 'iframe' },
    { type: 'worker' },
    { exclude: true },
  ]);

  connection.close();
  await connection.closed;
  await assert.rejects(tab.cdp.sendCommand('Page.enable', {}), /closed/);
});

// Catches: a tab whose operation already timed out still being opened or driven in the user's
// browser: a late reply leading on to createTarget, attach or auto-attach, a created target left
// open, or a child of the dead tab auto-attached and heard.
test("once a tab's signal aborts, nothing more is sent for it but closing it", async (t) => {
  const received = [];
  const held = new Map();
  let browserSide;
  const endpoint = await browserEndpoint(t, (socket) => {
    browserSide = socket;
    socket.on('message', (data) => {
      const message = JSON.parse(data);
      received.push([message.method, message.sessionId]);
      const reply = () =>
        socket.send(JSON.stringify({ id: message.id, result: RESULTS[message.method] ?? {} }));
      if (held.has(message.method)) held.set(message.method, reply);
      else reply();
    });
  });
  const { root } = applicationSupport(t, { port: endpoint.port });
  const connection = await connect(root);
  const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
  const timedOut = new Error('timed out');
  /** Opens a tab whose `method` reply is held until its signal aborted. */
  async function abortedWhile(method) {
    received.length = 0;
    held.set(method, null);
    const controller = new AbortController();
    const opening = connection.openTab(() => undefined, controller.signal);
    while (held.get(method) === null) await settle();
    controller.abort(timedOut);
    held.get(method)();
    held.delete(method);
    await assert.rejects(opening, (error) => error === timedOut);
    await settle();
    return received.map(([name]) => name);
  }
  assert.deepEqual(await abortedWhile('Target.setDiscoverTargets'), ['Target.setDiscoverTargets']);
  assert.deepEqual(await abortedWhile('Target.createTarget'), [
    'Target.setDiscoverTargets',
    'Target.createTarget',
    'Target.closeTarget',
  ]);

  const controller = new AbortController();
  const tab = await connection.openTab(() => undefined, controller.signal);
  const events = [];
  tab.cdp.on('message', (_event, method) => events.push(method));
  controller.abort(timedOut);
  received.length = 0;
  browserSide.send(
    JSON.stringify({
      method: 'Target.attachedToTarget',
      params: { sessionId: 'late-frame', targetInfo: { targetId: 'f', type: 'iframe' } },
      sessionId: 'own-page',
    })
  );
  await settle();
  await assert.rejects(tab.cdp.sendCommand('Page.navigate', { url: 'https://a.example/' }));
  assert.deepEqual([received, events], [[], []]);
  await tab.close();
  assert.deepEqual(received, [['Target.closeTarget', undefined]]);
  connection.close();
});

// Catches: remote debugging off ending the run instead of waiting for the user, the line kept on
// the guidance once it is on, or no Allow wait after it.
test('with remote debugging off the wait lasts until it is on, then asks to allow', async (t) => {
  const endpoint = await browserEndpoint(t, 'hold');
  const { root } = applicationSupport(t, { port: endpoint.port });
  // The file is left from before, but nothing listens until the user turns debugging on.
  const { runner, state } = fakeRunner({ lsof: '' });
  const wait = fakeWait();
  const controller = new AbortController();
  const connecting = connect(root, { runner, wait, signal: controller.signal });
  connecting.catch(() => undefined);
  while (wait.waits.length === 0) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(wait.waits, [{ kind: 'debugging_off', ended: false }]);

  state.lsof = 'p4242\nf20\n';
  while (endpoint.upgrades.length === 0) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(wait.waits, [
    { kind: 'debugging_off', ended: true },
    { kind: 'allow', ended: false },
  ]);
  controller.abort(new Error('the run stopped'));
  await assert.rejects(connecting);
});

// Catches: a stop while debugging is off that leaves the guidance up, or a poll still reading
// the browser's files and running lsof for a run that ended.
test('stopping the run while debugging is off ends the wait and the polling', async (t) => {
  const { root } = applicationSupport(t, { port: 9222 });
  const { runner, calls } = fakeRunner({ lsof: '' });
  const wait = fakeWait();
  const controller = new AbortController();
  const connecting = connect(root, { runner, wait, signal: controller.signal });
  while (wait.waits.length === 0) await new Promise((r) => setTimeout(r, 10));
  const reason = new Error('the run stopped');
  controller.abort(reason);
  await assert.rejects(connecting, (error) => error === reason);
  assert.deepEqual(wait.waits, [{ kind: 'debugging_off', ended: true }]);
  const checks = calls.length;
  await new Promise((r) => setTimeout(r, 1_500));
  assert.equal(calls.length, checks);
});

// Catches: a run stopped while the browser's signature was checked still asking the user to allow
// Pantaray in.
test(
  'a stop during the last check opens no socket and asks nothing',
  { timeout: 5_000 },
  async (t) => {
    const endpoint = await browserEndpoint(t, 'hold');
    const { root } = applicationSupport(t, { port: endpoint.port });
    const controller = new AbortController();
    const reason = new Error('the run stopped');
    const { runner: check } = fakeRunner();
    // The stop lands once the signature check has answered, before the socket would open.
    const runner = (file, args, signal) => {
      const answer = check(file, args, signal);
      if (file === '/usr/bin/codesign') {
        queueMicrotask(() => answer.then(() => controller.abort(reason)));
      }
      return answer;
    };
    const wait = fakeWait();
    await assert.rejects(
      connect(root, { runner, wait, signal: controller.signal }),
      (error) => error === reason
    );
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(endpoint.upgrades.length, 0);
    assert.deepEqual(wait.waits, []);
  }
);

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

// Catches: the button opening another app than the browser that asks, or a refused open passing
// as done.
test('bringing the browser forward opens its own app, and a refused open rejects', async () => {
  const calls = [];
  await bringUserBrowserForward('chrome_beta', async (file, args) => {
    calls.push([file, ...args]);
    return { exitCode: 0, stdout: '' };
  });
  assert.deepEqual(calls, [['/usr/bin/open', '-b', 'com.google.Chrome.beta']]);
  await assert.rejects(
    bringUserBrowserForward('edge', async () => ({ exitCode: 1, stdout: '' })),
    /com\.microsoft\.edgemac/
  );
});
