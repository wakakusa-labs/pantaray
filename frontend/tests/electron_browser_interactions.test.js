const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const { fakeBrowserElectron } = require('./helpers/browser_electron_fakes');

const DIST = path.join(__dirname, '../electron/dist/browser');
const { MAX_SNAPSHOT_CHARS } = require(`${DIST}/snapshot.js`);

/** A page whose accessibility tree, document and nodes the test sets; CDP answers from it. */
function harness() {
  const page = { loaderId: 'doc-1', nodes: [], gone: new Set(), native: new Set(), refusal: null };
  let boxed = null;
  const fake = fakeBrowserElectron({
    cdp: async (method, params) => {
      if (method === 'Page.getFrameTree')
        return { frameTree: { frame: { loaderId: page.loaderId } } };
      if (method === 'Accessibility.getFullAXTree') return { nodes: page.nodes };
      if (method === 'DOM.resolveNode') {
        if (page.gone.has(params.backendNodeId)) throw new Error('No node with given id found');
        return { object: { objectId: String(params.backendNodeId) } };
      }
      if (method === 'Runtime.callFunctionOn') {
        const node = Number(params.objectId);
        const fn = params.functionDeclaration;
        if (fn.includes('isConnected')) return { result: { value: true } };
        if (fn.includes("closest('label')")) return { result: { value: page.native.has(node) } };
        if (fn.includes('this.options')) return { result: { value: page.refusal } };
        return { result: { value: true } };
      }
      if (method === 'DOM.getContentQuads') {
        boxed = params.backendNodeId;
        return { quads: [[10, 20, 110, 20, 110, 60, 10, 60]] };
      }
      if (method === 'DOM.getNodeForLocation') return { backendNodeId: boxed };
      return {};
    },
  });
  const modules = ['browserSession', 'browserResponder', 'capture', 'snapshot', 'interactions'];
  const fresh = () => modules.forEach((name) => delete require.cache[`${DIST}/${name}.js`]);
  fresh();
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    return request === 'electron'
      ? fake.electron
      : originalLoad.call(this, request, parent, isMain);
  };
  let loaded;
  try {
    loaded = { ...require(`${DIST}/browserSession.js`), ...require(`${DIST}/browserResponder.js`) };
  } finally {
    Module._load = originalLoad;
    fresh();
  }
  const answers = [];
  const respond = loaded.createBrowserResponder({
    sessions: loaded.createBrowserSessions({ getBackendPort: () => 49152 }),
    localArtifactRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-browser-')),
    postAnswer: async (body) => answers.push(body),
  });
  let next = 0;
  const run = async (operation) => {
    next += 1;
    await respond({
      operation_request_id: `op-${next}`,
      session_id: 'session-1',
      action_id: 'action-1',
      process_id: 'process-1',
      tool_request_id: 'tool-1',
      operation,
      policy: {
        allow_network: true,
        file_roots: [],
        private_roots: [],
        private_root_exceptions: [],
        secret_paths: [],
        write_hosts: [],
      },
    });
    return answers.at(-1).result;
  };
  const commands = () => fake.windows[0].webContents.debugger.commands;
  const mouse = () => commands().filter((command) => command.method === 'Input.dispatchMouseEvent');
  return { page, run, fake, commands, mouse };
}

const ax = (nodeId, role, name, backendDOMNodeId, extra = {}) => ({
  nodeId,
  ignored: false,
  role: { value: role },
  name: { value: name },
  backendDOMNodeId,
  ...extra,
});
const root = (...children) => ax('root', 'RootWebArea', 'Page', 1, { childIds: children });
const opened = async () => {
  const h = harness();
  await h.run({ action: 'navigate', target: { kind: 'web', url: 'https://a.example/' } });
  return h;
};

// Catches: a ref that changes or names another node across snapshots, a ref that outlives its
// document, a subtree snapshot that shows the whole page, and page text outside `snapshot`.
test('refs name the same node across snapshots and die with their document', async () => {
  const h = await opened();
  const tree = [
    ax('h', 'heading', 'Counter', 2, { properties: [{ name: 'level', value: { value: 1 } }] }),
    ax('b', 'button', 'Add one', 3, { childIds: ['bt'] }),
    ax('bt', 'StaticText', 'Add one', 30),
    ax('c', 'combobox', 'Color', 4, { value: { value: 'Red' }, childIds: ['o1', 'o2'] }),
    ax('o1', 'option', 'Red', 5, { properties: [{ name: 'selected', value: { value: true } }] }),
    ax('o2', 'option', 'Blue', 6),
    ax('t', 'StaticText', 'Count 0', 7),
  ];
  h.page.nodes = [root('h', 'b', 'c', 't'), ...tree];
  const first = await h.run({ action: 'snapshot', ref: null });
  assert.equal(
    first.page_data.snapshot,
    [
      '- heading "Counter" [level=1] [ref=e1]',
      '- button "Add one" [ref=e2]',
      '- combobox "Color" [ref=e3]: Red',
      '  - option "Red" [selected] [ref=e4]',
      '  - option "Blue" [ref=e5]',
      '- text: Count 0',
    ].join('\n')
  );
  assert.equal(first.truncated, null);

  h.page.nodes = [root('c', 'h'), ...tree];
  const second = await h.run({ action: 'snapshot', ref: null });
  assert.match(
    second.page_data.snapshot,
    /combobox "Color" \[ref=e3\][\s\S]*heading "Counter" .*\[ref=e1\]/
  );
  assert.equal((await h.run({ action: 'click', ref: 'e2' })).code, 'BROWSER_REF_UNKNOWN');
  const subtree = await h.run({ action: 'snapshot', ref: 'e3' });
  assert.equal(subtree.page_data.snapshot.split('\n')[0], '- combobox "Color" [ref=e3]: Red');
  assert.doesNotMatch(subtree.page_data.snapshot, /heading/);

  h.page.loaderId = 'doc-2';
  assert.equal((await h.run({ action: 'hover', ref: 'e3' })).code, 'BROWSER_REF_UNKNOWN');
  assert.deepEqual(h.mouse(), []);
});

// Catches: a click sent to whatever now sits where a removed element was.
test('a ref whose node left the page is stale and nothing is clicked', async () => {
  const h = await opened();
  h.page.nodes = [root('b'), ax('b', 'button', 'Remove me', 3)];
  await h.run({ action: 'snapshot', ref: null });
  h.page.gone.add(3);
  assert.equal((await h.run({ action: 'click', ref: 'e1' })).code, 'BROWSER_REF_STALE');
  assert.deepEqual(h.mouse(), []);
});

// Catches: a snapshot past its bound sent whole or cut without saying so, and refs handed out for
// lines that were cut.
test('a long snapshot is cut at a line, says so, and names only what it shows', async () => {
  const h = await opened();
  const ids = Array.from({ length: 2_000 }, (_, i) => `n${i}`);
  h.page.nodes = [
    root(...ids),
    ...ids.map((id, i) => ax(id, 'button', `Button number ${i}`, 10 + i)),
  ];
  const cut = await h.run({ action: 'snapshot', ref: null });
  assert.equal(cut.truncated, 'snapshot_too_long');
  assert.ok(cut.page_data.snapshot.length <= MAX_SNAPSHOT_CHARS);
  const last = Number(/\[ref=e(\d+)\]$/.exec(cut.page_data.snapshot)[1]);
  assert.ok(last < 2_000);
  assert.equal((await h.run({ action: 'hover', ref: `e${last}` })).status, 'ok');
  assert.equal((await h.run({ action: 'hover', ref: `e${last + 1}` })).code, 'BROWSER_REF_UNKNOWN');
});

// Catches: a click on a list box or picker, which opens native UI on the user's display, and a
// select that clicks or reports a missing option as done.
test('no click reaches a list box or picker; select sets options without the mouse', async () => {
  const h = await opened();
  h.page.nodes = [root('s', 'b'), ax('s', 'combobox', 'Color', 3), ax('b', 'button', 'Go', 4)];
  await h.run({ action: 'snapshot', ref: null });
  h.page.native.add(3);
  const refused = await h.run({ action: 'click', ref: 'e1' });
  assert.equal(refused.code, 'BROWSER_ACTION_FAILED');
  assert.equal((await h.run({ action: 'select', ref: 'e1', values: ['Blue'] })).status, 'ok');
  h.page.refusal = 'A value is not an option of this list box.';
  const missing = await h.run({ action: 'select', ref: 'e1', values: ['Green'] });
  assert.equal(missing.code, 'BROWSER_ACTION_FAILED');
  assert.deepEqual(h.mouse(), []);

  assert.equal((await h.run({ action: 'click', ref: 'e2' })).status, 'ok');
  assert.deepEqual(
    h.mouse().map(({ params }) => [params.type, params.x, params.y]),
    ['mouseMoved', 'mousePressed', 'mouseReleased'].map((type) => [type, 60, 40])
  );
});

// Catches: a shortcut that types its letter, and a chord whose modifier never reaches the key.
test('press sends a chord with its modifiers and text only without a shortcut modifier', async () => {
  const h = await opened();
  await h.run({ action: 'press', key: 'Control+a' });
  await h.run({ action: 'press', key: 'Shift+A' });
  const keys = h
    .commands()
    .filter(({ method, params }) => method === 'Input.dispatchKeyEvent' && params.key.length === 1)
    .map(({ params }) => [params.type, params.modifiers, params.text ?? null]);
  const [control, shift] = [2, 8];
  assert.deepEqual(keys, [
    ['rawKeyDown', control, null],
    ['keyUp', control, null],
    ['keyDown', shift, 'A'],
    ['keyUp', shift, null],
  ]);
  assert.equal((await h.run({ action: 'press', key: 'Hyper+x' })).code, 'BROWSER_ACTION_FAILED');
});
