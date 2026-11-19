import { test, expect, type ElectronApplication } from '@playwright/test';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import path from 'node:path';
import { launchElectronE2E } from './harness';

const SESSION_MODULE = path.join(__dirname, '../../electron/dist/browser/browserSession.js');
// A worker that ran would open this; the host is never approved for writes.
const WORKER_SOCKET = "new WebSocket('wss://example.com/')";

/** What a frame still has of each API the guard removes; every entry must be `undefined`. */
const PROBE = `(w) => [
  typeof w.RTCPeerConnection, typeof w.webkitRTCPeerConnection, typeof w.WebTransport,
  typeof w.EyeDropper, typeof w.showOpenFilePicker, typeof w.showSaveFilePicker,
  typeof w.showDirectoryPicker, typeof w.HTMLVideoElement.prototype.requestPictureInPicture,
  typeof w.navigator.bluetooth,
].join()`;
const NOTHING_LEFT = Array(9).fill('undefined').join();

/** A frame that reports its probe on load and whenever its parent asks. */
function childScript(from: string): string {
  return `<!doctype html><script>
const probe = ${PROBE};
const report = () => parent.postMessage({ from: '${from}', probe: probe(window) }, '*');
addEventListener('message', report);
report();
</script>`;
}

function pages(crossSiteOrigin: string): Record<string, string> {
  return {
    '/workers.html': `<!doctype html><body><script>
window.done = (async () => {
  const errored = (worker) => new Promise((resolve) => {
    worker.onerror = () => resolve('error');
    setTimeout(() => resolve('running'), 3000);
  });
  const blob = URL.createObjectURL(new Blob(["${WORKER_SOCKET}"], { type: 'text/javascript' }));
  const outcome = async (create) => { try { return await errored(create()); } catch (e) { return e.name; } };
  let service;
  try { await navigator.serviceWorker.register('/sw.js'); service = 'registered'; } catch (e) { service = e.name; }
  return {
    dedicated: await outcome(() => new Worker('/w.js')),
    blob: await outcome(() => new Worker(blob)),
    shared: await outcome(() => new SharedWorker('/shared.js')),
    service,
  };
})();
</script></body>`,
    '/guarded.html': `<!doctype html><body><script>
const probe = ${PROBE};
const blank = document.createElement('iframe');
document.body.appendChild(blank);
const initial = { main: probe(window), blank: probe(blank.contentWindow) };
const children = {};
const replies = new Map();
addEventListener('message', (event) => {
  replies.set(event.data.from, event.data.probe);
  children[event.data.from] = event.source;
});
const both = () => new Promise((resolve) => {
  const check = () => (replies.size === 2 ? resolve(Object.fromEntries(replies)) : setTimeout(check, 50));
  check();
});
window.done = both().then((frames) => ({ ...initial, ...frames }));
// Asks every frame again, after the click that gave the page user activation.
window.again = () => {
  replies.clear();
  for (const child of Object.values(children)) child.postMessage('probe', '*');
  return both().then((frames) => ({ main: probe(window), blank: probe(blank.contentWindow), ...frames }));
};
const sandboxed = document.createElement('iframe');
sandboxed.sandbox = 'allow-scripts';
// Escaped so the srcdoc's closing tag does not end this script.
sandboxed.srcdoc = ${JSON.stringify(childScript('sandboxed')).replace(/</g, '\\u003c')};
document.body.appendChild(sandboxed);
const crossSite = document.createElement('iframe');
crossSite.src = '${crossSiteOrigin}/guarded-child.html';
document.body.appendChild(crossSite);
</script></body>`,
    '/guarded-child.html': childScript('crossSite'),
    '/ui.html': `<!doctype html><body><input type="file" id="file"><p>printable</p></body>`,
    '/w.js': WORKER_SOCKET,
    '/shared.js': WORKER_SOCKET,
    '/sw.js': WORKER_SOCKET,
  };
}

type Settled = {
  status: 'ok' | 'failed';
  value?: unknown;
  code?: string;
  report?: { notes: string[]; blocked: { reason: string; url: string }[]; network: unknown[] };
};

/** Runs one operation in the app's main process through the session module under test. */
function run(
  app: ElectronApplication,
  call: {
    session: string;
    url: string | null;
    expression: string | null;
    print?: boolean;
    click?: boolean;
  }
): Promise<Settled> {
  return app.evaluate(
    async (_electron, args) => {
      type Page = { contents: Electron.WebContents; cdp: Electron.Debugger };
      type Sessions = {
        run: (request: object, budgetMs: number, work: (page: Page) => Promise<unknown>) => unknown;
      };
      const g = globalThis as { e2eBrowserSessions?: Sessions };
      if (g.e2eBrowserSessions === undefined) {
        const nodeModule = process.getBuiltinModule('node:module');
        const sessionModule = nodeModule.createRequire(args.modulePath)(args.modulePath);
        // No page in these specs reaches this port.
        g.e2eBrowserSessions = sessionModule.createBrowserSessions({ getBackendPort: () => 9 });
      }
      const sessions = g.e2eBrowserSessions as Sessions;
      const request = {
        operation_request_id: 'op-e2e',
        session_id: args.session,
        action_id: 'action-e2e',
        process_id: 'process-e2e',
        tool_request_id: 'tool-e2e',
        operation: args.url
          ? { action: 'navigate', target: { kind: 'web', url: args.url } }
          : { action: 'console' },
        policy: {
          allow_network: true,
          file_roots: [],
          private_roots: [],
          private_root_exceptions: [],
          secret_paths: [],
          write_hosts: [],
        },
      };
      return sessions.run(request, 20_000, async (page: Page) => {
        if (args.url) await page.contents.loadURL(args.url);
        if (args.print) return (await page.contents.printToPDF({})).subarray(0, 5).toString();
        if (args.click) {
          for (const type of ['mousePressed', 'mouseReleased']) {
            await page.cdp.sendCommand('Input.dispatchMouseEvent', {
              type,
              x: 5,
              y: 5,
              button: 'left',
              clickCount: 1,
            });
          }
        }
        if (!args.expression) return null;
        const { result } = await page.cdp.sendCommand('Runtime.evaluate', {
          expression: args.expression,
          awaitPromise: true,
          returnByValue: true,
          userGesture: true,
        });
        return result.value;
      });
    },
    { ...call, print: call.print ?? false, click: call.click ?? false, modulePath: SESSION_MODULE }
  ) as Promise<Settled>;
}

// eslint-disable-next-line no-empty-pattern
test('a browser session page opens no worker, WebRTC, WebTransport or OS dialog', async ({}, testInfo) => {
  const hits: string[] = [];
  const server: Server = createServer((request, response) => {
    hits.push(request.url ?? '');
    const body = pages(`http://localhost:${(server.address() as AddressInfo).port}`)[
      request.url ?? ''
    ];
    response.writeHead(body === undefined ? 404 : 200, {
      'Content-Type': request.url?.endsWith('.js') ? 'text/javascript' : 'text/html',
    });
    response.end(body ?? '');
  });
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const { harness, stop } = await launchElectronE2E();
  const { app } = harness;
  try {
    await app.evaluate(({ app: electronApp, BrowserWindow }) => {
      for (const win of BrowserWindow.getAllWindows()) win.hide();
      electronApp.hide();
    });
    const windowsBefore = await app.evaluate(
      ({ BrowserWindow }) => BrowserWindow.getAllWindows().length
    );

    await test.step('no worker starts, so none can open a socket webRequest misses', async () => {
      const settled = await run(app, {
        session: 'workers',
        url: `${origin}/workers.html`,
        expression: 'window.done',
      });
      expect(settled.status).toBe('ok');
      const outcomes = settled.value as Record<string, string>;
      expect(outcomes.dedicated).toBe('error');
      expect(outcomes.blob).toBe('error');
      expect(outcomes.shared).not.toBe('running');
      expect(outcomes.service).not.toBe('registered');
      expect(hits.filter((hit) => hit.endsWith('.js'))).toEqual([]);
      expect(settled.report?.blocked.map((blocked) => blocked.url)).toContain(`${origin}/w.js`);
    });

    await test.step('WebRTC, WebTransport and the OS pickers are gone in every frame', async () => {
      const everyFrame = {
        main: NOTHING_LEFT,
        blank: NOTHING_LEFT,
        sandboxed: NOTHING_LEFT,
        crossSite: NOTHING_LEFT,
      };
      const loaded = await run(app, {
        session: 'guarded',
        url: `${origin}/guarded.html`,
        expression: 'window.done',
      });
      expect(loaded.value).toEqual(everyFrame);
      const clicked = await run(app, {
        session: 'guarded',
        url: null,
        click: true,
        expression: 'window.again()',
      });
      expect(clicked.value).toEqual(everyFrame);
    });

    await test.step('a page print is ignored and reported; the tool still prints', async () => {
      await run(app, { session: 'ui', url: `${origin}/ui.html`, expression: null });
      const printed = await run(app, {
        session: 'ui',
        url: null,
        // The reports arrive as CDP events; a moment lets them land before the answer.
        expression:
          "window.print(); document.execCommand('print'); new Promise((r) => setTimeout(() => r('asked'), 300))",
      });
      expect(printed.value).toBe('asked');
      expect(printed.report?.notes).toEqual(['dialog_dismissed']);
      const pdf = await run(app, { session: 'ui', url: null, expression: null, print: true });
      expect(pdf.value).toBe('%PDF-');
    });

    await test.step('a file chooser is intercepted and reported', async () => {
      const chose = await run(app, {
        session: 'ui',
        url: null,
        expression:
          "document.getElementById('file').click(); new Promise((r) => setTimeout(() => r(typeof showOpenFilePicker), 300))",
      });
      expect(chose.value).toBe('undefined');
      expect(chose.report?.notes).toEqual(['dialog_dismissed']);
    });

    await test.step('nothing became a window or visible', async () => {
      const state = await app.evaluate(({ BrowserWindow }) => ({
        count: BrowserWindow.getAllWindows().length,
        visible: BrowserWindow.getAllWindows().filter((win) => win.isVisible()).length,
      }));
      // One hidden page window per session opened above.
      expect(state).toEqual({ count: windowsBefore + 3, visible: 0 });
    });
  } finally {
    server.close();
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
