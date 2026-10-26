/**
 * Electron UI E2E harness.
 *
 * 1 テストごとに以下を丸ごと起動・破棄する（テスト間で状態を共有しない）:
 * - Supabase loopback stub（agents/scripts/dev/isolated_env/supabase_stub.py）
 * - local backend 用 env ファイル（agents/.env.local.backend.example の placeholder を
 *   stub/loopback ダミーで充足したもの。実 Supabase / 実 proxy へ向かう値は含めない）
 * - Vite dev server（動的ポート）
 * - Electron 本体（PANTARAY_USER_DATA_DIR で userData を一時 dir に完全隔離）
 */
import { _electron, type ElectronApplication, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

const FRONTEND_ROOT = path.resolve(__dirname, '..', '..');
const REPO_ROOT = path.resolve(FRONTEND_ROOT, '..');
const AGENTS_ROOT = path.join(REPO_ROOT, 'agents');
const SUPABASE_STUB_PATH = path.join(
  AGENTS_ROOT,
  'scripts',
  'dev',
  'isolated_env',
  'supabase_stub.py'
);
const BACKEND_ENV_EXAMPLE_PATH = path.join(AGENTS_ROOT, '.env.local.backend.example');
const STUB_PUBLISHABLE_KEY = 'sb_publishable_e2e_stub';
// electron/src/main.ts: LOCAL_BACKEND_ARTIFACT_ROOT_DIRNAME under userData.
const LOCAL_BACKEND_ARTIFACT_ROOT_DIRNAME = 'local-backend-artifacts';
// electron/local_backend_runtime_config.js: PYTHON_LOG_FILENAME in app.getPath('logs').
const LOCAL_BACKEND_LOG_FILENAME = 'pantaray-local-backend.log';
const SERVER_READY_TIMEOUT_MS = 60_000;
const SERVER_POLL_INTERVAL_MS = 250;
const CONTROL_SOCKET_STATUS_OPERATION = 'status';

export type ControlSocketStatus = {
  ok: boolean;
  helper_instance_id?: string;
  cloud_session_state?: string;
  llm_route?: string;
  backend_host?: string;
  backend_port?: number;
};

export type ElectronE2EHarness = {
  app: ElectronApplication;
  page: Page;
  controlSocketPath: string;
  /** `LOCAL_ARTIFACT_ROOT` of this run; stored images live under `generated/images`. */
  localArtifactRoot: string;
  readControlSocketStatus: () => Promise<ControlSocketStatus>;
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function inheritedEnv(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)
  );
}

function allocFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address() as net.AddressInfo;
      server.close(() => resolve(address.port));
    });
  });
}

async function waitForHttpOk(url: string, label: string): Promise<void> {
  const deadline = Date.now() + SERVER_READY_TIMEOUT_MS;
  let lastError = 'no response';
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: 'manual' });
      if (response.status >= 200 && response.status < 400) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await sleep(SERVER_POLL_INTERVAL_MS);
  }
  throw new Error(`${label} did not become ready at ${url}: ${lastError}`);
}

/**
 * example の placeholder 行（KEY=<...>）を実値で置換した env ファイル本文を作る。
 * example に新しいキーが増えて値が未定義の場合は fail-fast する
 * （bundle 生成側の "Missing local backend env key" よりも原因が分かる）。
 */
function renderBackendEnvFile(params: {
  exampleText: string;
  values: Record<string, string>;
}): string {
  const lines: string[] = [];
  const seenKeys = new Set<string>();
  for (const rawLine of params.exampleText.split('\n')) {
    const match = /^([A-Z][A-Z0-9_]*)=/.exec(rawLine.trim());
    if (!match) continue;
    const key = match[1];
    const value = params.values[key];
    if (value === undefined) {
      throw new Error(
        `E2E backend env has no value for ${key} (new key in .env.local.backend.example?)`
      );
    }
    seenKeys.add(key);
    lines.push(`${key}=${value}`);
  }
  const unusedKeys = Object.keys(params.values).filter((key) => !seenKeys.has(key));
  if (unusedKeys.length > 0) {
    throw new Error(
      `E2E backend env defines keys missing from the example: ${unusedKeys.join(', ')}`
    );
  }
  return `${lines.join('\n')}\n`;
}

function buildBackendEnvValues(params: { rendererOrigins: string[] }): Record<string, string> {
  return {
    NODE_ENV: 'development',
    USE_MOCKS: 'false',
    LOG_LEVEL: 'INFO',
    ALLOWED_ORIGINS: params.rendererOrigins.join(','),
    ALLOWED_HOSTS: 'localhost,127.0.0.1',
    LOCAL_DB_BUSY_TIMEOUT_MS: '5000',
  };
}

function spawnDetached(params: {
  command: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  logPath: string;
}): ChildProcess {
  const logFd = fs.openSync(params.logPath, 'a');
  const child = spawn(params.command, params.args, {
    cwd: params.cwd,
    env: params.env,
    detached: true,
    stdio: ['ignore', logFd, logFd],
  });
  fs.closeSync(logFd);
  return child;
}

function killProcessGroup(child: ChildProcess | null): void {
  if (!child || child.pid === undefined || child.exitCode !== null) return;
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch {
    try {
      child.kill('SIGKILL');
    } catch {
      // 既に終了している
    }
  }
}

function readControlSocketStatusOnce(socketPath: string): Promise<ControlSocketStatus> {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = '';
    socket.once('error', (error) => {
      socket.destroy();
      reject(error);
    });
    socket.once('connect', () => {
      socket.write(
        `${JSON.stringify({ operation: CONTROL_SOCKET_STATUS_OPERATION, payload: {} })}\n`
      );
    });
    socket.on('data', (chunk) => {
      buffer += String(chunk);
      const newlineIndex = buffer.indexOf('\n');
      if (newlineIndex < 0) return;
      socket.end();
      try {
        const response = JSON.parse(buffer.slice(0, newlineIndex)) as ControlSocketStatus & {
          local_api_token?: string;
        };
        // The runtime-issued token is reported here and nowhere else; tests never
        // need it, and failures print this object verbatim.
        const { local_api_token: _localApiToken, ...status } = response;
        resolve(status);
      } catch (error) {
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  });
}

async function findMainWindow(app: ElectronApplication, viteOrigin: string): Promise<Page> {
  const deadline = Date.now() + SERVER_READY_TIMEOUT_MS;
  // 通知 overlay など別 window と取り違えないよう、dev server origin を持ち、かつ
  // overlay ページ（notification.html — 同じ dev origin 配下）ではない window を探す。
  while (Date.now() < deadline) {
    for (const candidate of app.windows()) {
      const url = candidate.url();
      if (url.startsWith(viteOrigin) && !url.includes('notification.html')) return candidate;
    }
    await sleep(SERVER_POLL_INTERVAL_MS);
  }
  const urls = app.windows().map((candidate) => candidate.url());
  throw new Error(`Main window did not load ${viteOrigin}. Windows: ${JSON.stringify(urls)}`);
}

export async function launchElectronE2E(): Promise<{
  harness: ElectronE2EHarness;
  stop: (options: { keepArtifacts: boolean }) => Promise<void>;
}> {
  // NOTE: unix socket の sun_path 上限（macOS で 104 bytes）に収めるため /tmp 直下の短い
  // ディレクトリを使う（control.sock が userData 配下に作られる）。
  const workDir = fs.mkdtempSync('/tmp/pantaray-e2e-');
  const userDataDir = path.join(workDir, 'ud');
  fs.mkdirSync(userDataDir, { recursive: true });
  // UI 言語を固定してテキストベースの locator を決定的にする。
  const settingsDir = path.join(userDataDir, 'settings', '__logged_out__');
  fs.mkdirSync(settingsDir, { recursive: true });
  fs.writeFileSync(path.join(settingsDir, 'ui-settings.json'), '{"ui_language":"ja"}\n');
  // The flows start in the app, so first-run setup is already finished.
  fs.writeFileSync(path.join(userDataDir, 'onboarding.json'), '{"status":"completed"}\n');
  const backendEnvPath = path.join(workDir, 'backend.env');
  const controlSocketPath = path.join(userDataDir, 'local-backend', 'control.sock');

  const stubPort = await allocFreePort();
  const vitePort = await allocFreePort();
  const backendNominalPort = await allocFreePort();
  const stubOrigin = `http://127.0.0.1:${stubPort}`;
  const viteOrigin = `http://localhost:${vitePort}`;
  const backendNominalUrl = `http://127.0.0.1:${backendNominalPort}`;

  let stubProcess: ChildProcess | null = null;
  let viteProcess: ChildProcess | null = null;
  let app: ElectronApplication | null = null;
  let backendLogPath: string | null = null;

  const stop = async (options: { keepArtifacts: boolean }): Promise<void> => {
    if (app) {
      const electronProcess = app.process();
      try {
        // macOS の close ハンドラは isQuitting=false だと hide するだけなので明示 quit する。
        await app.evaluate(({ app: electronApp }) => {
          (electronApp as { isQuitting?: boolean }).isQuitting = true;
        });
        await app.close();
      } catch {
        // graceful quit に失敗した場合は下の SIGKILL に任せる
      }
      if (electronProcess.exitCode === null) {
        try {
          electronProcess.kill('SIGKILL');
        } catch {
          // 既に終了している
        }
      }
      app = null;
    }
    killProcessGroup(viteProcess);
    killProcessGroup(stubProcess);
    if (options.keepArtifacts) {
      // local backend の構造化ログは LOG_FILE_PATH（app.getPath('logs')）にしか
      // 書かれず、electron.log には uvicorn の stdout しか流れない。失敗調査に
      // 必要なので workDir へ写す（CI の artifact glob は workDir 直下の *.log）。
      if (backendLogPath !== null) {
        try {
          fs.copyFileSync(backendLogPath, path.join(workDir, 'local-backend.log'));
        } catch {
          // backend が起動する前に失敗した場合はログ自体が無い
        }
      }
      console.log(`[e2e] artifacts kept for debugging: ${workDir}`);
    } else {
      fs.rmSync(workDir, { recursive: true, force: true });
    }
  };

  try {
    stubProcess = spawnDetached({
      command: 'uv',
      args: ['run', 'python', SUPABASE_STUB_PATH, '--port', String(stubPort)],
      cwd: AGENTS_ROOT,
      env: process.env,
      logPath: path.join(workDir, 'supabase-stub.log'),
    });
    await waitForHttpOk(`${stubOrigin}/health`, 'supabase stub');

    fs.writeFileSync(
      backendEnvPath,
      renderBackendEnvFile({
        exampleText: fs.readFileSync(BACKEND_ENV_EXAMPLE_PATH, 'utf8'),
        values: buildBackendEnvValues({
          rendererOrigins: [viteOrigin, `http://127.0.0.1:${vitePort}`],
        }),
      }),
      { mode: 0o600 }
    );

    const rendererEnv = {
      VITE_WEB_APP_URL: viteOrigin,
      VITE_SUPABASE_URL: stubOrigin,
      VITE_SUPABASE_PUBLISHABLE_KEY: STUB_PUBLISHABLE_KEY,
      VITE_API_HOST: backendNominalUrl,
    };
    viteProcess = spawnDetached({
      command: process.execPath,
      args: [
        path.join(FRONTEND_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'),
        '--port',
        String(vitePort),
        '--strictPort',
      ],
      cwd: FRONTEND_ROOT,
      env: { ...process.env, ...rendererEnv },
      logPath: path.join(workDir, 'vite.log'),
    });
    await waitForHttpOk(viteOrigin, 'vite dev server');

    // main プロセス（と main が spawn する local backend）の出力を失敗調査用に残す。
    const electronLogFd = fs.openSync(path.join(workDir, 'electron.log'), 'a');
    const appendElectronLog = (chunk: Buffer | string) => {
      try {
        fs.writeSync(electronLogFd, chunk);
      } catch {
        // ログ収集はベストエフォート
      }
    };
    app = await _electron.launch({
      executablePath: require('electron') as unknown as string,
      args: [FRONTEND_ROOT],
      cwd: FRONTEND_ROOT,
      env: {
        ...inheritedEnv(),
        ...rendererEnv,
        NODE_ENV: 'development',
        FRONTEND_PORT: String(vitePort),
        PANTARAY_USER_DATA_DIR: userDataDir,
        // macOS の権限が無いと会話ウィンドウは開かず「記録を始める」画面が出る。
        // CI ランナーには Accessibility を付与できないので、未パッケージ版だけで
        // 効くこの上書きで付与済みとして扱う（macosPermissions.ts）。
        PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS: '1',
        PANTARAY_LOCAL_BACKEND_ENV_FILE: backendEnvPath,
        BACKEND_URL: backendNominalUrl,
      },
    });
    app.process().stdout?.on('data', appendElectronLog);
    app.process().stderr?.on('data', appendElectronLog);
    backendLogPath = path.join(
      await app.evaluate(({ app: electronApp }) => electronApp.getPath('logs')),
      LOCAL_BACKEND_LOG_FILENAME
    );

    const page = await findMainWindow(app, viteOrigin);

    const readControlSocketStatus = () => readControlSocketStatusOnce(controlSocketPath);
    return {
      harness: {
        app,
        page,
        controlSocketPath,
        localArtifactRoot: path.join(userDataDir, LOCAL_BACKEND_ARTIFACT_ROOT_DIRNAME),
        readControlSocketStatus,
      },
      stop,
    };
  } catch (error) {
    await stop({ keepArtifacts: true });
    throw error;
  }
}
