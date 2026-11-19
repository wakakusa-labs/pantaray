import { test, expect, type ElectronApplication } from '@playwright/test';
import { execFileSync } from 'node:child_process';
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
  typeof w.showDirectoryPicker,
  typeof w.navigator.bluetooth,
  typeof w.HTMLInputElement.prototype.showPicker, typeof w.HTMLSelectElement.prototype.showPicker,
  // The guard preload exposes nothing to the page.
  typeof w.require, typeof w.process, typeof w.electron,
].join()`;
const NOTHING_LEFT = Array(13).fill('undefined').join();

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
    // A playing video with native controls, drawn from a canvas so no media is fetched.
    '/pip.html': `<!doctype html><body style="margin:0">
<video id="video" controls muted width="640" height="360"></video>
<script>
const canvas = Object.assign(document.createElement('canvas'), { width: 64, height: 36 });
const paint = canvas.getContext('2d');
setInterval(() => { paint.fillStyle = 'hsl(' + (Date.now() % 360) + ', 80%, 50%)'; paint.fillRect(0, 0, 64, 36); }, 50);
const video = document.getElementById('video');
video.srcObject = canvas.captureStream(10);
window.entered = false;
video.addEventListener('enterpictureinpicture', () => (window.entered = true));
window.ready = video.play().then(() => new Promise((resolve) => setTimeout(resolve, 500)));
</script></body>`,
    '/ui.html': `<!doctype html><body><input type="file" id="file"><p>printable</p></body>`,
    // Every kind of field whose list or picker the OS draws, at fixed places (`PICKER_POINTS`).
    '/pickers.html': `<!doctype html><body style="margin:0">
<select id="list" style="${at(0, 0, 160, 30)}"><option>a</option><option>b</option></select>
<input id="color" type="color" style="${at(200, 0, 60, 30)}">
<input id="date" type="date" style="${at(300, 0, 200, 30)}">
<input id="suggest" list="words" style="${at(540, 0, 200, 30)}">
<datalist id="words"><option value="alpha"><option value="alps"></datalist>
<button id="open" style="${at(0, 100, 200, 50)}">open</button>
<div id="host"></div>
<iframe src="${crossSiteOrigin}/pickers-child.html" style="${at(300, 100, 300, 150)};border:0"></iframe>
<script>
const shadow = host.attachShadow({ mode: 'closed' });
shadow.innerHTML = '<input type="color">';
window.tried = [];
open.onclick = () => {
  for (const field of [list, color, date, suggest]) {
    try { field.showPicker(); tried.push('opened'); } catch (error) { tried.push(error.name); }
  }
  color.click();
  shadow.querySelector('input').click();
  color.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
};
</script></body>`,
    // A cross-site frame: a picker field under the click, and a script that clicks another.
    '/pickers-child.html': `<!doctype html><body style="margin:0">
<input id="color" type="color" style="${at(0, 0, 300, 150)}"><input id="time" type="time">
<script>addEventListener('click', () => document.getElementById('time').click());</script>
</body>`,
    '/w.js': WORKER_SOCKET,
    '/shared.js': WORKER_SOCKET,
    '/sw.js': WORKER_SOCKET,
  };
}

/** An absolutely placed box, in CSS pixels. */
function at(left: number, top: number, width: number, height: number): string {
  return `position:absolute;left:${left}px;top:${top}px;width:${width}px;height:${height}px`;
}

/** Where each field of /pickers.html takes a click, in viewport CSS pixels. */
const PICKER_POINTS = {
  list: { x: 80, y: 15 },
  color: { x: 230, y: 15 },
  dateField: { x: 330, y: 15 },
  dateIndicator: { x: 488, y: 15 },
  suggest: { x: 640, y: 15 },
  open: { x: 100, y: 125 },
  frameColor: { x: 450, y: 175 },
};

/** A key and its CDP modifier bits (Alt 1, Shift 8). */
type Key = { key: string; code: string; vk: number; text?: string; modifiers?: number };
const KEYS: Record<string, Key> = {
  shiftTab: { key: 'Tab', code: 'Tab', vk: 9, modifiers: 8 },
  space: { key: ' ', code: 'Space', vk: 32, text: ' ' },
  enter: { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  down: { key: 'ArrowDown', code: 'ArrowDown', vk: 40 },
  altDown: { key: 'ArrowDown', code: 'ArrowDown', vk: 40, modifiers: 1 },
  f4: { key: 'F4', code: 'F4', vk: 115 },
  a: { key: 'a', code: 'KeyA', vk: 65, text: 'a' },
};

/**
 * The windows the app's main process has on the display, as the window server lists them. A list
 * box's NSMenu and a picker's popup window are among them; a hidden window is not.
 */
function onScreenWindows(pid: number): number {
  const script = `ObjC.import('CoreGraphics');
const all = ObjC.deepUnwrap(ObjC.castRefToObject(
  $.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, $.kCGNullWindowID)));
all.filter((window) => window.kCGWindowOwnerPID === ${pid}).length`;
  return Number(
    execFileSync('osascript', ['-l', 'JavaScript', '-e', script], { encoding: 'utf8' })
  );
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 700));

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
    clicks?: { x: number; y: number }[];
    keys?: Key[];
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
        if (args.keys.length > 0) {
          // The hidden window has no focus of its own; keys reach the focused element with this.
          await page.cdp.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
        }
        for (const key of args.keys) {
          const event = {
            key: key.key,
            code: key.code,
            windowsVirtualKeyCode: key.vk,
            modifiers: key.modifiers ?? 0,
          };
          await page.cdp.sendCommand('Input.dispatchKeyEvent', {
            ...event,
            ...(key.text === undefined
              ? { type: 'rawKeyDown' }
              : { type: 'keyDown', text: key.text, unmodifiedText: key.text }),
          });
          await page.cdp.sendCommand('Input.dispatchKeyEvent', { ...event, type: 'keyUp' });
        }
        for (const point of args.clicks) {
          for (const type of ['mousePressed', 'mouseReleased']) {
            await page.cdp.sendCommand('Input.dispatchMouseEvent', {
              type,
              ...point,
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
    {
      ...call,
      print: call.print ?? false,
      clicks: call.clicks ?? [],
      keys: call.keys ?? [],
      modulePath: SESSION_MODULE,
    }
  ) as Promise<Settled>;
}

// eslint-disable-next-line no-empty-pattern
test('a browser session page opens no worker, WebRTC, WebTransport, picture-in-picture or OS dialog', async ({}, testInfo) => {
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
        clicks: [{ x: 5, y: 5 }],
        expression: 'window.again()',
      });
      expect(clicked.value).toEqual(everyFrame);
    });

    await test.step('no click on the video controls enters picture-in-picture', async () => {
      const ready = await run(app, {
        session: 'pip',
        url: `${origin}/pip.html`,
        expression:
          'window.ready.then(() => ({ playing: !video.paused, enabled: document.pictureInPictureEnabled }))',
      });
      // A playing video in a document whose policy refuses picture-in-picture; the controls'
      // button shows only when the policy allows it (HTMLVideoElement::SupportsPictureInPicture).
      expect(ready.value).toEqual({ playing: true, enabled: false });
      // Every control along the bar, the picture-in-picture button's place included.
      const clicks = [24, 80, 160, 320, 480, 540, 570, 590, 610, 625].map((x) => ({ x, y: 340 }));
      const clicked = await run(app, {
        session: 'pip',
        url: null,
        clicks,
        expression: `new Promise((resolve) => setTimeout(resolve, 500)).then(async () => ({
          element: document.pictureInPictureElement,
          entered: window.entered,
          requested: await video.requestPictureInPicture().then(() => 'entered', (error) => error.name),
        }))`,
      });
      expect(clicked.value).toEqual({ element: null, entered: false, requested: 'SecurityError' });
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
      expect(state).toEqual({ count: windowsBefore + 4, visible: 0 });
    });
  } finally {
    server.close();
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});

// eslint-disable-next-line no-empty-pattern
test('no list box or picker of a browser session page reaches the display', async ({}, testInfo) => {
  const server: Server = createServer((request, response) => {
    const body = pages(`http://localhost:${(server.address() as AddressInfo).port}`)[
      request.url ?? ''
    ];
    response.writeHead(body === undefined ? 404 : 200, { 'Content-Type': 'text/html' });
    response.end(body ?? '');
  });
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const { harness, stop } = await launchElectronE2E();
  const { app } = harness;
  try {
    // The app stays active with its windows hidden, as when the user works in another of its
    // windows: a picker it opened would be on the display.
    const pid = await app.evaluate(({ BrowserWindow }) => {
      for (const win of BrowserWindow.getAllWindows()) win.hide();
      return process.pid;
    });
    await settle();
    const before = onScreenWindows(pid);

    await test.step('a hidden window without the guard does put a picker on the display', async () => {
      const opened = await app.evaluate(
        async ({ BrowserWindow }, optionsPath) => {
          const load = process.getBuiltinModule('node:module').createRequire(optionsPath);
          const { pageWindowOptions } = load(optionsPath);
          // A session's own window, painting while hidden, in a partition with no guard preload.
          const control = new BrowserWindow(pageWindowOptions('e2e-picker-control'));
          (globalThis as { e2ePickerControl?: Electron.BrowserWindow }).e2ePickerControl = control;
          await control.loadURL('data:text/html,<input id="color" type="color">');
          return control.webContents.executeJavaScript(
            `const color = document.getElementById('color');
            color.showPicker();
            new Promise((resolve) => setTimeout(() => resolve(color.matches(':open')), 500))`,
            true
          );
        },
        path.join(__dirname, '../../electron/dist/browser/pageWindowOptions.js')
      );
      // Blink's own view of the picker, apart from the display's.
      expect(opened).toBe(true);
      await settle();
      expect(onScreenWindows(pid)).toBeGreaterThan(before);
      await app.evaluate(() =>
        (globalThis as { e2ePickerControl?: Electron.BrowserWindow }).e2ePickerControl?.destroy()
      );
      await settle();
      expect(onScreenWindows(pid)).toBe(before);
    });

    const session = 'pickers';
    const loaded = await run(app, { session, url: `${origin}/pickers.html`, expression: null });
    expect(loaded.status).toBe('ok');
    const opensNothing = async (call: { clicks?: { x: number; y: number }[]; keys?: Key[] }) => {
      const settled = await run(app, { session, url: null, expression: null, ...call });
      expect(settled.status).toBe('ok');
      await settle();
      expect(onScreenWindows(pid)).toBe(before);
    };

    await test.step('a click on any field opens nothing', async () => {
      const { list, color, dateField, dateIndicator, suggest } = PICKER_POINTS;
      for (const point of [list, color, dateField, dateIndicator, suggest]) {
        await opensNothing({ clicks: [point] });
      }
    });

    await test.step('no key on a focused list box, date field or suggestion field opens it', async () => {
      const { shiftTab, space, enter, down, altDown, f4, a } = KEYS;
      // Shift+Tab from the color field reaches the list box, the first field.
      await run(app, {
        session,
        url: null,
        expression: "document.getElementById('color').focus()",
      });
      await opensNothing({ keys: [shiftTab] });
      const onList = await run(app, {
        session,
        url: null,
        expression: 'document.activeElement.id',
      });
      expect(onList.value).toBe('list');
      await opensNothing({ keys: [space, enter, down, altDown, f4] });
      const focused = await run(app, {
        session,
        url: null,
        expression: "document.getElementById('date').focus(); document.activeElement.id",
      });
      expect(focused.value).toBe('date');
      await opensNothing({ keys: [space, enter, altDown, f4] });
      await run(app, {
        session,
        url: null,
        expression: "document.getElementById('suggest').focus()",
      });
      await opensNothing({ keys: [a, down] });
    });

    await test.step('a page script given a click opens no picker', async () => {
      await opensNothing({ clicks: [PICKER_POINTS.open] });
      const tried = await run(app, { session, url: null, expression: 'window.tried' });
      expect(tried.value).toEqual(Array(4).fill('TypeError'));
    });

    await test.step('a click in a cross-site frame opens no picker', async () => {
      await opensNothing({ clicks: [PICKER_POINTS.frameColor] });
    });
  } finally {
    server.close();
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
