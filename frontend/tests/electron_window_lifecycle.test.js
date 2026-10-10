const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');

function loadWindowLifecycleWithBrowserWindow(BrowserWindow, appOverrides = {}) {
  const sourcePath = require.resolve('../electron/window_lifecycle.js');
  delete require.cache[sourcePath];
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') {
      return {
        app: { isPackaged: false, quit: () => {}, ...appOverrides },
        BrowserWindow,
        dialog: { showErrorBox: () => {} },
      };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require(sourcePath);
  } finally {
    Module._load = originalLoad;
    delete require.cache[sourcePath];
  }
}

function createInterruptedReadError() {
  const error = new Error('EINTR: interrupted system call, read');
  error.code = 'EINTR';
  return error;
}

function withMutedConsole(fn) {
  const originalWarn = console.warn;
  const originalError = console.error;
  console.warn = () => {};
  console.error = () => {};
  try {
    return fn();
  } finally {
    console.warn = originalWarn;
    console.error = originalError;
  }
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      const address = server.address();
      resolve(address.port);
    });
  });
}

test('createBrowserWindowWithRetry retries consecutive EINTR reads', () => {
  const calls = [];
  function BrowserWindow(options) {
    calls.push(options);
    if (calls.length <= 2) {
      throw createInterruptedReadError();
    }
    this.options = options;
  }

  const { createBrowserWindowWithRetry } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  const win = withMutedConsole(() => createBrowserWindowWithRetry({ width: 1000 }));

  assert.equal(calls.length, 3);
  assert.deepEqual(win.options, { width: 1000 });
});

test('createBrowserWindowWithRetry rethrows persistent EINTR after retry limit', () => {
  const calls = [];
  function BrowserWindow(options) {
    calls.push(options);
    throw createInterruptedReadError();
  }

  const { createBrowserWindowWithRetry } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);

  assert.throws(
    () => withMutedConsole(() => createBrowserWindowWithRetry({ width: 1000 })),
    /interrupted system call/
  );
  assert.equal(calls.length, 9);
});

test('createBrowserWindowWithRetry does not retry non-EINTR errors', () => {
  const calls = [];
  function BrowserWindow(options) {
    calls.push(options);
    throw new Error('window creation failed');
  }

  const { createBrowserWindowWithRetry } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);

  assert.throws(() => createBrowserWindowWithRetry({ width: 1000 }), /window creation failed/);
  assert.equal(calls.length, 1);
});

test('loadDevWindow does not load the renderer when dev server wait fails', async () => {
  function BrowserWindow() {}
  const { loadDevWindow } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  const calls = [];
  const win = {
    loadURL: (url) => {
      calls.push(url);
      return Promise.resolve();
    },
  };
  const originalInfo = console.info;
  console.info = () => {};

  try {
    await assert.rejects(
      loadDevWindow(win, {
        serverUrl: 'http://127.0.0.1:3001',
        finalUrl: 'http://127.0.0.1:3001/#/login',
        waitForServer: async () => {
          throw new Error('dev server timeout');
        },
      }),
      /dev server timeout/
    );
    assert.deepEqual(calls, []);
  } finally {
    console.info = originalInfo;
  }
});

test('requestHttpOk waits for the response body to finish', async () => {
  function BrowserWindow() {}
  const { requestHttpOk } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  let finishResponse;
  let markPartialBodyFlushed;
  const partialBodyFlushed = new Promise((resolve) => {
    markPartialBodyFlushed = resolve;
  });
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    finishResponse = () => res.end('</html>');
    res.write('<html>', markPartialBodyFlushed);
  });
  const port = await listen(server);

  try {
    const request = requestHttpOk(`http://127.0.0.1:${port}/`, 500).then(
      () => 'resolved',
      () => 'rejected'
    );
    // Wait for the handler to flush the headers and the partial body instead of
    // for a fixed delay the handler can miss under load.
    await partialBodyFlushed;
    assert.equal(await Promise.race([request, Promise.resolve('pending')]), 'pending');
    finishResponse();
    assert.equal(await request, 'resolved');
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('requestHttpOk rejects when the response body does not finish before timeout', async () => {
  function BrowserWindow() {}
  const { requestHttpOk } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.write('<html>');
  });
  const port = await listen(server);

  try {
    await assert.rejects(
      requestHttpOk(`http://127.0.0.1:${port}/`, 50),
      /Dev server request timeout/
    );
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('waitForDevServer includes the last HTTP failure in the timeout error', async () => {
  function BrowserWindow() {}
  const { waitForDevServer } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  const server = http.createServer((_req, res) => {
    res.writeHead(500, { 'content-type': 'text/plain' });
    res.end('not ready');
  });
  const port = await listen(server);

  try {
    await assert.rejects(
      waitForDevServer(`http://127.0.0.1:${port}/`, 20, 1),
      /Dev server timeout: Dev server returned HTTP 500/
    );
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('waitForWindowLoad rejects renderer load failures', async () => {
  function BrowserWindow() {}
  const { waitForWindowLoad } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  const webContents = new EventEmitter();
  webContents.removeListener = webContents.off.bind(webContents);
  const win = {
    webContents,
    loadURL: () => {
      queueMicrotask(() => {
        webContents.emit('did-fail-load', null, -102, 'Connection refused', 'http://test/');
      });
      return new Promise(() => {});
    },
  };

  await assert.rejects(
    waitForWindowLoad(win, 'http://test/', 1000),
    /Window load failed: http:\/\/test\/ \(-102: Connection refused\)/
  );
});

test('createMainWindow continues when centering is interrupted by EINTR', () => {
  const calls = [];
  const handlers = new Map();
  function BrowserWindow(options) {
    this.options = options;
    this.center = () => {
      calls.push('center');
      throw createInterruptedReadError();
    };
    this.on = (event, handler) => {
      handlers.set(event, handler);
    };
    this.webContents = {
      session: {
        webRequest: {
          onHeadersReceived: () => {
            calls.push('headers');
          },
        },
      },
      on: () => {},
    };
  }

  const { createMainWindow } = loadWindowLifecycleWithBrowserWindow(BrowserWindow, {
    isPackaged: true,
    getAppPath: () => '/tmp/pantaray-test-app',
  });
  const win = withMutedConsole(() => createMainWindow({ initialUiLanguage: 'ja' }));

  assert.equal(win.options.width, 1280);
  // The first click after an overlay took keyboard focus acts instead of only refocusing.
  assert.equal(win.options.acceptFirstMouse, true);
  assert.deepEqual(calls, [
    'center',
    'center',
    'center',
    'center',
    'center',
    'center',
    'center',
    'center',
    'center',
    'headers',
  ]);
  assert.equal(handlers.has('close'), true);
});

test('setupWindowEventListeners retries CSP listener registration after EINTR', () => {
  const calls = [];
  let webContentsReads = 0;
  const win = {
    on: (event) => calls.push(`on:${event}`),
    get webContents() {
      webContentsReads += 1;
      if (webContentsReads <= 2) {
        throw createInterruptedReadError();
      }
      return {
        session: {
          webRequest: {
            onHeadersReceived: () => calls.push('headers'),
          },
        },
      };
    },
  };

  const { setupWindowEventListeners } = loadWindowLifecycleWithBrowserWindow(
    function BrowserWindow() {}
  );

  withMutedConsole(() => setupWindowEventListeners(win));

  assert.equal(webContentsReads, 3);
  assert.deepEqual(calls, ['on:close', 'headers']);
});

test('createMainWindow retries webContents listener registration after EINTR', () => {
  const calls = [];
  let didFailAttempts = 0;
  function BrowserWindow(options) {
    this.options = options;
    this.center = () => calls.push('center');
    this.on = (event) => calls.push(`window:${event}`);
    this.webContents = {
      session: {
        webRequest: {
          onHeadersReceived: () => calls.push('headers'),
        },
      },
      on: (event) => {
        calls.push(`web:${event}`);
        if (event === 'did-fail-load') {
          didFailAttempts += 1;
          if (didFailAttempts <= 2) {
            throw createInterruptedReadError();
          }
        }
      },
    };
  }

  const { createMainWindow } = loadWindowLifecycleWithBrowserWindow(BrowserWindow, {
    isPackaged: true,
    getAppPath: () => '/tmp/pantaray-test-app',
  });
  const win = withMutedConsole(() => createMainWindow({ initialUiLanguage: 'ja' }));

  assert.equal(win.options.width, 1280);
  assert.equal(didFailAttempts, 3);
  assert.deepEqual(calls, [
    'center',
    // The navigation guard is in place before anything loads.
    'web:will-navigate',
    'web:did-fail-load',
    'web:did-fail-load',
    'web:did-fail-load',
    'web:did-finish-load',
    'web:devtools-opened',
    'web:before-input-event',
    'window:close',
    'headers',
  ]);
});

function createEntryWindow() {
  const win = new EventEmitter();
  win.center = () => {};
  win.webContents = new EventEmitter();
  win.webContents.session = { webRequest: { onHeadersReceived: () => {} } };
  return win;
}

test('development main window opens the app root', async (t) => {
  const server = http.createServer((_req, res) => res.end('ready'));
  const port = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const originalPort = process.env.FRONTEND_PORT;
  process.env.FRONTEND_PORT = String(port);
  t.after(() => {
    if (originalPort === undefined) delete process.env.FRONTEND_PORT;
    else process.env.FRONTEND_PORT = originalPort;
  });
  let markLoaded;
  const loaded = new Promise((resolve) => {
    markLoaded = resolve;
  });
  function BrowserWindow() {
    const win = createEntryWindow();
    win.loadURL = (url) => {
      markLoaded(url);
      return Promise.resolve();
    };
    return win;
  }
  const { createMainWindow } = loadWindowLifecycleWithBrowserWindow(BrowserWindow);
  createMainWindow({ initialUiLanguage: 'ja' });
  assert.equal(await loaded, `http://localhost:${port}#/`);
});

test('packaged main window opens the app root on initial load and recovery', (t) => {
  const appPath = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-entry-'));
  t.after(() => fs.rmSync(appPath, { recursive: true, force: true }));
  fs.mkdirSync(path.join(appPath, 'dist'));
  const indexPath = path.join(appPath, 'dist', 'index.html');
  fs.writeFileSync(indexPath, '<!doctype html>');
  const originalEnv = process.env.NODE_ENV;
  process.env.NODE_ENV = 'production';
  t.after(() => {
    if (originalEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = originalEnv;
  });
  const loads = [];
  function BrowserWindow() {
    const win = createEntryWindow();
    win.loadFile = (file, options) => loads.push({ file, options });
    return win;
  }
  const { createMainWindow } = loadWindowLifecycleWithBrowserWindow(BrowserWindow, {
    isPackaged: true,
    getAppPath: () => appPath,
  });
  const win = createMainWindow({ initialUiLanguage: 'ja' });
  withMutedConsole(() => win.webContents.emit('did-fail-load', null, -2, 'Failed', indexPath));
  assert.deepEqual(loads, [
    { file: indexPath, options: { hash: '/' } },
    { file: indexPath, options: { hash: '/' } },
  ]);
});

test('packaged main window created on a task retries its first load there, and later loads at the root', (t) => {
  const appPath = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-entry-'));
  t.after(() => fs.rmSync(appPath, { recursive: true, force: true }));
  fs.mkdirSync(path.join(appPath, 'dist'));
  const indexPath = path.join(appPath, 'dist', 'index.html');
  fs.writeFileSync(indexPath, '<!doctype html>');
  const originalEnv = process.env.NODE_ENV;
  process.env.NODE_ENV = 'production';
  t.after(() => {
    if (originalEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = originalEnv;
  });
  const loads = [];
  function BrowserWindow() {
    const win = createEntryWindow();
    win.loadFile = (file, options) => loads.push(options.hash);
    return win;
  }
  const { createMainWindow } = loadWindowLifecycleWithBrowserWindow(BrowserWindow, {
    isPackaged: true,
    getAppPath: () => appPath,
  });
  const task = '/history?item=action:act-1';
  const failFirst = createMainWindow({ initialUiLanguage: 'ja', hashRoute: task });
  withMutedConsole(() =>
    failFirst.webContents.emit('did-fail-load', null, -2, 'Failed', indexPath)
  );
  const failLater = createMainWindow({ initialUiLanguage: 'ja', hashRoute: task });
  failLater.webContents.emit('did-finish-load');
  withMutedConsole(() =>
    failLater.webContents.emit('did-fail-load', null, -2, 'Failed', indexPath)
  );

  assert.deepEqual(loads, [task, task, task, '/']);
});
