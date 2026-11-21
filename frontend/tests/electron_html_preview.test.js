const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { pathToFileURL } = require('node:url');
const { test } = require('node:test');

const { PDF, fakeBrowserElectron } = require('./helpers/browser_electron_fakes');
const { registerActionFileHandlers } = require('../electron/dist/ipc/handlers/actionFiles.js');
const {
  createActionFileProtocolHandler,
} = require('../electron/dist/protocol/actionFileProtocol.js');
const { buildActionFileUrl } = require('../electron/dist/protocol/actionFileUrl.js');
const { decideFileAccess } = require('../electron/dist/browser/pagePolicy.js');

const SESSION_MODULE = require.resolve('../electron/dist/browser/browserSession.js');
const PREVIEW_MODULE = require.resolve('../electron/dist/render/htmlPreview.js');
const ON_ELECTRON = /[\\/]electron[\\/]dist[\\/](browser|render)[\\/]/;

/** Fresh copies of the browser engine and the preview, on the fake Electron. */
function loadOnElectron(electron) {
  const fresh = () => {
    for (const key of Object.keys(require.cache)) {
      if (ON_ELECTRON.test(key)) delete require.cache[key];
    }
  };
  fresh();
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return electron;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return { ...require(SESSION_MODULE), ...require(PREVIEW_MODULE) };
  } finally {
    Module._load = originalLoad;
    fresh();
  }
}

/**
 * The preview of an Action whose answer links `linked`: `actionFile:read` and the
 * `pantaray-action-file` protocol over one set of printed PDFs.
 */
function previewHarness({ linked, load }) {
  const fake = fakeBrowserElectron({ load });
  const { createBrowserSessions, createHtmlPreviewPdfs } = loadOnElectron(fake.electron);
  const userDataDir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-user-')));
  const pageOwners = [];
  const sessions = createBrowserSessions({
    getBackendPort: () => 49152,
    pages: { opened: (_id, actionId) => pageOwners.push(actionId), touched() {}, closed() {} },
  });
  const pdfs = createHtmlPreviewPdfs({ userDataDir, sessions });
  const finalOutput = linked.map((file) => `[f](pantaray-file://${encodeURI(file)})`).join('\n');
  const sources = {
    readConversationPage: async ({ actionId }) => {
      if (actionId !== 'act-1') throw new Error('Action not found');
      return {
        action: { action_id: actionId },
        runs: [{ run_id: 'run-1', final_output: finalOutput, entries: [] }],
        next_cursor: null,
      };
    },
    readSuggestionBootstrap: async () => null,
    getCurrentSubjectId: () => 'user-1',
  };
  const handlers = new Map();
  registerActionFileHandlers(
    {
      actions: sources,
      overlay: { resolveOverlayBootstrap: sources.readSuggestionBootstrap },
      actionFiles: { renderHtml: pdfs.render },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  const protocol = createActionFileProtocolHandler({
    ...sources,
    readRenderedPdf: pdfs.readRendered,
  });
  return {
    ...fake,
    pageOwners,
    read: (file) =>
      handlers.get('actionFile:read')({}, { target: { kind: 'action', id: 'act-1' }, path: file }),
    fetch: (url) => protocol({ url }),
  };
}

const DEAD_PROXY = { proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '<-loopback>' };

// Catches: the preview reaching the network, loading outside its folder, printing twice for
// one view, serving a PDF for another file or owner, leaving its session open, or its page
// offered to an Action's chat as that Action's browser.
test('an owned HTML file previews as its PDF, loading its own folder and no network', async () => {
  const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-html-preview-')));
  const deck = path.join(work, 'deck', 'deck.html');
  fs.mkdirSync(path.dirname(deck));
  fs.writeFileSync(deck, '<p>deck</p>');
  fs.writeFileSync(path.join(work, 'deck', 'chart.png'), 'png');
  fs.writeFileSync(path.join(work, 'secret.txt'), 'secret');
  const unowned = path.join(work, 'deck', 'other.html');
  fs.writeFileSync(unowned, '<p>other</p>');
  const loads = [];
  const h = previewHarness({
    linked: [deck],
    load: async (url, { ses }) => {
      assert.equal(url, pathToFileURL(deck).href);
      loads.push(['proxies', [...ses.proxies]]);
      for (const file of ['deck/deck.html', 'deck/chart.png', 'secret.txt']) {
        const response = await ses.fileHandler({ url: pathToFileURL(path.join(work, file)).href });
        loads.push([file, response.type]);
      }
      for (const url of ['https://cdn.example.com/chart.js', 'http://127.0.0.1:5173/']) {
        ses.beforeRequest({ url, method: 'GET' }, (decision) => loads.push([url, decision]));
      }
    },
  });

  const result = await h.read(deck);
  assert.equal(result.kind, 'pdf');
  const renderId = new URL(result.url).searchParams.get('render');
  assert.equal(result.url, buildActionFileUrl({ kind: 'action', id: 'act-1' }, deck, renderId));
  assert.deepEqual(loads, [
    ['proxies', [DEAD_PROXY]],
    ['deck/deck.html', 'default'],
    ['deck/chart.png', 'default'],
    ['secret.txt', 'error'],
    ['https://cdn.example.com/chart.js', { cancel: true }],
    ['http://127.0.0.1:5173/', { cancel: true }],
  ]);
  assert.equal(h.windows.length, 1);
  assert.deepEqual(h.pageOwners, [null]);
  assert.equal(h.windows[0].destroyed, true);
  assert.equal(h.windows[0].shown, false);

  // The viewer may ask again; the PDF is served from memory, not printed twice.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await h.fetch(result.url);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-type'), 'application/pdf');
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), PDF);
  }
  assert.equal(h.windows.length, 1);

  // The render id serves only the file it was printed from, for its owner.
  const swapped = buildActionFileUrl({ kind: 'action', id: 'act-1' }, unowned, renderId);
  assert.equal((await h.fetch(swapped)).status, 404);
  const otherOwner = buildActionFileUrl({ kind: 'action', id: 'act-2' }, deck, renderId);
  assert.equal((await h.fetch(otherOwner)).status, 404);
  assert.deepEqual(await h.read(unowned), { kind: 'unavailable', reason: 'not_found' });
  assert.equal(h.windows.length, 1);
});

// Catches: a failed load reported as a PDF, or a failed preview leaving its session open.
test('a preview whose page fails to load reads as render_failed and closes its session', async () => {
  const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-html-preview-')));
  const deck = path.join(work, 'deck.html');
  fs.writeFileSync(deck, '<p>deck</p>');
  const h = previewHarness({
    linked: [deck],
    load: async () => Promise.reject(Object.assign(new Error('x'), { code: 'ERR_FAILED' })),
  });
  assert.deepEqual(await h.read(deck), { kind: 'unavailable', reason: 'render_failed' });
  assert.equal(h.windows.length, 1);
  assert.equal(h.windows[0].destroyed, true);
});

const notSameDirectory = () => false;

// Catches: the preview reading private app storage, or losing its own workspace folder there.
test('the preview loads its own folder in private storage, and no private storage below it', () => {
  const { previewFileScope } = loadOnElectron(fakeBrowserElectron().electron);
  const privateRoots = ['/Users/me/Library/Pantaray', '/Users/me/Library/Caches/temp'];
  const workspace = '/Users/me/Library/Pantaray/actions/u/a/workspace';
  const inWorkspace = previewFileScope(`${workspace}/deck.html`, privateRoots, notSameDirectory);
  assert.deepEqual(inWorkspace, {
    fileRoots: [workspace],
    privateRoots,
    privateRootExceptions: [workspace],
    secretPaths: [],
  });
  const decide = (scope, target) => decideFileAccess(target, scope, notSameDirectory);
  assert.equal(decide(inWorkspace, `${workspace}/img/chart.png`), null);
  assert.equal(decide(inWorkspace, '/Users/me/Library/Pantaray/db.sqlite3'), 'outside_read_scope');

  const inHome = previewFileScope('/Users/me/deck.html', privateRoots, notSameDirectory);
  assert.deepEqual(inHome.privateRootExceptions, []);
  assert.equal(decide(inHome, '/Users/me/img/chart.png'), null);
  assert.equal(decide(inHome, '/Users/me/Library/Pantaray/db.sqlite3'), 'outside_read_scope');
});
