import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { launchElectronE2E } from './harness';

const DIST = path.join(__dirname, '../../electron/dist');

const FORM = `<!doctype html><title>Form</title><body style="margin:0">
<h1>Counter</h1>
<button id="add" style="width:200px;height:60px;border:0;background:rgb(255,0,0)">Add one</button>
<p id="count">Count 0</p>
<label>Name <input id="name"></label><p id="hello"></p>
<label>Color <select id="color"><option value="r">Red</option><option value="b">Blue</option></select></label>
<p id="chosen"></p>
<button id="later">Later</button>
<div style="height:3000px"></div>
<script>
add.onclick = () => { count.textContent = 'Count ' + (Number(count.textContent.slice(6)) + 1); };
document.getElementById('name').oninput = (e) => { hello.textContent = 'Hello, ' + e.target.value; };
color.onchange = () => { chosen.textContent = 'Chosen ' + color.value; };
later.onclick = () => setTimeout(() => {
  later.remove();
  document.body.append(Object.assign(document.createElement('p'), { textContent: 'Ready now' }));
  console.warn('ready-line');
}, 500);
</script></body>`;

// A transparent layer over Cancel that would turn a click on it into Confirm.
const OVERLAY = `<!doctype html><title>Overlay</title><body style="margin:0">
<button id="cancel" style="position:absolute;left:0;top:0;width:200px;height:60px">Cancel</button>
<div id="cover" style="position:absolute;left:0;top:0;width:200px;height:60px"></div>
<p id="log" style="margin-top:100px">Nothing yet</p>
<script>
cover.onmousemove = () => (log.textContent = 'Hovered the cover');
cover.onclick = () => (log.textContent = 'Confirmed');
cancel.onclick = () => (log.textContent = 'Canceled');
</script></body>`;

const PICKERS = `<!doctype html><title>Pickers</title><body>
<label>Name <input id="name"></label>
<label>Size <select id="size"><option>Small</option><option>Large</option></select></label>
<label>Shade <input id="shade" type="color"></label>
<label>Visit <input id="visit" type="date"></label>
<label>Fruit <input id="fruit" list="fruits"></label>
<datalist id="fruits"><option value="Apple"><option value="Pear"></datalist>
</body>`;

// A list sorted by pointer events, whose dragged card follows the pointer, and an HTML drag and
// drop of a box into a bin.
const DRAG = `<!doctype html><title>Drag</title><body style="margin:0;user-select:none">
<ul id="cards" style="list-style:none;margin:0;padding:0;width:200px">
<li aria-label="First card" style="height:40px">First card</li>
<li aria-label="Second card" style="height:40px">Second card</li>
<li aria-label="Third card" style="height:40px">Third card</li>
</ul>
<p id="order">Order: First card, Second card, Third card</p>
<div id="box" role="button" draggable="true" style="width:120px;height:40px">Box</div>
<div id="bin" role="region" aria-label="Bin" style="margin-top:40px;width:200px;height:80px">Bin</div>
<p id="dropped">Nothing dropped</p>
<script>
let dragging = null;
let startY = 0;
cards.onpointerdown = (e) => { dragging = e.target.closest('li'); startY = e.clientY; };
onpointermove = (e) => {
  if (dragging !== null) dragging.style.transform = 'translateY(' + (e.clientY - startY) + 'px)';
};
onpointerup = (e) => {
  if (dragging === null) return;
  dragging.style.transform = '';
  const under = document.elementsFromPoint(e.clientX, e.clientY)
    .find((el) => el.localName === 'li' && el !== dragging);
  under?.after(dragging);
  dragging = null;
  order.textContent = 'Order: ' + [...cards.children].map((li) => li.ariaLabel).join(', ');
};
box.ondragstart = (e) => e.dataTransfer.setData('text/plain', 'box-1');
bin.ondragover = (e) => e.preventDefault();
bin.ondrop = (e) => {
  e.preventDefault();
  dropped.textContent = 'Dropped ' + e.dataTransfer.getData('text/plain');
};
</script></body>`;

const PAGES: Record<string, string> = {
  '/form.html': FORM,
  '/overlay.html': OVERLAY,
  '/pickers.html': PICKERS,
  '/drag.html': DRAG,
};

type Result = {
  status: 'ok' | 'failed';
  code?: string;
  message?: string;
  truncated?: string | null;
  page_data?: { snapshot: string | null; console: { level: string; text: string }[] | null };
  artifact?: { width_px?: number; height_px?: number } | null;
};

// eslint-disable-next-line no-empty-pattern
test('the browser acts on a real page through snapshot refs', async ({}, testInfo) => {
  const server: Server = createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end(PAGES[request.url ?? '']);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const artifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-browser-e2e-'));
  const { harness, stop } = await launchElectronE2E();
  const { app } = harness;
  try {
    await app.evaluate(({ app: electronApp, BrowserWindow }) => {
      for (const win of BrowserWindow.getAllWindows()) win.hide();
      electronApp.hide();
    });
    let next = 0;
    // Runs the real responder in the app's main process and returns its answer's result; a
    // screenshot also returns the color near its top left corner, clear of the button's label.
    const run = (operation: object) => {
      next += 1;
      return app.evaluate(
        async ({ nativeImage }, args) => {
          const g = globalThis as {
            e2eInteractions?: { respond: (request: object) => Promise<void>; answers: unknown[] };
          };
          if (g.e2eInteractions === undefined) {
            const load = process.getBuiltinModule('node:module').createRequire(args.dist);
            const { createBrowserSessions } = load(`${args.dist}/browser/browserSession.js`);
            const { createBrowserResponder } = load(`${args.dist}/browser/browserResponder.js`);
            const { createUserBrowserSessions } = load(
              `${args.dist}/browser/userBrowserSession.js`
            );
            const answers: unknown[] = [];
            const respond = createBrowserResponder({
              // No page here reaches this port.
              // No window here watches the page.
              sessions: createBrowserSessions({
                getBackendPort: () => 9,
                pages: { opened() {}, touched() {}, closed() {} },
              }),
              // Every request here is for the app's page; none connects to a browser.
              userSessions: createUserBrowserSessions({
                connect: () => Promise.reject(new Error('not used')),
                getBackendPort: () => 9,
              }),
              localArtifactRoot: args.artifactRoot,
              postAnswer: async (body: unknown) => answers.push(body),
            });
            g.e2eInteractions = { respond, answers };
          }
          const id = `interaction-${args.next}`;
          await g.e2eInteractions.respond({
            operation_request_id: id,
            session_id: 'interactions-e2e',
            action_id: null,
            operation: args.operation,
            policy: {
              allow_network: true,
              file_roots: [],
              private_roots: [],
              private_root_exceptions: [],
              secret_paths: [],
              write_hosts: [],
            },
            user_browser: null,
          });
          const { result } = g.e2eInteractions.answers.at(-1) as { result: Result };
          if (result.status !== 'ok' || result.artifact == null) return { result, corner: null };
          const image = nativeImage.createFromPath(`${args.artifactRoot}/.tmp/browser/${id}.png`);
          const pixel = image.crop({ x: 4, y: 4, width: 1, height: 1 }).toBitmap();
          // BGRA.
          return { result, corner: [pixel[2], pixel[1], pixel[0]] };
        },
        { dist: DIST, artifactRoot, operation, next }
      ) as Promise<{ result: Result; corner: number[] | null }>;
    };
    const act = async (operation: object) => (await run(operation)).result;
    const snapshot = async () =>
      (await act({ action: 'snapshot', ref: null })).page_data?.snapshot ?? '';
    const refOf = (text: string, line: string) => {
      const ref = new RegExp(`${line} \\[ref=(e\\d+)\\]`).exec(text)?.[1];
      expect(ref, `${line} in\n${text}`).toBeTruthy();
      return ref as string;
    };

    await act({ action: 'navigate', target: { kind: 'web', url: `${origin}/form.html` } });
    const page = await snapshot();
    expect(page).toContain('- heading "Counter" [level=1] [ref=');
    const add = refOf(page, 'button "Add one"');

    await test.step('a click by ref changes the page', async () => {
      expect((await act({ action: 'click', ref: add })).status).toBe('ok');
      expect(await snapshot()).toContain('Count 1');
    });

    await test.step('type replaces the field text', async () => {
      const typed = await act({ action: 'type', ref: refOf(page, 'textbox "Name"'), text: 'Ada' });
      expect(typed.status).toBe('ok');
      expect(await snapshot()).toContain('Hello, Ada');
    });

    await test.step('select sets an option; a click on the list box is refused', async () => {
      const color = refOf(page, 'combobox "Color"');
      expect((await act({ action: 'click', ref: color })).code).toBe('BROWSER_ACTION_FAILED');
      expect((await act({ action: 'select', ref: color, values: ['Blue'] })).status).toBe('ok');
      expect(await snapshot()).toContain('Chosen b');
    });

    await test.step('wait sees delayed text, the console has its line, a gone ref is stale', async () => {
      const later = refOf(page, 'button "Later"');
      await act({ action: 'click', ref: later });
      expect((await act({ action: 'wait', text: 'Ready now' })).status).toBe('ok');
      const logged = await act({ action: 'console' });
      expect(logged.page_data?.console).toContainEqual({ level: 'warning', text: 'ready-line' });
      expect((await act({ action: 'click', ref: later })).code).toBe('BROWSER_REF_STALE');
    });

    await test.step('a ref screenshot is the element box, wherever the page is scrolled', async () => {
      await act({ action: 'scroll', ref: null, direction: 'down' });
      const shot = await run({ action: 'screenshot', ref: add });
      expect(shot.result.artifact).toMatchObject({ width_px: 200, height_px: 60 });
      expect(shot.corner).toEqual([255, 0, 0]);
    });

    await test.step('a click or hover on a covered element is refused and reaches nothing', async () => {
      await act({ action: 'navigate', target: { kind: 'web', url: `${origin}/overlay.html` } });
      const cancel = refOf(await snapshot(), 'button "Cancel"');
      for (const action of ['click', 'hover']) {
        const refused = await act({ action, ref: cancel });
        expect(refused.code).toBe('BROWSER_ACTION_FAILED');
        expect(refused.message).toContain('Another element covers this one');
      }
      expect(await snapshot()).toContain('Nothing yet');
    });

    await test.step('drag moves a card of a pointer list and drops a box in a bin', async () => {
      await act({ action: 'navigate', target: { kind: 'web', url: `${origin}/drag.html` } });
      const board = await snapshot();
      const sorted = await act({
        action: 'drag',
        ref: refOf(board, 'listitem "First card"'),
        to_ref: refOf(board, 'listitem "Third card"'),
      });
      expect(sorted.status).toBe('ok');
      const dropped = await act({
        action: 'drag',
        ref: refOf(board, 'button "Box"'),
        to_ref: refOf(board, 'region "Bin"'),
      });
      expect(dropped.status).toBe('ok');
      const after = await snapshot();
      expect(after).toContain('Order: Second card, Third card, First card');
      expect(after).toContain('Dropped box-1');
    });

    await test.step('no click, typing or opening key reaches a list or picker field', async () => {
      await act({ action: 'navigate', target: { kind: 'web', url: `${origin}/pickers.html` } });
      const fields = await snapshot();
      expect((await act({ action: 'click', ref: refOf(fields, 'combobox "Size"') })).code).toBe(
        'BROWSER_ACTION_FAILED'
      );
      for (const label of ['Shade', 'Visit']) {
        const ref = new RegExp(`"${label}" \\[ref=(e\\d+)\\]`).exec(fields)?.[1];
        expect(ref, `${label} in\n${fields}`).toBeTruthy();
        expect((await act({ action: 'click', ref })).code, label).toBe('BROWSER_ACTION_FAILED');
      }
      // A suggestion list opens nothing for a hidden window, so its field takes input.
      const fruit = refOf(fields, 'combobox "Fruit"');
      expect((await act({ action: 'click', ref: fruit })).status).toBe('ok');
      expect((await act({ action: 'type', ref: fruit, text: 'Ap' })).status).toBe('ok');
      // Shift+Tab lands on the date field, where only the keys that open its picker are refused.
      expect((await act({ action: 'press', key: 'Shift+Tab' })).status).toBe('ok');
      for (const key of [' ', 'Enter', 'F4', 'Alt+ArrowDown']) {
        expect((await act({ action: 'press', key })).code, key).toBe('BROWSER_ACTION_FAILED');
      }
      for (const key of ['1', 'ArrowUp', 'Backspace']) {
        expect((await act({ action: 'press', key })).status, key).toBe('ok');
      }
      // Tab from the name field lands on the list box; only Tab and Escape reach it there.
      expect(
        (await act({ action: 'type', ref: refOf(fields, 'textbox "Name"'), text: 'Ada' })).status
      ).toBe('ok');
      expect((await act({ action: 'press', key: 'Tab' })).status).toBe('ok');
      for (const key of [' ', 'Enter', 'ArrowDown', 'Alt+ArrowDown']) {
        expect((await act({ action: 'press', key })).code, key).toBe('BROWSER_ACTION_FAILED');
      }
      expect((await act({ action: 'press', key: 'Escape' })).status).toBe('ok');
      const size = refOf(fields, 'combobox "Size"');
      expect((await act({ action: 'select', ref: size, values: ['Large'] })).status).toBe('ok');
      expect(await snapshot()).toMatch(/combobox "Size" \[ref=e\d+\]: Large/);
    });
  } finally {
    server.close();
    fs.rmSync(artifactRoot, { recursive: true, force: true });
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
