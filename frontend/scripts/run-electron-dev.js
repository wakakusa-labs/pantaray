#!/usr/bin/env node

const { spawn, spawnSync } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const SHUTDOWN_SIGNAL = 'SIGTERM';
const FORCE_KILL_DELAY_MS = 3_000;
const VITE_READY_TIMEOUT_MS = 30_000;
const VITE_READY_POLL_INTERVAL_MS = 300;
const VITE_READY_REQUEST_TIMEOUT_MS = 1_500;
// The downloaded Electron is only ad-hoc signed, so macOS ties its Screen Recording and
// Accessibility grants to that exact binary and drops them whenever the Electron version
// or the checkout changes. Signing with a real identity ties them to the identity instead.
const DEV_SIGN_IDENTITY_ENV = 'PANTARAY_DEV_SIGN_IDENTITY';
async function resolveFrontendPort() {
  const { resolveConfig } = await import('vite');
  const config = await resolveConfig({}, 'serve', 'development');
  return config.server.port;
}

function describePortOwner(port) {
  const result = spawnSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN'], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  if (result.status !== 0) {
    return '';
  }
  return result.stdout.trim();
}

function assertPortAvailable(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', (error) => {
      if (error && error.code === 'EADDRINUSE') {
        const owner = describePortOwner(port);
        reject(
          new Error(
            [
              `Port ${port} is already in use.`,
              owner ? `Current listener:\n${owner}` : null,
              'Stop the listener or set a different FRONTEND_PORT in .env.local.',
            ]
              .filter(Boolean)
              .join('\n')
          )
        );
        return;
      }
      reject(error);
    });
    server.listen(port, () => {
      server.close(() => resolve());
    });
  });
}

function resignElectronBundle({ identity, executable, run = spawnSync }) {
  // <bundle>.app/Contents/MacOS/Electron
  const bundle = path.resolve(executable, '..', '..', '..');
  const result = run('codesign', ['--force', '--deep', '--sign', identity, bundle], {
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    throw new Error(
      `${DEV_SIGN_IDENTITY_ENV}: codesign could not sign ${bundle}.\n${result.stderr || result.error}`
    );
  }
}

function spawnChild(name, command, args) {
  const child = spawn(command, args, {
    cwd: process.cwd(),
    env: { ...process.env, NODE_DISABLE_COMPILE_CACHE: '1' },
    stdio: 'inherit',
  });
  child.once('error', (error) => {
    console.error(`[${name}] failed to start:`, error);
  });
  return child;
}

const children = [];

let shuttingDown = false;

function stopChildren(except) {
  for (const child of children) {
    if (child === except || child.exitCode !== null || child.signalCode !== null) {
      continue;
    }
    child.kill(SHUTDOWN_SIGNAL);
    setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill('SIGKILL');
      }
    }, FORCE_KILL_DELAY_MS).unref();
  }
}

function buildFrontendUrl(port) {
  return `http://127.0.0.1:${port}/`;
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function requestHttpOk(url, timeoutMs = VITE_READY_REQUEST_TIMEOUT_MS) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (ready) => {
      if (settled) return;
      settled = true;
      resolve(ready);
    };
    const req = http.request(url, { method: 'GET', timeout: timeoutMs }, (res) => {
      const ok = Boolean(res.statusCode && res.statusCode >= 200 && res.statusCode < 500);
      res.resume();
      res.once('end', () => {
        settle(ok);
      });
      res.once('error', () => {
        settle(false);
      });
    });
    req.once('timeout', () => {
      req.destroy();
      settle(false);
    });
    req.once('error', () => {
      settle(false);
    });
    req.end();
  });
}

async function waitForViteReady({
  child,
  url,
  timeoutMs = VITE_READY_TIMEOUT_MS,
  intervalMs = VITE_READY_POLL_INTERVAL_MS,
  requestReady = requestHttpOk,
}) {
  const deadline = Date.now() + timeoutMs;
  let childExit = null;
  child.once('exit', (code, signal) => {
    childExit = { code, signal };
  });

  while (Date.now() < deadline) {
    if (childExit) {
      const { code, signal } = childExit;
      throw new Error(`Vite exited before becoming ready (code=${code}, signal=${signal}).`);
    }
    if (await requestReady(url)) {
      return;
    }
    if (childExit) {
      const { code, signal } = childExit;
      throw new Error(`Vite exited before becoming ready (code=${code}, signal=${signal}).`);
    }
    await sleep(intervalMs);
  }
  throw new Error(`Timed out waiting for Vite dev server: ${url}`);
}

function monitorChildExit(child) {
  child.once('exit', (code, signal) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    stopChildren(child);
    process.exitCode = typeof code === 'number' ? code : signal ? 1 : 0;
  });
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    shuttingDown = true;
    stopChildren(null);
  });
}

async function main() {
  const frontendPort = await resolveFrontendPort();
  process.env.FRONTEND_PORT = String(frontendPort);
  await assertPortAvailable(frontendPort);
  const viteUrl = buildFrontendUrl(frontendPort);
  const vite = spawnChild('vite', process.execPath, ['./node_modules/vite/bin/vite.js']);
  children.push(vite);
  monitorChildExit(vite);
  try {
    await waitForViteReady({ child: vite, url: viteUrl });
  } catch (error) {
    shuttingDown = true;
    stopChildren(null);
    throw error;
  }

  const signIdentity = process.env[DEV_SIGN_IDENTITY_ENV];
  if (signIdentity && process.platform === 'darwin') {
    try {
      resignElectronBundle({ identity: signIdentity, executable: require('electron') });
    } catch (error) {
      shuttingDown = true;
      stopChildren(null);
      throw error;
    }
  }

  const electron = spawnChild('electron', process.execPath, [
    './node_modules/electron/cli.js',
    '.',
  ]);
  children.push(electron);
  monitorChildExit(electron);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}

module.exports = {
  assertPortAvailable,
  buildFrontendUrl,
  requestHttpOk,
  resignElectronBundle,
  resolveFrontendPort,
  waitForViteReady,
};
