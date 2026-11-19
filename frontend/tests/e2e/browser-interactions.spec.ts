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

type Result = {
  status: 'ok' | 'failed';
  code?: string;
  truncated?: string | null;
  page_data?: { snapshot: string | null; console: { level: string; text: string }[] | null };
  artifact?: { width_px?: number; height_px?: number } | null;
};

// eslint-disable-next-line no-empty-pattern
test('the browser acts on a real page through snapshot refs', async ({}, testInfo) => {
  const server: Server = createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end(FORM);
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
            const answers: unknown[] = [];
            const respond = createBrowserResponder({
              // No page here reaches this port.
              sessions: createBrowserSessions({ getBackendPort: () => 9 }),
              localArtifactRoot: args.artifactRoot,
              postAnswer: async (body: unknown) => answers.push(body),
            });
            g.e2eInteractions = { respond, answers };
          }
          const id = `interaction-${args.next}`;
          await g.e2eInteractions.respond({
            operation_request_id: id,
            session_id: 'interactions-e2e',
            action_id: 'action-e2e',
            process_id: 'process-e2e',
            tool_request_id: 'tool-e2e',
            operation: args.operation,
            policy: {
              allow_network: true,
              file_roots: [],
              private_roots: [],
              private_root_exceptions: [],
              secret_paths: [],
              write_hosts: [],
            },
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
  } finally {
    server.close();
    fs.rmSync(artifactRoot, { recursive: true, force: true });
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
