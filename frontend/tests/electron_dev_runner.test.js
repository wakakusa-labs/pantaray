const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const { execFile } = require('node:child_process');
const { test } = require('node:test');

const {
  assertPortAvailable,
  buildFrontendUrl,
  requestHttpOk,
  resignElectronBundle,
  resolveFrontendPort,
  waitForViteReady,
} = require('../scripts/run-electron-dev');

function createChild() {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.kill = () => {};
  return child;
}

test('resignElectronBundle signs the whole .app that holds the Electron executable', () => {
  const calls = [];
  resignElectronBundle({
    identity: 'Developer ID Application: Example (TEAM)',
    executable: '/x/dist/Electron.app/Contents/MacOS/Electron',
    run: (command, args) => {
      calls.push([command, args]);
      return { status: 0 };
    },
  });
  assert.deepEqual(calls, [
    [
      'codesign',
      [
        '--force',
        '--deep',
        '--sign',
        'Developer ID Application: Example (TEAM)',
        '/x/dist/Electron.app',
      ],
    ],
  ]);
});

test('resignElectronBundle fails loudly when codesign fails', () => {
  assert.throws(
    () =>
      resignElectronBundle({
        identity: 'missing',
        executable: '/x/dist/Electron.app/Contents/MacOS/Electron',
        run: () => ({ status: 1, stderr: 'no identity found' }),
      }),
    /no identity found/
  );
});

test('run-electron-dev builds the loopback frontend URL', () => {
  assert.equal(buildFrontendUrl(3001), 'http://127.0.0.1:3001/');
});

test('development config shares the default and env-file port with the runner', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'frontend-port-'));
  const cwd = process.cwd();
  const previousPort = process.env.FRONTEND_PORT;
  const previousNodeEnv = process.env.NODE_ENV;
  t.after(() => {
    process.chdir(cwd);
    if (previousPort === undefined) delete process.env.FRONTEND_PORT;
    else process.env.FRONTEND_PORT = previousPort;
    if (previousNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previousNodeEnv;
    fs.rmSync(root, { recursive: true, force: true });
  });
  fs.copyFileSync(path.join(__dirname, '../vite.config.ts'), path.join(root, 'vite.config.ts'));
  fs.symlinkSync(path.join(__dirname, '../node_modules'), path.join(root, 'node_modules'));
  process.chdir(root);
  delete process.env.FRONTEND_PORT;
  assert.equal(await resolveFrontendPort(), 3001);

  fs.writeFileSync(path.join(root, '.env.local'), 'FRONTEND_PORT=43123\n');
  assert.equal(await resolveFrontendPort(), 43123);
  const { resolveConfig } = await import('vite');
  assert.equal((await resolveConfig({}, 'serve', 'development')).server.port, 43123);

  process.env.FRONTEND_PORT = '43124';
  assert.equal(await resolveFrontendPort(), 43124);
  delete process.env.FRONTEND_PORT;

  for (const value of ['', 'not-a-port', '0', '65536', '3001.5']) {
    fs.writeFileSync(path.join(root, '.env.local'), `FRONTEND_PORT=${value}\n`);
    await assert.rejects(resolveFrontendPort(), /FRONTEND_PORT must be a valid TCP port/);
  }
  // Build must ignore even an invalid development-only setting.
  assert.equal((await resolveConfig({}, 'build', 'production')).command, 'build');
});

test('an occupied port is rejected without stopping its existing listener', async () => {
  const server = http.createServer((_req, res) => res.end('existing service'));
  await new Promise((resolve) => server.listen(0, resolve));
  try {
    const { port } = server.address();
    await assert.rejects(assertPortAvailable(port), /already in use/);
    assert.equal(await requestHttpOk(buildFrontendUrl(port)), true);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('the runner starts Vite and passes the selected port to Electron', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'frontend-runner-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.copyFileSync(path.join(__dirname, '../vite.config.ts'), path.join(root, 'vite.config.ts'));
  fs.mkdirSync(path.join(root, 'node_modules/electron'), { recursive: true });
  for (const dependency of ['vite', '@vitejs']) {
    fs.symlinkSync(
      path.join(__dirname, '../node_modules', dependency),
      path.join(root, 'node_modules', dependency)
    );
  }
  const probe = http.createServer();
  await new Promise((resolve) => probe.listen(0, resolve));
  const port = probe.address().port;
  await new Promise((resolve) => probe.close(resolve));
  fs.writeFileSync(path.join(root, '.env.local'), `FRONTEND_PORT=${port}\n`);
  fs.writeFileSync(path.join(root, 'index.html'), '<html>port wiring verified</html>');
  // Replace only the GUI executable; use the real runner, Vite config and HTTP server.
  fs.writeFileSync(
    path.join(root, 'node_modules/electron/cli.js'),
    `
    fetch('http://127.0.0.1:' + process.env.FRONTEND_PORT).then(async response => {
      if (!(await response.text()).includes('port wiring verified')) process.exit(1);
      console.log('ELECTRON_FRONTEND_PORT=' + process.env.FRONTEND_PORT);
    }).catch(() => process.exit(1));
  `
  );
  const env = { ...process.env, NODE_ENV: 'development' };
  delete env.FRONTEND_PORT;
  // The runner resolves `electron` from its own checkout, not from `root`, so a signing
  // identity in the developer's shell would re-sign the real Electron.app.
  delete env.PANTARAY_DEV_SIGN_IDENTITY;
  const { stdout } = await promisify(execFile)(
    process.execPath,
    [path.join(__dirname, '../scripts/run-electron-dev.js')],
    { cwd: root, env, timeout: 15000 }
  );
  assert.match(stdout, new RegExp(`ELECTRON_FRONTEND_PORT=${port}\\b`));
});

// The readiness deadline is scaffolding for these two tests, not the behavior
// under test. A budget tight enough to expire between polls under load turns
// them into stopwatch races against the "Timed out" branch.
const READY_DEADLINE_MS = 30_000;

test('waitForViteReady waits until the HTTP readiness check succeeds', async () => {
  const child = createChild();
  let calls = 0;

  await waitForViteReady({
    child,
    url: 'http://127.0.0.1:3001/',
    timeoutMs: READY_DEADLINE_MS,
    intervalMs: 1,
    requestReady: async () => {
      calls += 1;
      return calls === 2;
    },
  });

  assert.equal(calls, 2);
});

test('waitForViteReady fails when Vite exits before becoming ready', async () => {
  const child = createChild();

  await assert.rejects(
    waitForViteReady({
      child,
      url: 'http://127.0.0.1:3001/',
      timeoutMs: READY_DEADLINE_MS,
      intervalMs: 1,
      requestReady: async () => {
        // Vite dies while the readiness probe is in flight, so the rejection
        // does not depend on the exit landing inside a polling window.
        child.emit('exit', 1, null);
        return false;
      },
    }),
    /Vite exited before becoming ready/
  );
});

test('requestHttpOk waits for the GET response body to finish', async () => {
  let finishResponse;
  let resolveRequestReceived;
  const requestReceived = new Promise((resolve) => {
    resolveRequestReceived = resolve;
  });
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.write('<html>');
    finishResponse = () => {
      res.end('</html>');
    };
    resolveRequestReceived();
  });

  await new Promise((resolve) => {
    server.listen(0, '127.0.0.1', resolve);
  });

  try {
    const { port } = server.address();
    let resolved = false;
    const readyPromise = requestHttpOk(`http://127.0.0.1:${port}/`, 500).then((ready) => {
      resolved = true;
      return ready;
    });

    await requestReceived;
    assert.equal(resolved, false);

    finishResponse();
    assert.equal(await readyPromise, true);
  } finally {
    await new Promise((resolve) => {
      server.close(resolve);
    });
  }
});
