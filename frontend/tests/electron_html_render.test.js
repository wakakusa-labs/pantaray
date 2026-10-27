const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { pathToFileURL } = require('node:url');
const { test } = require('node:test');

const MODULE_PATH = require.resolve('../electron/dist/render/htmlRender.js');
const { LocalBackendRequestError } = require('../electron/dist/localBackend/client.js');
const PDF = Buffer.from('%PDF-1.7 rendered');

/** A fake session and window factory; `load` and `print` script each render. */
function fakeElectron({ load = async () => {}, print = async () => PDF } = {}) {
  const ses = new EventEmitter();
  Object.assign(ses, {
    proxies: [],
    cleared: [],
    protocol: {
      handle: (_scheme, handler) => (ses.fileHandler = { handler }),
      unhandle: (scheme) => (ses.unhandled = scheme),
    },
    webRequest: { onBeforeRequest: (listener) => (ses.beforeRequest = listener) },
    setPermissionRequestHandler: (handler) => (ses.permissionRequest = handler),
    setPermissionCheckHandler: (handler) => (ses.permissionCheck = handler),
    setDevicePermissionHandler: (handler) => (ses.devicePermission = handler),
    setProxy: async (config) => ses.proxies.push(config),
    clearCache: async () => ses.cleared.push('cache'),
    clearStorageData: async () => ses.cleared.push('storage'),
  });
  const windows = [];
  class BrowserWindow {
    constructor(options) {
      this.options = options;
      this.destroyed = false;
      const contents = new EventEmitter();
      Object.assign(contents, {
        getURL: () => 'file:///work/deck.html',
        setWindowOpenHandler: (handler) => (contents.openHandler = handler),
        setWebRTCIPHandlingPolicy: (policy) => (contents.webRtcPolicy = policy),
        loadURL: (url) => load(url, { ses, contents }),
        executeJavaScript: async () => undefined,
        printToPDF: async (options) => {
          contents.printOptions = options;
          return print();
        },
      });
      this.webContents = contents;
      windows.push(this);
    }

    destroy() {
      this.destroyed = true;
    }
  }
  return { ses, windows, electron: { BrowserWindow, session: { fromPartition: () => ses } } };
}

function loadHtmlRender(electron) {
  delete require.cache[MODULE_PATH];
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return electron;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require(MODULE_PATH);
  } finally {
    Module._load = originalLoad;
    delete require.cache[MODULE_PATH];
  }
}

const { decideFileAccess, decideNetworkRequest, htmlRenderPdfPath, isWithinRoot } = loadHtmlRender(
  fakeElectron().electron
);

function harness(scripts = {}, postAnswer = async () => ({})) {
  const fake = fakeElectron(scripts);
  const artifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-html-render-'));
  const posted = [];
  const logged = [];
  const respond = loadHtmlRender(fake.electron).createHtmlRenderResponder({
    localArtifactRoot: artifactRoot,
    postAnswer: async (body) => {
      posted.push({ body, fileExists: fs.existsSync(pdfPathOf(body.render_request_id)) });
      return postAnswer(body);
    },
    logger: { error: (name, payload) => logged.push({ name, payload }) },
  });
  const pdfPathOf = (id) => path.join(artifactRoot, '.tmp', 'html-renders', `${id}.pdf`);
  return { ...fake, artifactRoot, posted, logged, respond, pdfPathOf };
}

function request(overrides = {}) {
  return {
    renderRequestId: 'render-1',
    actionId: 'action-1',
    processId: 'proc-1',
    toolRequestId: 'tool-1',
    htmlPath: '/work/deck.html',
    fileRoots: ['/work'],
    privateRoots: [],
    privateRootExceptions: [],
    allowNetwork: false,
    ...overrides,
  };
}

function assertTornDown(fake) {
  assert.ok(fake.windows.every((window) => window.destroyed));
  assert.equal(fake.ses.unhandled, 'file');
  assert.equal(fake.ses.beforeRequest, null);
  assert.equal(fake.ses.listenerCount('will-download'), 0);
  assert.deepEqual(fake.ses.cleared.sort(), ['cache', 'storage']);
  assert.deepEqual(fake.ses.proxies.at(-1), { mode: 'system' });
}

const notSameDirectory = () => false;

test('file access mirrors PrivateAppStorage.hides() under the read roots', () => {
  const scope = {
    fileRoots: ['/work'],
    privateRoots: ['/work/.pantaray'],
    privateRootExceptions: ['/work/.pantaray/published'],
  };
  const decide = (target) => decideFileAccess(target, scope, notSameDirectory);
  assert.equal(decide('/work/deck/img.png'), null);
  assert.equal(decide('/work'), null);
  assert.equal(decide('/workshop/img.png'), 'outside_read_scope');
  assert.equal(decide('/etc/passwd'), 'outside_read_scope');
  assert.equal(decide('/work/.pantaray/db.sqlite'), 'outside_read_scope');
  assert.equal(decide('/work/.pantaray'), 'outside_read_scope');
  assert.equal(decide('/work/.pantaray/published/chart.png'), null);
  assert.equal(
    decideFileAccess('/Users/me/secret.txt', { ...scope, fileRoots: ['/'] }, notSameDirectory),
    null
  );
});

test('a private root reached under another letter case is still private', () => {
  const sameDirectory = (left, right) => left.toLowerCase() === right.toLowerCase();
  const scope = {
    fileRoots: ['/'],
    privateRoots: ['/Users/me/Library/Pantaray'],
    privateRootExceptions: [],
  };
  assert.equal(
    decideFileAccess('/Users/me/library/pantaray/db.sqlite', scope, sameDirectory),
    'outside_read_scope'
  );
  // The name matches but the directory is another one.
  assert.equal(
    isWithinRoot('/other/pantaray/x', '/Users/me/Library/Pantaray', () => false),
    false
  );
});

test('only http(s) reads leave the machine, and only with the network allowed', () => {
  const cases = [
    ['https://fonts.googleapis.com/css2', 'GET', true, null],
    ['http://example.com/a.png', 'HEAD', true, null],
    ['https://example.com/api', 'POST', true, 'not_allowed'],
    ['https://example.com/embed', 'GET', true, 'not_allowed', 'subFrame'],
    ['data:text/html,<p>x</p>', 'GET', true, null, 'subFrame'],
    ['wss://example.com/', 'GET', true, 'not_allowed'],
    ['ftp://example.com/a', 'GET', true, 'not_allowed'],
    ['https://fonts.googleapis.com/css2', 'GET', false, 'network_off'],
    ['wss://example.com/', 'GET', false, 'network_off'],
    ['file:///work/a.png', 'GET', false, null],
    ['data:image/png;base64,AA==', 'GET', false, null],
    ['not a url', 'GET', true, 'not_allowed'],
  ];
  for (const [url, method, allowNetwork, expected, resourceType = 'image'] of cases) {
    assert.equal(
      decideNetworkRequest({ url, method, resourceType }, allowNetwork),
      expected,
      `${method} ${resourceType} ${url}`
    );
  }
});

test('the PDF path is the one the runtime derives, and an id cannot leave the folder', () => {
  assert.equal(
    htmlRenderPdfPath('/art', '6f1c2d4e-aaaa-bbbb-cccc-0123456789ab'),
    '/art/.tmp/html-renders/6f1c2d4e-aaaa-bbbb-cccc-0123456789ab.pdf'
  );
  for (const id of ['../escape', 'a/b', '', '..']) {
    assert.throws(() => htmlRenderPdfPath('/art', id));
  }
});

test('a render prints in a closed hidden window, writes the PDF and reports blocked loads', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-html-root-'));
  const work = fs.realpathSync(root);
  fs.writeFileSync(path.join(work, 'deck.html'), '<p>deck</p>');
  fs.mkdirSync(path.join(work, 'private'));
  fs.writeFileSync(path.join(work, 'private', 'secret.html'), 'secret');
  const served = [];
  const h = harness({
    load: async (url, { ses }) => {
      const { handler } = ses.fileHandler;
      for (const file of ['deck.html', 'private/secret.html', '.', 'missing.png']) {
        const response = await handler({ url: pathToFileURL(path.join(work, file)).href });
        served.push([
          file,
          response.type,
          response.headers.get('content-type'),
          response.headers.get('content-security-policy'),
          await response.text(),
        ]);
      }
      for (const [url, method, resourceType] of [
        ['https://example.com/font.woff2', 'GET', 'font'],
        ['https://example.com/api', 'POST', 'xhr'],
      ]) {
        ses.beforeRequest({ url, method, resourceType }, (decision) =>
          served.push([url, decision])
        );
      }
      ses.emit('will-download', { preventDefault: () => served.push('download refused') });
      assert.equal(url, pathToFileURL(path.join(work, 'deck.html')).href);
    },
  });
  await h.respond(
    request({
      htmlPath: path.join(work, 'deck.html'),
      fileRoots: [work],
      privateRoots: [path.join(work, 'private')],
      allowNetwork: true,
    })
  );

  const [window] = h.windows;
  assert.equal(window.options.show, false);
  assert.equal(window.options.focusable, false);
  assert.deepEqual(window.options.webPreferences, {
    partition: 'html-render',
    sandbox: true,
    contextIsolation: true,
    nodeIntegration: false,
    nodeIntegrationInSubFrames: false,
    webviewTag: false,
    devTools: false,
    disableDialogs: true,
    webSecurity: true,
    backgroundThrottling: false,
    javascript: true,
  });
  const contents = window.webContents;
  assert.deepEqual(contents.openHandler({ url: 'https://example.com' }), { action: 'deny' });
  assert.deepEqual(contents.printOptions, {
    printBackground: true,
    preferCSSPageSize: true,
    pageSize: 'A4',
  });
  let prevented = 0;
  const event = { preventDefault: () => (prevented += 1) };
  contents.emit('will-navigate', event, 'https://example.com/elsewhere');
  contents.emit('will-attach-webview', event);
  assert.equal(prevented, 2);
  let permission = null;
  h.ses.permissionRequest(contents, 'media', (granted) => (permission = granted));
  assert.equal(permission, false);
  assert.equal(h.ses.permissionCheck(), false);
  assert.equal(h.ses.devicePermission(), false);

  assert.deepEqual(served, [
    // No allow-modals: print() and dialogs do nothing in this frame and every one below.
    ['deck.html', 'default', 'text/html', 'sandbox allow-scripts allow-same-origin', '<p>deck</p>'],
    ['private/secret.html', 'error', null, null, ''],
    ['.', 'error', null, null, ''],
    ['missing.png', 'error', null, null, ''],
    ['https://example.com/font.woff2', { cancel: false }],
    ['https://example.com/api', { cancel: true }],
    'download refused',
  ]);
  // With the network allowed, connections go as the system routes them.
  assert.deepEqual(h.ses.proxies, [{ mode: 'system' }]);
  assert.equal(contents.webRtcPolicy, undefined);
  assert.deepEqual(h.posted, [
    {
      body: {
        render_request_id: 'render-1',
        result: {
          status: 'rendered',
          byte_size: PDF.byteLength,
          sha256: createHash('sha256').update(PDF).digest('hex'),
          blocked_requests: [
            {
              reason: 'outside_read_scope',
              url: pathToFileURL(path.join(work, 'private/secret.html')).href,
            },
            { reason: 'not_allowed', url: pathToFileURL(work).href },
            { reason: 'not_allowed', url: 'https://example.com/api' },
          ],
        },
      },
      fileExists: true,
    },
  ]);
  assert.deepEqual(fs.readFileSync(h.pdfPathOf('render-1')), PDF);
  assertTornDown(h);
});

test('a file swapped after it was opened is not served', async (t) => {
  const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-html-swap-')));
  fs.writeFileSync(path.join(work, 'chart.svg'), '<svg/>');
  const outside = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-html-out-')));
  fs.writeFileSync(path.join(outside, 'secret.svg'), 'SECRET');
  const link = path.join(work, 'link.svg');
  fs.symlinkSync(path.join(outside, 'secret.svg'), link);
  // The link points outside when it is opened and back inside when it is checked.
  const realpath = fs.promises.realpath;
  t.mock.method(fs.promises, 'realpath', async (target) => {
    if (target === link) {
      fs.rmSync(link);
      fs.symlinkSync(path.join(work, 'chart.svg'), link);
    }
    return realpath(target);
  });
  let response;
  const h = harness({
    load: async (_url, { ses }) => {
      response = await ses.fileHandler.handler({ url: pathToFileURL(link).href });
    },
  });
  await h.respond(request({ htmlPath: path.join(work, 'deck.html'), fileRoots: [work] }));
  assert.equal(response.type, 'error');
  assert.deepEqual(h.posted[0].body.result.blocked_requests, [
    { reason: 'outside_read_scope', url: pathToFileURL(link).href },
  ]);
});

test('with the network off, connections outside webRequest go to a dead proxy', async () => {
  let proxiesAtLoad;
  const h = harness({ load: async (_url, { ses }) => (proxiesAtLoad = [...ses.proxies]) });
  await h.respond(request({ allowNetwork: false }));
  assert.deepEqual(proxiesAtLoad, [
    { proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '<-loopback>' },
  ]);
  assert.equal(h.windows[0].webContents.webRtcPolicy, 'disable_non_proxied_udp');
  assertTornDown(h);
});

for (const [name, scripts, code] of [
  [
    'the page does not load',
    { load: async () => Promise.reject(new Error('ERR_FAILED')) },
    'HTML_LOAD_FAILED',
  ],
  ['printing fails', { print: async () => Promise.reject(new Error('boom')) }, 'HTML_PRINT_FAILED'],
  [
    'the PDF is over 100 MB',
    { print: async () => Buffer.allocUnsafe(100 * 1024 * 1024 + 1) },
    'HTML_PDF_TOO_LARGE',
  ],
  [
    'the renderer crashes',
    {
      load: (_url, { contents }) => {
        setImmediate(() => contents.emit('render-process-gone', {}, { reason: 'crashed' }));
        return new Promise(() => {});
      },
    },
    'HTML_RENDERER_CRASHED',
  ],
]) {
  test(`a failed answer and no PDF when ${name}`, async () => {
    const h = harness(scripts);
    await h.respond(request());
    assert.equal(h.posted.length, 1);
    assert.equal(h.posted[0].body.result.status, 'failed');
    assert.equal(h.posted[0].body.result.code, code);
    assert.equal(h.posted[0].fileExists, false);
    assertTornDown(h);
  });
}

test('a page that never finishes is stopped after 20 s', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness({ load: () => new Promise(() => {}) });
  const done = h.respond(request());
  while (h.windows.length === 0) await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  t.mock.timers.tick(19_999);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.posted.length, 0);
  t.mock.timers.tick(1);
  await done;
  assert.equal(h.posted[0].body.result.code, 'HTML_RENDER_TIMED_OUT');
  assertTornDown(h);
});

test('renders run one at a time', async () => {
  let release;
  let loads = 0;
  const h = harness({
    load: () => (++loads === 1 ? new Promise((resolve) => (release = resolve)) : Promise.resolve()),
  });
  const first = h.respond(request({ renderRequestId: 'render-1' }));
  const second = h.respond(request({ renderRequestId: 'render-2' }));
  for (let turn = 0; turn < 20; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.windows.length, 1);
  release();
  await Promise.all([first, second]);
  assert.deepEqual(
    h.posted.map((entry) => entry.body.render_request_id),
    ['render-1', 'render-2']
  );
});

test('the answer is posted once; only a 404 deletes the PDF', async () => {
  for (const [status, kept] of [
    [404, false],
    [500, true],
  ]) {
    const failure = new LocalBackendRequestError('failed', status);
    const h = harness({}, async () => Promise.reject(failure));
    await h.respond(request());
    assert.equal(h.posted.length, 1);
    assert.equal(h.posted[0].fileExists, true);
    assert.equal(fs.existsSync(h.pdfPathOf('render-1')), kept, `status ${status}`);
    assert.equal(h.logged[0].name, 'HTML_RENDER_REQUEST_ERR');
  }
});
