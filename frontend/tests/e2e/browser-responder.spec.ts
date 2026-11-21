import { test, expect } from '@playwright/test';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { launchElectronE2E } from './harness';

const DIST = path.join(__dirname, '../../electron/dist');
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const PAGES: Record<string, string> = {
  '/first.html': '<!doctype html><title>First</title><p>first</p>',
  // Taller than a full-page capture keeps.
  '/tall.html':
    '<!doctype html><title>Tall</title><body style="margin:0"><div style="height:20000px;background:linear-gradient(red,blue)"></div></body>',
};

type Answer = {
  operation_request_id: string;
  result: {
    status: 'ok' | 'failed';
    code?: string;
    page_data?: { url: string; title: string };
    truncated?: string | null;
    artifact?: {
      kind: 'png' | 'pdf';
      byte_size: number;
      sha256: string;
      width_px?: number;
      height_px?: number;
    } | null;
  };
};

// eslint-disable-next-line no-empty-pattern
test('the browser responder answers each operation with the artifact it claims', async ({}, testInfo) => {
  const server: Server = createServer((request, response) => {
    const body = PAGES[request.url ?? ''];
    response.writeHead(body === undefined ? 404 : 200, { 'Content-Type': 'text/html' });
    response.end(body ?? '');
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
    // Runs the real responder in the app's main process; the answer is kept instead of posted,
    // and `gone` makes the post fail as the runtime's 404 does once it stopped waiting.
    const answer = (id: string, operation: object, gone = false) =>
      app.evaluate(
        async (_electron, args) => {
          type Responder = (request: object) => Promise<void>;
          const g = globalThis as {
            e2eBrowser?: { respond: (gone: boolean) => Responder; answers: unknown[] };
          };
          if (g.e2eBrowser === undefined) {
            const load = process.getBuiltinModule('node:module').createRequire(args.dist);
            const { createBrowserSessions } = load(`${args.dist}/browser/browserSession.js`);
            const { createBrowserResponder } = load(`${args.dist}/browser/browserResponder.js`);
            const { createUserBrowserSessions } = load(
              `${args.dist}/browser/userBrowserSession.js`
            );
            const { LocalBackendRequestError } = load(`${args.dist}/localBackend/client.js`);
            // No page here reaches this port.
            // No window here watches the page.
            const sessions = createBrowserSessions({
              getBackendPort: () => 9,
              pages: { opened() {}, touched() {}, closed() {} },
            });
            // Every request here is for the app's page; none connects to a browser.
            const userSessions = createUserBrowserSessions({
              connect: () => Promise.reject(new Error('not used')),
              getBackendPort: () => 9,
            });
            const answers: unknown[] = [];
            g.e2eBrowser = {
              answers,
              respond: (isGone) =>
                createBrowserResponder({
                  sessions,
                  userSessions,
                  localArtifactRoot: args.artifactRoot,
                  postAnswer: async (body: unknown) => {
                    answers.push(body);
                    if (isGone) throw new LocalBackendRequestError('gone', 404);
                    return {};
                  },
                }),
            };
          }
          const browser = g.e2eBrowser;
          await browser.respond(args.gone)({
            operation_request_id: args.id,
            session_id: 'responder-e2e',
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
          return browser.answers[browser.answers.length - 1];
        },
        { dist: DIST, artifactRoot, id, operation, gone }
      ) as Promise<Answer>;
    const navigate = (url: string) => ({ action: 'navigate', target: { kind: 'web', url } });
    const fileOf = (id: string, kind: string) =>
      path.join(artifactRoot, '.tmp', 'browser', `${id}.${kind}`);
    /** The file the claim names, checked against the claim's size and digest. */
    const claimed = (reply: Answer) => {
      const artifact = reply.result.artifact;
      expect(artifact).toBeTruthy();
      const bytes = fs.readFileSync(fileOf(reply.operation_request_id, artifact!.kind));
      expect(bytes.length).toBe(artifact!.byte_size);
      expect(createHash('sha256').update(bytes).digest('hex')).toBe(artifact!.sha256);
      return bytes;
    };

    await test.step('navigate and back answer with the page', async () => {
      const first = await answer('nav-first', navigate(`${origin}/first.html`));
      expect(first.result).toMatchObject({ status: 'ok', page_data: { title: 'First' } });
      await answer('nav-tall', navigate(`${origin}/tall.html`));
      const back = await answer('back', { action: 'back' });
      expect(back.result.page_data?.url).toBe(`${origin}/first.html`);
      await answer('nav-tall-again', navigate(`${origin}/tall.html`));
    });

    await test.step('a viewport screenshot is a 1280x800 PNG as claimed', async () => {
      const shot = await answer('shot-view', { action: 'screenshot', ref: null, full_page: null });
      const bytes = claimed(shot);
      expect(bytes.subarray(0, 8).equals(PNG_SIGNATURE)).toBe(true);
      expect([bytes.readUInt32BE(16), bytes.readUInt32BE(20)]).toEqual([1280, 800]);
      expect(shot.result.artifact).toMatchObject({ kind: 'png', width_px: 1280, height_px: 800 });
    });

    await test.step('a full page is cut at its cap and says so', async () => {
      const shot = await answer('shot-full', { action: 'screenshot', ref: null, full_page: true });
      const bytes = claimed(shot);
      expect([bytes.readUInt32BE(16), bytes.readUInt32BE(20)]).toEqual([1280, 8000]);
      expect(shot.result.truncated).toBe('page_too_tall');
    });

    await test.step('print writes the PDF it claims', async () => {
      const printed = await answer('print', { action: 'print' });
      expect(claimed(printed).subarray(0, 5).toString()).toBe('%PDF-');
    });

    await test.step('an artifact the runtime stopped waiting for is removed', async () => {
      const late = await answer(
        'shot-late',
        { action: 'screenshot', ref: null, full_page: null },
        true
      );
      expect(late.result.artifact?.kind).toBe('png');
      expect(fs.existsSync(fileOf('shot-late', 'png'))).toBe(false);
    });
  } finally {
    server.close();
    fs.rmSync(artifactRoot, { recursive: true, force: true });
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
