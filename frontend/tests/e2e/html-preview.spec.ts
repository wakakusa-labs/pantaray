import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { launchElectronE2E } from './harness';

const DIST = path.join(__dirname, '../../electron/dist');

// eslint-disable-next-line no-empty-pattern
test('the task preview prints a local HTML file to a PDF and the page reaches no network', async ({}, testInfo) => {
  let networkRequests = 0;
  const server: Server = createServer((_request, response) => {
    networkRequests += 1;
    response.writeHead(200, { 'Content-Type': 'image/png' });
    response.end();
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-preview-e2e-')));
  const deck = path.join(work, 'deck.html');
  // The image holds `load` until its request settles, so a request let through would reach the
  // server before the print.
  fs.writeFileSync(
    deck,
    `<!doctype html><title>Deck</title><body><p id="chart"></p><img src="${origin}/pixel.png">
<script>document.getElementById('chart').textContent = 'drawn'; console.log('script ran');</script>
</body>`
  );
  const { harness, stop } = await launchElectronE2E();
  const { app } = harness;
  try {
    await app.evaluate(({ app: electronApp, BrowserWindow }) => {
      for (const win of BrowserWindow.getAllWindows()) win.hide();
      electronApp.hide();
    });
    const outcome = await app.evaluate(
      async ({ app: electronApp, BrowserWindow }, args) => {
        const load = process.getBuiltinModule('node:module').createRequire(args.dist);
        const { createBrowserSessions } = load(`${args.dist}/browser/browserSession.js`);
        const { createHtmlPreviewPdfs } = load(`${args.dist}/render/htmlPreview.js`);
        const consoleLines: string[] = [];
        electronApp.on('web-contents-created', (_event, contents) =>
          contents.on('console-message', (details) => consoleLines.push(details.message))
        );
        const windowsBefore = BrowserWindow.getAllWindows().length;
        // No page here reaches this port.
        // No window here watches the page.
        const sessions = createBrowserSessions({
          getBackendPort: () => 9,
          pages: { opened() {}, touched() {}, closed() {} },
        });
        const pdfs = createHtmlPreviewPdfs({
          userDataDir: electronApp.getPath('userData'),
          sessions,
        });
        const rendered = await pdfs.render(args.deck);
        const pdf: Buffer | null =
          rendered.kind === 'rendered' ? pdfs.readRendered(rendered.renderId, args.deck) : null;
        return {
          rendered,
          pdfHead: pdf === null ? null : pdf.subarray(0, 5).toString('latin1'),
          consoleLines,
          windowsLeft: BrowserWindow.getAllWindows().length - windowsBefore,
        };
      },
      { dist: DIST, deck }
    );
    expect(outcome.rendered.kind).toBe('rendered');
    expect(outcome.pdfHead).toBe('%PDF-');
    expect(outcome.consoleLines).toContain('script ran');
    expect(networkRequests).toBe(0);
    // The preview's session is closed once it printed.
    expect(outcome.windowsLeft).toBe(0);
  } finally {
    server.close();
    fs.rmSync(work, { recursive: true, force: true });
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
