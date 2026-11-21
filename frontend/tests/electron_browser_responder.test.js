const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { EventEmitter } = require('node:events');
const { png, fakeBrowserElectron } = require('./helpers/browser_electron_fakes');
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
  const fake = fakeBrowserElectron(scripts);
  const { createBrowserSessions, createBrowserResponder } = loadOnElectron(fake.electron);
  const artifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-browser-'));
  const sessions = createBrowserSessions({
    getBackendPort: () => BACKEND_PORT,
    pages: { opened() {}, touched() {}, closed() {} },
  });
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
    user_browser: null,
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

// Catches: an opened host the runtime would have to parse itself, or one parsed otherwise than
// Chromium loads it (an empty authority, a percent-encoded or numeric host), which would name a
// site the page did not open in the write approval.
test('a navigate answer names the host WHATWG parses from the requested URL', async () => {
  const h = harness();
  for (const url of ['https:////evil.example/', 'https://%65vil.example/', 'http://0x08080808/']) {
    const opened = navigate(url);
    await h.respond(opened);
    assert.equal(h.answerOf(opened.operation_request_id).opened_host, new URL(url).hostname);
  }
  assert.deepEqual(
    ['evil.example', 'evil.example', '8.8.8.8'],
    h.answers.map((answer) => answer.result.opened_host)
  );
  const file = operation({ action: 'navigate', target: { kind: 'file', path: '/work/deck.html' } });
  await h.respond(file);
  assert.equal(h.answerOf(file.operation_request_id).opened_host, null);
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

// Catches: an artifact left behind when the runtime already gave up on the operation, and one
// removed while a runtime that took the answer may still be reading it (a dropped connection).
test('an artifact is removed only once the runtime said it does not wait for it', async () => {
  for (const [failure, kept] of [
    [new LocalBackendRequestError('gone', 404), false],
    [new TypeError('fetch failed'), true],
  ]) {
    const h = harness(
      {
        cdp: async (method) =>
          method === 'Page.captureScreenshot' ? { data: png(1280, 800).toString('base64') } : {},
      },
      async (body) => {
        if (body.result.artifact) throw failure;
      }
    );
    await h.respond(navigate('https://a.example/'));
    const shot = screenshot();
    await h.respond(shot);
    assert.equal(fs.existsSync(h.artifactPath(shot.operation_request_id, 'png')), kept);
  }
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
