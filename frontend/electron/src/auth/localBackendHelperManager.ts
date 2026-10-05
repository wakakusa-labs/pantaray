import { spawn, type ChildProcess, type StdioOptions } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createConnection } from 'node:net';
import path from 'node:path';

type LoggerLike = {
  info?: (message: string, payload?: unknown) => void;
  warn?: (message: string, payload?: unknown) => void;
  error?: (message: string, payload?: unknown) => void;
};

type SpawnFn = typeof spawn;

type HelperProcessHandle = ChildProcess & {
  exitCode: number | null;
  signalCode: NodeJS.Signals | null;
};

type HelperStatus = {
  helperInstanceId: string;
  /** Minted per helper process; the control socket is the only channel that discloses it. */
  localApiToken: string;
  cloudSessionState: string;
  backendHost: string;
  backendPort: number;
};

type HelperReadinessProbe =
  | {
      kind: 'owned';
      status: HelperStatus;
    }
  | {
      kind: 'foreign';
      status: HelperStatus;
    }
  | {
      kind: 'unavailable';
      error: Error;
    };

type ProbeReadinessFn = (
  socketPath: string,
  expectedHelperInstanceId: string
) => Promise<HelperReadinessProbe>;

// Design limit: the first start after an install waits while macOS scans the
// new files and verifies each native library on first load, which has taken
// over 30 s; raise this if a first start is observed to exceed 120 s.
const HELPER_READY_TIMEOUT_MS = 120_000;
const HELPER_TERMINATION_TIMEOUT_MS = 5_000;
const HELPER_TERMINATION_POLL_INTERVAL_MS = 50;
const DEVELOPMENT_HELPER_EXECUTABLE = 'uv';
const STATUS_OPERATION = 'status';
const HELPER_INSTANCE_ID_ENV = 'PANTARAY_HELPER_INSTANCE_ID';
const MAIN_PROCESS_PID_ENV = 'PANTARAY_MAIN_PROCESS_PID';
const PYTHON_DONT_WRITE_BYTECODE_ENV = 'PYTHONDONTWRITEBYTECODE';
const PYTHON_PYCACHE_PREFIX_ENV = 'PYTHONPYCACHEPREFIX';
const PYTHONPATH_ENV = 'PYTHONPATH';
const HELPER_PYCACHE_DIRNAME = 'local-backend-python-cache';
const AGENTS_SOURCE_DIRNAME = 'src';
const DEV_HELPER_STDIO = 'inherit';
// stderr is piped so a helper that dies before Python logging is configured
// still leaves its traceback; stdin and stdout stay detached.
const PACKAGED_HELPER_STDIO: StdioOptions = ['ignore', 'ignore', 'pipe'];
// Enough for the last traceback of a failed start, small enough for one log line.
const HELPER_STDERR_TAIL_BYTES = 8 * 1024;
const LOCAL_BACKEND_DYNAMIC_PORT = 0;
const CANONICAL_LOOPBACK_HOST = '127.0.0.1';
const ALLOWED_LOOPBACK_HOSTS = new Set([CANONICAL_LOOPBACK_HOST, 'localhost']);

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function normalizeRequiredString(value: string, fieldName: string): string {
  const normalized = String(value || '').trim();
  if (!normalized) {
    throw new Error(`${fieldName} must not be empty.`);
  }
  return normalized;
}

function normalizePositiveInteger(value: unknown, fieldName: string): number {
  const normalized =
    typeof value === 'number' ? value : Number.parseInt(String(value || '').trim(), 10);
  if (!Number.isInteger(normalized) || normalized <= 0) {
    throw new Error(`${fieldName} must be a positive integer.`);
  }
  return normalized;
}

function normalizeLoopbackHost(value: string, fieldName: string): string {
  const normalized = normalizeRequiredString(value, fieldName);
  if (!ALLOWED_LOOPBACK_HOSTS.has(normalized)) {
    throw new Error(`${fieldName} must be a loopback host.`);
  }
  return normalized === 'localhost' ? CANONICAL_LOOPBACK_HOST : normalized;
}

function buildRuntimeBackendUrl(host: string, port: number): string {
  const url = new URL('http://127.0.0.1/');
  url.hostname = normalizeLoopbackHost(host, 'backend_host');
  url.port = String(port);
  return url.toString().replace(/\/$/, '');
}

function buildHelperPythonCachePrefix(controlSocketPath: string): string {
  const socketDir = path.dirname(normalizeRequiredString(controlSocketPath, 'controlSocketPath'));
  return path.join(path.dirname(socketDir), HELPER_PYCACHE_DIRNAME);
}

function buildDevelopmentPythonPath(
  agentsRoot: string,
  inheritedPythonPath: string | undefined
): string {
  const sourcePath = path.join(
    normalizeRequiredString(agentsRoot, 'agentsRoot'),
    AGENTS_SOURCE_DIRNAME
  );
  const inherited = String(inheritedPythonPath || '').trim();
  return inherited ? `${sourcePath}${path.delimiter}${inherited}` : sourcePath;
}

async function probeHelperReadiness(
  socketPath: string,
  expectedHelperInstanceId: string
): Promise<HelperReadinessProbe> {
  try {
    const status = await new Promise<HelperStatus>((resolve, reject) => {
      const socket = createConnection(socketPath);
      let responseBuffer = '';
      const onError = (error: Error): void => {
        socket.destroy();
        reject(error);
      };
      socket.once('error', onError);
      socket.once('connect', () => {
        socket.write(`${JSON.stringify({ operation: STATUS_OPERATION, payload: {} })}\n`);
      });
      socket.on('data', (chunk: Buffer | string) => {
        responseBuffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
        const newlineIndex = responseBuffer.indexOf('\n');
        if (newlineIndex < 0) {
          return;
        }
        try {
          const response = JSON.parse(responseBuffer.slice(0, newlineIndex));
          if (!response || typeof response !== 'object' || response.ok !== true) {
            throw new Error('Local backend helper status response must be ok=true.');
          }
          const helperInstanceId = normalizeRequiredString(
            (response as { helper_instance_id?: string }).helper_instance_id ?? '',
            'helper_instance_id'
          );
          const localApiToken = normalizeRequiredString(
            (response as { local_api_token?: string }).local_api_token ?? '',
            'local_api_token'
          );
          const cloudSessionState = normalizeRequiredString(
            (response as { cloud_session_state?: string }).cloud_session_state ?? '',
            'cloud_session_state'
          );
          const backendHost = normalizeLoopbackHost(
            (response as { backend_host?: string }).backend_host ?? '',
            'backend_host'
          );
          const backendPort = normalizePositiveInteger(
            (response as { backend_port?: unknown }).backend_port,
            'backend_port'
          );
          socket.removeListener('error', onError);
          socket.end();
          resolve({
            helperInstanceId,
            localApiToken,
            cloudSessionState,
            backendHost,
            backendPort,
          });
        } catch (error) {
          socket.destroy();
          reject(error instanceof Error ? error : new Error(String(error)));
        }
      });
    });
    if (status.helperInstanceId === expectedHelperInstanceId) {
      return { kind: 'owned', status };
    }
    return { kind: 'foreign', status };
  } catch (error) {
    return {
      kind: 'unavailable',
      error: error instanceof Error ? error : new Error(String(error)),
    };
  }
}

// `killed` only records that a signal was delivered, so liveness follows the
// exit status: a helper that has not exited may still hold its credential.
function isAlive(handle: HelperProcessHandle | null): handle is HelperProcessHandle {
  return handle !== null && handle.exitCode === null && handle.signalCode === null;
}

export function createLocalBackendHelperManager(params: {
  isDevRuntime: boolean;
  agentsRoot: string;
  resourcesPath: string;
  getControlSocketPath: () => string;
  getHelperExecutablePath: () => string | null;
  getLoopbackBinding: () => {
    bindHost: string;
    bindPort: number;
  };
  logger?: LoggerLike;
  spawnFn?: SpawnFn;
  probeReadiness?: ProbeReadinessFn;
  onReady?: (ready: { runtimeBackendUrl: string }) => void;
  /** 起動済みの helper が main の意図しない理由で終了した。ローカル実行環境はもう無い。 */
  onUnexpectedExit?: () => void;
}): {
  /** 起動済みなら何もしない。戻り値の instance id は helper の世代を表す。 */
  ensureStarted: () => Promise<{ helperInstanceId: string }>;
  /** 稼働中の helper が発行したローカル API トークン。renderer にもログにも渡さない。 */
  getLocalApiToken: () => string | null;
  terminateCurrentHelper: () => Promise<void>;
  stopForShutdown: () => Promise<void>;
} {
  const spawnFn = params.spawnFn ?? spawn;
  const probeReadiness = params.probeReadiness ?? probeHelperReadiness;
  let currentHandle: HelperProcessHandle | null = null;
  let currentHelperInstanceId: string | null = null;
  let currentLocalApiToken: string | null = null;
  const readyHandles = new WeakSet<HelperProcessHandle>();
  // A start that keeps failing respawns until the ready deadline; one record
  // per start keeps the log readable.
  let notReadyExitRecorded = false;
  let startupQueue: Promise<{ helperInstanceId: string }> = Promise.resolve({
    helperInstanceId: '',
  });

  function resolveSpawnCommand(): {
    command: string;
    args: string[];
    cwd: string;
    env: NodeJS.ProcessEnv;
    helperInstanceId: string;
  } {
    const binding = params.getLoopbackBinding();
    const bindHost = normalizeRequiredString(binding.bindHost, 'bindHost');
    const bindPort = LOCAL_BACKEND_DYNAMIC_PORT;
    const helperInstanceId = randomUUID();
    const agentsRoot = normalizeRequiredString(params.agentsRoot, 'agentsRoot');
    if (!Number.isInteger(bindPort) || bindPort < 0) {
      throw new Error('bindPort must be a non-negative integer.');
    }
    if (params.isDevRuntime) {
      return {
        command: DEVELOPMENT_HELPER_EXECUTABLE,
        args: [
          'run',
          'python',
          '-m',
          'pantaray_agents',
          '--host',
          bindHost,
          '--port',
          String(bindPort),
        ],
        cwd: agentsRoot,
        env: {
          ...process.env,
          [HELPER_INSTANCE_ID_ENV]: helperInstanceId,
          [MAIN_PROCESS_PID_ENV]: String(process.pid),
          [PYTHON_PYCACHE_PREFIX_ENV]: buildHelperPythonCachePrefix(params.getControlSocketPath()),
          [PYTHONPATH_ENV]: buildDevelopmentPythonPath(agentsRoot, process.env[PYTHONPATH_ENV]),
        },
        helperInstanceId,
      };
    }
    return {
      command: normalizeRequiredString(
        params.getHelperExecutablePath() ?? '',
        'LOCAL_BACKEND_HELPER_EXECUTABLE'
      ),
      args: ['-m', 'pantaray_agents', '--host', bindHost, '--port', String(bindPort)],
      cwd: normalizeRequiredString(params.resourcesPath, 'resourcesPath'),
      env: {
        ...process.env,
        [HELPER_INSTANCE_ID_ENV]: helperInstanceId,
        [MAIN_PROCESS_PID_ENV]: String(process.pid),
        // No PYTHONPYCACHEPREFIX: with one set, Python ignores the bytecode
        // shipped next to the bundled sources and recompiles every module.
        [PYTHON_DONT_WRITE_BYTECODE_ENV]: '1',
      },
      helperInstanceId,
    };
  }

  function spawnHelper(spawnConfig: ReturnType<typeof resolveSpawnCommand>): HelperProcessHandle {
    const child = spawnFn(spawnConfig.command, spawnConfig.args, {
      cwd: spawnConfig.cwd,
      env: spawnConfig.env,
      stdio: params.isDevRuntime ? DEV_HELPER_STDIO : PACKAGED_HELPER_STDIO,
    }) as HelperProcessHandle;
    currentHandle = child;
    currentHelperInstanceId = spawnConfig.helperInstanceId;
    currentLocalApiToken = null;
    // A helper that could not be spawned at all — a missing executable, a fork that the
    // OS refused — reports only through 'error', and an unsubscribed 'error' takes the
    // main process down with it. Node has already set the negative exit status by then,
    // so this handle is dead like any other and ensureStartedInner's spawn loop takes it.
    child.once('error', (error: Error) => {
      params.logger?.error?.('LOCAL_BACKEND_HELPER_SPAWN_FAILED', {
        pid: child.pid ?? null,
        message: error.message,
      });
    });
    let stderrTail = Buffer.alloc(0);
    child.stderr?.on('data', (chunk: Buffer) => {
      stderrTail = Buffer.concat([stderrTail, chunk]);
      if (stderrTail.length > HELPER_STDERR_TAIL_BYTES) {
        stderrTail = stderrTail.subarray(stderrTail.length - HELPER_STDERR_TAIL_BYTES);
      }
    });
    // 'close' follows the end of stderr, so the tail is complete here.
    child.once('close', (code: number | null, signal: NodeJS.Signals | null) => {
      if (readyHandles.has(child) || notReadyExitRecorded) {
        return;
      }
      notReadyExitRecorded = true;
      params.logger?.error?.('LOCAL_BACKEND_HELPER_EXITED_BEFORE_READY', {
        pid: child.pid ?? null,
        code,
        signal,
        stderrTail: stderrTail.toString('utf8'),
      });
    });
    child.once('exit', (code: number | null, signal: NodeJS.Signals | null) => {
      if (currentHandle !== child) {
        return;
      }
      // main 自身の kill は current を外してから撃つので、current のまま終了した
      // helper だけが main の知らない終了。readiness より前の終了は
      // ensureStartedInner の spawn ループが引き取るので、token を受け取った
      // helper — 利用可能として公開された世代 — に限って外へ伝える。
      const wasReady = currentLocalApiToken !== null;
      currentHandle = null;
      if (!wasReady) {
        return;
      }
      params.logger?.error?.('LOCAL_BACKEND_HELPER_EXITED', {
        pid: child.pid ?? null,
        code,
        signal,
      });
      params.onUnexpectedExit?.();
    });
    return child;
  }

  async function ensureStartedInner(): Promise<{ helperInstanceId: string }> {
    if (isAlive(currentHandle) && currentHelperInstanceId) {
      return { helperInstanceId: currentHelperInstanceId };
    }
    const socketPath = normalizeRequiredString(params.getControlSocketPath(), 'controlSocketPath');
    const startedAtMs = Date.now();
    const deadline = Date.now() + HELPER_READY_TIMEOUT_MS;
    notReadyExitRecorded = false;
    let spawnConfig = resolveSpawnCommand();
    let child = spawnHelper(spawnConfig);
    let lastError: Error | null = null;

    while (Date.now() < deadline) {
      const readiness = await probeReadiness(socketPath, spawnConfig.helperInstanceId);
      if (readiness.kind === 'owned') {
        readyHandles.add(child);
        currentLocalApiToken = readiness.status.localApiToken;
        const readyRuntimeBackendUrl = buildRuntimeBackendUrl(
          readiness.status.backendHost,
          readiness.status.backendPort
        );
        params.onReady?.({
          runtimeBackendUrl: readyRuntimeBackendUrl,
        });
        params.logger?.info?.('LOCAL_BACKEND_HELPER_READY', {
          pid: child.pid ?? null,
          runtimeBackendUrl: readyRuntimeBackendUrl,
          ms: Date.now() - startedAtMs,
        });
        return { helperInstanceId: spawnConfig.helperInstanceId };
      }
      if (readiness.kind === 'foreign') {
        lastError = new Error(
          `Expected helper_instance_id=${spawnConfig.helperInstanceId} but received ${readiness.status.helperInstanceId}`
        );
      } else {
        lastError = readiness.error;
        if (!isAlive(child)) {
          spawnConfig = resolveSpawnCommand();
          child = spawnHelper(spawnConfig);
        }
      }
      await sleep(HELPER_TERMINATION_POLL_INTERVAL_MS);
    }

    if (currentHandle === child) {
      currentHandle = null;
    }
    if (isAlive(child)) {
      child.kill('SIGTERM');
    }
    throw new Error(
      `Timed out waiting for owned local backend helper: ${lastError?.message ?? 'unknown error'}`
    );
  }

  async function waitUntilExited(handle: HelperProcessHandle): Promise<boolean> {
    const deadline = Date.now() + HELPER_TERMINATION_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (!isAlive(handle)) {
        return true;
      }
      await sleep(HELPER_TERMINATION_POLL_INTERVAL_MS);
    }
    return false;
  }

  async function waitForExit(handle: HelperProcessHandle): Promise<void> {
    if (await waitUntilExited(handle)) {
      return;
    }
    // A helper that outlives SIGTERM still holds whatever session it was given.
    handle.kill('SIGKILL');
    if (await waitUntilExited(handle)) {
      return;
    }
    throw new Error('Timed out waiting for local backend helper termination.');
  }

  // 稼働中の helper を main の要求で止める。current を外してから kill するのは、
  // exit ハンドラがこの終了を「main の知らない終了」と取り違えないため。
  async function stopCurrentHelper(): Promise<HelperProcessHandle | null> {
    const handle = currentHandle;
    if (!isAlive(handle)) {
      return null;
    }
    currentHandle = null;
    handle.kill('SIGTERM');
    await waitForExit(handle);
    return handle;
  }

  return {
    ensureStarted: (): Promise<{ helperInstanceId: string }> => {
      startupQueue = startupQueue.then(ensureStartedInner, ensureStartedInner);
      return startupQueue;
    },
    // The token dies with the process that minted it, so liveness is the only
    // thing that keeps it valid.
    getLocalApiToken: (): string | null => (isAlive(currentHandle) ? currentLocalApiToken : null),
    terminateCurrentHelper: async (): Promise<void> => {
      const handle = await stopCurrentHelper();
      if (handle) {
        params.logger?.info?.('LOCAL_BACKEND_HELPER_TERMINATED', {
          pid: handle.pid ?? null,
        });
      }
    },
    stopForShutdown: async (): Promise<void> => {
      await stopCurrentHelper();
    },
  };
}
