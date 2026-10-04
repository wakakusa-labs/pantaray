const { app, BrowserWindow, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { buildFrontendDevHashUrl, buildFrontendDevOrigin } = require('./dev_frontend_env');
const { loadRuntimeConfig } = require('./runtime_config');
const { buildUiLanguageAdditionalArguments } = require('./ui_language_bootstrap');
const { buildContentSecurityPolicy } = require('./content_security_policy');
const { PANTARAY_ACCOUNT_LOGIN_ENABLED } = require('./dist/auth/accountLoginFeature');

const BROWSER_WINDOW_EINTR_RETRY_LIMIT = 8;
const WINDOW_STARTUP_STEP_EINTR_RETRY_LIMIT = 8;
const DEV_SERVER_WAIT_TIMEOUT_MS = 20000;
const DEV_SERVER_REQUEST_TIMEOUT_MS = 1500;
const DEV_WINDOW_LOAD_TIMEOUT_MS = 15000;

function isDevRuntime() {
  // NOTE:
  // - `pnpm run electron:dev` は NODE_ENV を明示していないケースがあるため、
  //   "未パッケージ" を dev とみなす（dist の状態に引きずられて白画面にならないようにする）。
  return process.env.NODE_ENV === 'development' || !app.isPackaged;
}

/**
 * 配布版の index.html パスを解決する。
 */
function resolvePackagedIndexPath() {
  return path.join(app.getAppPath(), 'dist', 'index.html');
}

function getStartupCopy() {
  const lang = String(app.getLocale ? app.getLocale() : '')
    .toLowerCase()
    .startsWith('ja')
    ? 'ja'
    : 'en';
  return lang === 'ja'
    ? {
        title: '起動エラー',
        body: 'UIの読み込みに失敗しました。ビルド成果物（dist）を確認してください。',
      }
    : {
        title: 'Startup error',
        body: 'Failed to load the UI. Check the build output (dist).',
      };
}

/**
 * 配布版の index.html を読み込む（hashRoute は "/login" のように先頭スラッシュ付き）。
 */
function loadPackagedIndex(win, hashRoute) {
  const indexPath = resolvePackagedIndexPath();
  if (!fs.existsSync(indexPath)) {
    console.error('Packaged index not found:', indexPath);
    try {
      const text = getStartupCopy();
      dialog.showErrorBox(text.title, text.body);
    } catch {
      // no-op
    }
    return { ok: false };
  }
  try {
    win.loadFile(indexPath, { hash: hashRoute });
    return { ok: true, path: indexPath };
  } catch (error) {
    console.error('Failed to load packaged index:', error);
    try {
      const text = getStartupCopy();
      dialog.showErrorBox(text.title, text.body);
    } catch {
      // no-op
    }
    return { ok: false };
  }
}

function setupWindowEventListeners(win) {
  if (!win) return;

  runWindowStartupStepWithRetry(
    'close-listener',
    () => {
      win.on('close', (event) => {
        if (process.platform === 'darwin' && !app.isQuitting) {
          event.preventDefault();
          try {
            win.hide();
          } catch (error) {
            console.error('Failed to hide main window on macOS close event:', error);
          }
          return;
        }

        app.quit();
      });
    },
    { optional: true }
  );

  runWindowStartupStepWithRetry(
    'csp-listener',
    () => {
      win.webContents.session.webRequest.onHeadersReceived((details, callback) => {
        const isDev = isDevRuntime();
        let apiOrigin = null;
        if (!isDev) {
          // prod は許可先を固定（VITE_API_HOST相当 + アカウントログインが有効なら Supabase）
          const cfg = loadRuntimeConfig({
            isPackaged: app.isPackaged,
            accountLoginEnabled: PANTARAY_ACCOUNT_LOGIN_ENABLED,
          });
          apiOrigin = cfg && cfg.api_host_origin ? String(cfg.api_host_origin) : null;
        }
        const csp = buildContentSecurityPolicy({
          isDev,
          apiOrigin,
          accountLoginEnabled: PANTARAY_ACCOUNT_LOGIN_ENABLED,
        });
        callback({
          responseHeaders: {
            ...details.responseHeaders,
            'Content-Security-Policy': [csp],
          },
        });
      });
    },
    { optional: isDevRuntime() }
  );
}

function requestHttpOk(url, timeoutMs = DEV_SERVER_REQUEST_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const req = http.get(url, (res) => {
      res.resume();
      res.on('end', () => {
        if (settled) return;
        settled = true;
        const statusCode = Number(res.statusCode || 0);
        if (statusCode >= 200 && statusCode < 400) {
          resolve(true);
          return;
        }
        reject(new Error(`Dev server returned HTTP ${statusCode} for ${url}`));
      });
      res.on('error', (error) => {
        if (settled) return;
        settled = true;
        reject(error);
      });
    });
    req.on('error', (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    });
    req.setTimeout(timeoutMs, () => {
      if (settled) return;
      settled = true;
      req.destroy();
      reject(new Error(`Dev server request timeout: ${url}`));
    });
  });
}

function waitForDevServer(url, timeoutMs = DEV_SERVER_WAIT_TIMEOUT_MS, intervalMs = 300) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const tryOnce = () => {
      requestHttpOk(url)
        .then(() => {
          resolve(true);
        })
        .catch((error) => {
          const lastError = error instanceof Error ? error : new Error(String(error));
          if (Date.now() > deadline) {
            return reject(new Error(`Dev server timeout: ${lastError.message}`));
          }
          setTimeout(tryOnce, intervalMs);
        });
    };
    tryOnce();
  });
}

function waitForWindowLoad(win, url, timeoutMs = DEV_WINDOW_LOAD_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      rejectOnce(new Error(`Window load timeout: ${url}`));
    }, timeoutMs);

    const cleanup = () => {
      clearTimeout(timer);
      try {
        win.webContents.removeListener('did-finish-load', onFinish);
      } catch {}
      try {
        win.webContents.removeListener('did-fail-load', onFail);
      } catch {}
    };

    const rejectOnce = (error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };

    const resolveOnce = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(true);
    };

    const onFinish = () => {
      resolveOnce();
    };

    const onFail = (_event, errorCode, errorDescription, validatedURL) => {
      rejectOnce(
        new Error(
          `Window load failed: ${validatedURL || url} (${errorCode}: ${errorDescription})`
        )
      );
    };

    try {
      win.webContents.once('did-finish-load', onFinish);
      win.webContents.once('did-fail-load', onFail);
      Promise.resolve(win.loadURL(url)).then(resolveOnce).catch(rejectOnce);
    } catch (error) {
      rejectOnce(error);
    }
  });
}

async function loadDevWindow(
  win,
  { serverUrl, finalUrl, waitForServer = waitForDevServer, waitForLoad = waitForWindowLoad }
) {
  console.info('MAIN_WINDOW_DEV_SERVER_WAIT_START', { serverUrl });
  await waitForServer(serverUrl);
  console.info('MAIN_WINDOW_DEV_SERVER_READY', { serverUrl });
  console.info('MAIN_WINDOW_DEV_LOAD_START', { finalUrl });
  await waitForLoad(win, finalUrl);
  console.info('MAIN_WINDOW_DEV_LOAD_OK', { finalUrl });
}

function reportDevWindowLoadFailure(error, finalUrl) {
  const message = error instanceof Error ? error.message : String(error);
  console.error('MAIN_WINDOW_DEV_LOAD_FAILED', {
    finalUrl,
    error: message,
  });
  try {
    dialog.showErrorBox(
      '起動エラー',
      `開発用フロントエンドを読み込めませんでした。\n\n${message}`
    );
  } catch {}
}

function isInterruptedReadError(error) {
  return (
    error &&
    typeof error === 'object' &&
    (error.code === 'EINTR' ||
      String(error.message || '')
        .toLowerCase()
        .includes('interrupted system call, read'))
  );
}

function createBrowserWindowWithRetry(options) {
  let lastInterruptedError = null;
  for (let attempt = 0; attempt <= BROWSER_WINDOW_EINTR_RETRY_LIMIT; attempt += 1) {
    try {
      return new BrowserWindow(options);
    } catch (error) {
      if (!isInterruptedReadError(error)) {
        throw error;
      }
      lastInterruptedError = error;
      if (attempt >= BROWSER_WINDOW_EINTR_RETRY_LIMIT) {
        break;
      }
      console.warn('BrowserWindow creation interrupted by EINTR; retrying.', {
        attempt: attempt + 1,
        retryLimit: BROWSER_WINDOW_EINTR_RETRY_LIMIT,
      });
    }
  }
  throw lastInterruptedError;
}

function runWindowStartupStepWithRetry(name, fn, options = {}) {
  let lastInterruptedError = null;
  const optional = Boolean(options.optional);
  for (let attempt = 0; attempt <= WINDOW_STARTUP_STEP_EINTR_RETRY_LIMIT; attempt += 1) {
    try {
      return fn();
    } catch (error) {
      if (!isInterruptedReadError(error)) {
        throw error;
      }
      lastInterruptedError = error;
      if (attempt >= WINDOW_STARTUP_STEP_EINTR_RETRY_LIMIT) {
        break;
      }
      console.warn('Window startup step interrupted by EINTR; retrying.', {
        step: name,
        attempt: attempt + 1,
        retryLimit: WINDOW_STARTUP_STEP_EINTR_RETRY_LIMIT,
      });
    }
  }
  if (optional) {
    console.warn(`Window startup step interrupted by EINTR; continuing without ${name}.`);
    return undefined;
  }
  throw lastInterruptedError;
}

function createMainWindow(options = {}) {
  const isDev = isDevRuntime();
  const hashRoute = '/';

  const win = createBrowserWindowWithRetry({
    width: 1000,
    height: 700,
    minWidth: 800,
    minHeight: 600,
    frame: true,
    transparent: false,
    // The middle stop of the renderer's fog ground (index.css --app-background), so the
    // window shows the same color before the first paint.
    backgroundColor: '#0f131a',
    hasShadow: true,
    resizable: true,
    fullscreenable: true,
    webPreferences: {
      preload: path.join(__dirname, 'dist', 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      webviewTag: false,
      additionalArguments: buildUiLanguageAdditionalArguments(options.initialUiLanguage),
      // 配布版は devtools を開けない（ログ/内部情報の露出を防ぐ）
      devTools: isDev,
    },
    ...(process.platform === 'darwin'
      ? {
          titleBarStyle: 'hiddenInset',
          trafficLightPosition: { x: 10, y: 10 },
          visualEffectState: 'active',
        }
      : {}),
  });

  runWindowStartupStepWithRetry('center', () => win.center(), { optional: true });

  if (isDev) {
    const url = buildFrontendDevOrigin();
    const finalUrl = buildFrontendDevHashUrl(`#${hashRoute}`);
    // Devサーバーが立ち上がるまで待機してから読み込む（白画面長時間化を防止）
    void loadDevWindow(win, { serverUrl: url, finalUrl }).catch((error) => {
      reportDevWindowLoadFailure(error, finalUrl);
    });
  } else {
    loadPackagedIndex(win, hashRoute);
  }

  let hasRetried = false;
  runWindowStartupStepWithRetry(
    'did-fail-load-listener',
    () => {
      win.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
        if (!isDevRuntime()) {
          console.error('MAIN_WINDOW_LOAD_FAILED', {
            errorCode,
            errorDescription,
            validatedURL,
          });
          if (hasRetried) return;
          hasRetried = true;
          loadPackagedIndex(win, hashRoute);
        }
      });
    },
    { optional: true }
  );

  runWindowStartupStepWithRetry(
    'did-finish-load-listener',
    () => {
      win.webContents.on('did-finish-load', () => {
        // noop
      });
    },
    { optional: true }
  );

  // --- DevTools hard-disable for release ---
  if (!isDev) {
    try {
      win.webContents.on('devtools-opened', () => {
        try {
          win.webContents.closeDevTools();
        } catch {}
      });
      win.webContents.on('before-input-event', (event, input) => {
        // DevTools shortcuts: Cmd/Ctrl+Alt+I, Cmd/Ctrl+Shift+I, F12 etc.
        const key = String(input && input.key ? input.key : '');
        const ctrlOrMeta = Boolean(input && (input.control || input.meta));
        const shift = Boolean(input && input.shift);
        const alt = Boolean(input && input.alt);
        const isDevToolsShortcut =
          key === 'F12' ||
          (ctrlOrMeta && alt && key.toLowerCase() === 'i') ||
          (ctrlOrMeta && shift && key.toLowerCase() === 'i');
        if (isDevToolsShortcut) {
          try {
            event.preventDefault();
          } catch {}
        }
      });
    } catch {
      // no-op
    }
  }

  setupWindowEventListeners(win);
  return win;
}

module.exports = {
  createMainWindow,
  setupWindowEventListeners,
  createBrowserWindowWithRetry,
  loadDevWindow,
  requestHttpOk,
  waitForDevServer,
  waitForWindowLoad,
  isInterruptedReadError,
};
