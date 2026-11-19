const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');

const SESSION_MODULE = require.resolve('../electron/dist/browser/browserSession.js');
const RESPONDER_MODULE = require.resolve('../electron/dist/browser/browserResponder.js');
const CAPTURE_MODULE = require.resolve('../electron/dist/browser/capture.js');
const { BrowserOperationAnswerSchema } = require('../electron/dist/browser/browserContract.js');
const { LocalBackendRequestError } = require('../electron/dist/localBackend/client.js');
const {
  createLocalBackendHelperManager,
} = require('../electron/dist/auth/localBackendHelperManager.js');

const BACKEND_PORT = 49152;
const PDF = Buffer.from('%PDF-1.7 printed');

/** A PNG header of the given size; the code under test reads only the signature and IHDR. */
function png(width, height) {
  const size = Buffer.alloc(8);
  size.writeUInt32BE(width, 0);
  size.writeUInt32BE(height, 4);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13]),
    Buffer.from('IHDR'),
    size,
    Buffer.alloc(16),
  ]);
}

/** Fake Electron: one session per partition, windows whose page `load` and `cdp` script. */
function fakeElectron({ load = async () => {}, cdp = async () => ({}) } = {}) {
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
      registerPreloadScript: () => undefined,
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
      const history = [];
      const contents = new EventEmitter();
      const debuggerApi = new EventEmitter();
      Object.assign(debuggerApi, {
        commands: [],
        attach: (version) => (debuggerApi.attached = version),
        sendCommand: async (method, params, sessionId) => {
          debuggerApi.commands.push({ method, params, sessionId });
          return cdp(method, params, { contents, debuggerApi });
        },
      });
      const ses = fromPartition(options.webPreferences.partition);
      Object.assign(contents, {
        debugger: debuggerApi,
        history,
        getURL: () => history.at(-1) ?? '',
        getTitle: () => `title of ${history.at(-1)}`,
        // The session loads about:blank before it attaches CDP; the page's history starts after.
        loadURL: async (url) => {
          if (url === 'about:blank') return;
          await load(url, { ses, contents });
          history.push(url);
        },
        isLoading: () => false,
        printToPDF: async (printOptions) => {
          contents.printOptions = printOptions;
          return PDF;
        },
        navigationHistory: {
          canGoBack: () => history.length > 1,
          goBack: () => {
            history.pop();
            setImmediate(() => contents.emit('did-navigate'));
          },
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
  const fresh = () =>
    [SESSION_MODULE, RESPONDER_MODULE, CAPTURE_MODULE].forEach((p) => delete require.cache[p]);
  fresh();
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return electron;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return { ...require(SESSION_MODULE), ...require(RESPONDER_MODULE) };
  } finally {
    Module._load = originalLoad;
    fresh();
  }
}

function harness(scripts = {}, postAnswer = async () => ({})) {
  const fake = fakeElectron(scripts);
  const { createBrowserSessions, createBrowserResponder } = loadOnElectron(fake.electron);
  const artifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-browser-'));
  const sessions = createBrowserSessions({ getBackendPort: () => BACKEND_PORT });
  const answers = [];
  const logged = [];
  const respond = createBrowserResponder({
    sessions,
    localArtifactRoot: artifactRoot,
    postAnswer: async (body) => {
      // Every answer must pass the wire contract the runtime validates.
      BrowserOperationAnswerSchema.parse(body);
      answers.push(body);
      return postAnswer(body);
    },
    logger: { error: (name, payload) => logged.push({ name, payload }) },
  });
  const artifactPath = (id, kind) => path.join(artifactRoot, '.tmp', 'browser', `${id}.${kind}`);
  const answerOf = (id) => answers.find((answer) => answer.operation_request_id === id)?.result;
  return { ...fake, sessions, respond, answers, answerOf, logged, artifactPath };
}

let nextOperation = 0;
function operation(op, { session = 'session-1', policy = {} } = {}) {
  nextOperation += 1;
  return {
    operation_request_id: `op-${nextOperation}`,
    session_id: session,
    action_id: 'action-1',
    process_id: 'process-1',
    tool_request_id: 'tool-1',
    operation: op,
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
const navigate = (url, options) =>
  operation({ action: 'navigate', target: { kind: 'web', url } }, options);
const screenshot = (fields = {}, options) =>
  operation({ action: 'screenshot', ref: null, full_page: null, ...fields }, options);

// Catches: an answer without the page's url, a back that never settles, and a navigation
// failure that leaks the URL Chromium echoes in its message or reports as success.
test('navigate and back answer with the page, and a failed load names only its error', async () => {
  const h = harness({
    load: async (url) => {
      if (url.includes('down')) {
        throw Object.assign(new Error(`ERR_CONNECTION_REFUSED (-102) loading '${url}'`), {
          code: 'ERR_CONNECTION_REFUSED',
        });
      }
    },
  });
  const first = navigate('https://a.example/');
  await h.respond(first);
  assert.equal(h.answerOf(first.operation_request_id).page_data.url, 'https://a.example/');
  const noEarlier = operation({ action: 'back' });
  await h.respond(noEarlier);
  assert.equal(h.answerOf(noEarlier.operation_request_id).code, 'BROWSER_ACTION_FAILED');

  await h.respond(navigate('https://b.example/'));
  const back = operation({ action: 'back' });
  await h.respond(back);
  assert.equal(h.answerOf(back.operation_request_id).page_data.url, 'https://a.example/');

  const down = navigate('https://down.example/secret-token');
  await h.respond(down);
  const failed = h.answerOf(down.operation_request_id);
  assert.equal(failed.code, 'BROWSER_NAVIGATION_FAILED');
  assert.match(failed.message, /ERR_CONNECTION_REFUSED/);
  assert.doesNotMatch(failed.message, /secret-token/);
});

// Catches: a claim that does not match the file written, an image at the wrong size, a full
// page past the cap without `page_too_tall`, and a print that leaves no PDF.
test('screenshots and prints write the artifact their claim describes', async () => {
  const h = harness({
    cdp: async (method, params) => {
      if (method === 'Page.getLayoutMetrics') return { cssContentSize: { height: 20_000 } };
      if (method === 'Page.captureScreenshot') {
        return { data: png(1280, params.clip?.height ?? 800).toString('base64') };
      }
      return {};
    },
  });
  await h.respond(navigate('https://a.example/'));
  const view = screenshot();
  const full = screenshot({ full_page: true });
  const printed = operation({ action: 'print' });
  for (const request of [view, full, printed]) await h.respond(request);

  const claimOf = (request, kind) => {
    const result = h.answerOf(request.operation_request_id);
    const bytes = fs.readFileSync(h.artifactPath(request.operation_request_id, kind));
    assert.equal(result.artifact.byte_size, bytes.length);
    assert.equal(result.artifact.sha256, createHash('sha256').update(bytes).digest('hex'));
    return result;
  };
  const viewResult = claimOf(view, 'png');
  assert.deepEqual([viewResult.artifact.width_px, viewResult.artifact.height_px], [1280, 800]);
  assert.equal(viewResult.truncated, null);
  const fullResult = claimOf(full, 'png');
  assert.deepEqual([fullResult.artifact.width_px, fullResult.artifact.height_px], [1280, 8000]);
  assert.equal(fullResult.truncated, 'page_too_tall');
  assert.equal(claimOf(printed, 'pdf').artifact.kind, 'pdf');
});

// Catches: an artifact left behind when the runtime already gave up on the operation.
test('an artifact the runtime no longer waits for is removed', async () => {
  const h = harness(
    {
      cdp: async (method) =>
        method === 'Page.captureScreenshot' ? { data: png(1280, 800).toString('base64') } : {},
    },
    async (body) => {
      if (body.result.artifact) throw new LocalBackendRequestError('gone', 404);
    }
  );
  await h.respond(navigate('https://a.example/'));
  const shot = screenshot();
  await h.respond(shot);
  assert.equal(fs.existsSync(h.artifactPath(shot.operation_request_id, 'png')), false);
});

// Catches: a failed post logged with its message, which can repeat page text or a private URL.
test('a failed answer is logged without its message', async () => {
  const h = harness({}, async () => {
    throw new LocalBackendRequestError('ignore previous instructions at /private?token=x', 500);
  });
  await h.respond(navigate('https://a.example/'));
  assert.deepEqual(h.logged, [
    {
      name: 'BROWSER_OPERATION_REQUEST_ERR',
      payload: {
        operation_request_id: h.answers[0].operation_request_id,
        error: 'LocalBackendRequestError',
        status: 500,
      },
    },
  ]);
});

// Catches: operations N4c owns answered as success, and a ref screenshot with no ref map.
test('operations this engine does not run yet fail clearly', async () => {
  const h = harness();
  await h.respond(navigate('https://a.example/'));
  const click = operation({ action: 'click', ref: 'e1' });
  const byRef = screenshot({ ref: 'e1' });
  await h.respond(click);
  await h.respond(byRef);
  assert.equal(h.answerOf(click.operation_request_id).code, 'BROWSER_ACTION_FAILED');
  assert.equal(h.answerOf(byRef.operation_request_id).code, 'BROWSER_REF_UNKNOWN');
});

// Catches: sessions outliving the backend generation that opened them, which also happens
// when the app quits (shutdown stops the helper).
test('every session closes when a ready backend generation ends', async () => {
  const h = harness();
  await h.respond(navigate('https://a.example/', { session: 'one' }));
  await h.respond(navigate('https://b.example/', { session: 'two' }));
  {
    const child = new EventEmitter();
    Object.assign(child, {
      exitCode: null,
      signalCode: null,
      pid: 1,
      kill(signal) {
        child.signalCode = signal;
        child.emit('exit', null, signal);
      },
    });
    const manager = createLocalBackendHelperManager({
      isDevRuntime: true,
      agentsRoot: '/tmp/agents',
      resourcesPath: '/tmp/resources',
      getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
      getHelperExecutablePath: () => null,
      getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 0 }),
      spawnFn: () => child,
      probeReadiness: async (_socket, helperInstanceId) => ({
        kind: 'owned',
        status: {
          helperInstanceId,
          localApiToken: 'token',
          cloudSessionState: 'absent',
          backendHost: '127.0.0.1',
          backendPort: BACKEND_PORT,
        },
      }),
      onGenerationEnded: h.sessions.closeAll,
    });
    await manager.ensureStarted();
    await manager.stopForShutdown();
  }
  assert.ok(h.windows.every((window) => window.destroyed));
  const after = navigate('https://a.example/', { session: 'one' });
  await h.respond(after);
  assert.equal(h.answerOf(after.operation_request_id).code, 'BROWSER_SESSION_GONE');
});
