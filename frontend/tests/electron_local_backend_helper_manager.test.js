const assert = require('assert');
const { EventEmitter } = require('events');
const { test } = require('node:test');
const path = require('path');

const {
  createLocalBackendHelperManager,
} = require('../electron/dist/auth/localBackendHelperManager.js');

class FakeChildProcess extends EventEmitter {
  constructor({ exitDelayMs = 0 } = {}) {
    super();
    this.exitCode = null;
    this.signalCode = null;
    this.killed = false;
    this.pid = 1234;
    this.signals = [];
    this.exitDelayMs = exitDelayMs;
    this.stderr = new EventEmitter();
  }

  kill(signal = 'SIGTERM') {
    this.signals.push(signal);
    this.killed = true;
    const exit = () => {
      this.exitCode = 0;
      this.signalCode = signal;
      this.emit('exit', 0, signal);
    };
    if (this.exitDelayMs > 0) {
      setTimeout(exit, this.exitDelayMs);
    } else {
      exit();
    }
    return true;
  }
}

function buildOwnedStatus(helperInstanceId, overrides = {}) {
  return {
    helperInstanceId,
    localApiToken: overrides.localApiToken ?? `local-api-token-${helperInstanceId}`,
    cloudSessionState: overrides.cloudSessionState ?? 'absent',
    backendHost: overrides.backendHost ?? '127.0.0.1',
    backendPort: overrides.backendPort ?? 49152,
  };
}

function createReadyManager(children, onUnexpectedExit) {
  const pending = [...children];
  return createLocalBackendHelperManager({
    isDevRuntime: true,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => null,
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn: () => {
      const child = pending.shift();
      assert.ok(child);
      return child;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => ({
      kind: 'owned',
      status: buildOwnedStatus(helperInstanceId),
    }),
    onUnexpectedExit,
  });
}

test('ensureStarted spawns the helper and waits for control socket readiness', async () => {
  const spawnCalls = [];
  const probeCalls = [];
  const readyUrls = [];
  const fakeChild = new FakeChildProcess();
  const manager = createLocalBackendHelperManager({
    isDevRuntime: true,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => null,
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn(command, args, options) {
      spawnCalls.push({ command, args, options });
      return fakeChild;
    },
    probeReadiness: async (socketPath, helperInstanceId) => {
      probeCalls.push({ socketPath, helperInstanceId });
      return {
        kind: 'owned',
        status: buildOwnedStatus(helperInstanceId),
      };
    },
    onReady: ({ runtimeBackendUrl }) => {
      readyUrls.push(runtimeBackendUrl);
    },
  });

  await manager.ensureStarted();

  assert.equal(spawnCalls.length, 1);
  assert.equal(spawnCalls[0].command, 'uv');
  assert.deepStrictEqual(spawnCalls[0].args, [
    'run',
    'python',
    '-m',
    'pantaray_agents',
    '--host',
    '127.0.0.1',
    '--port',
    '0',
  ]);
  assert.equal(spawnCalls[0].options.cwd, '/tmp/agents');
  assert.equal(spawnCalls[0].options.stdio, 'inherit');
  assert.equal(
    typeof spawnCalls[0].options.env.PANTARAY_HELPER_INSTANCE_ID,
    'string'
  );
  assert.equal(
    spawnCalls[0].options.env.PANTARAY_MAIN_PROCESS_PID,
    String(process.pid)
  );
  assert.equal(
    spawnCalls[0].options.env.PYTHONPYCACHEPREFIX,
    '/tmp/user/local-backend-python-cache'
  );
  const expectedDevPythonPath = process.env.PYTHONPATH
    ? `/tmp/agents/src${path.delimiter}${process.env.PYTHONPATH}`
    : '/tmp/agents/src';
  assert.equal(spawnCalls[0].options.env.PYTHONPATH, expectedDevPythonPath);
  assert.deepStrictEqual(probeCalls, [
    {
      socketPath: '/tmp/user/local-backend/control.sock',
      helperInstanceId: spawnCalls[0].options.env.PANTARAY_HELPER_INSTANCE_ID,
    },
  ]);
  assert.deepStrictEqual(readyUrls, ['http://127.0.0.1:49152']);
});

test('terminateCurrentHelper kills the owned helper handle', async () => {
  const fakeChild = new FakeChildProcess();
  const manager = createLocalBackendHelperManager({
    isDevRuntime: true,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => null,
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn() {
      return fakeChild;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => ({
      kind: 'owned',
      status: buildOwnedStatus(helperInstanceId),
    }),
  });

  await manager.ensureStarted();
  await manager.terminateCurrentHelper();

  assert.equal(fakeChild.killed, true);
});

test('ensureStarted respawns after orphan helper disappears within the same call', async () => {
  const spawnCalls = [];
  const firstChild = new FakeChildProcess();
  const secondChild = new FakeChildProcess();
  secondChild.pid = 5678;
  const children = [firstChild, secondChild];
  let unexpectedExits = 0;
  let probeCount = 0;
  const manager = createLocalBackendHelperManager({
    isDevRuntime: true,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => null,
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn(command, args, options) {
      const child = children.shift();
      assert.ok(child);
      spawnCalls.push({ command, args, options, child });
      return child;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => {
      probeCount += 1;
      if (probeCount === 1) {
        return {
          kind: 'foreign',
          status: buildOwnedStatus('orphan-helper', { cloudSessionState: 'present' }),
        };
      }
      if (probeCount === 2) {
        firstChild.kill();
        return {
          kind: 'unavailable',
          error: new Error('socket unavailable'),
        };
      }
      return {
        kind: 'owned',
        status: buildOwnedStatus(helperInstanceId),
      };
    },
    onUnexpectedExit: () => {
      unexpectedExits += 1;
    },
  });

  await manager.ensureStarted();

  assert.equal(spawnCalls.length, 2);
  assert.equal(spawnCalls[0].options.stdio, 'inherit');
  assert.equal(spawnCalls[1].options.stdio, 'inherit');
  assert.notEqual(
    spawnCalls[0].options.env.PANTARAY_HELPER_INSTANCE_ID,
    spawnCalls[1].options.env.PANTARAY_HELPER_INSTANCE_ID
  );
  // 起動待ちの間に落ちた helper はこのループが引き取る。degraded にはしない。
  assert.equal(unexpectedExits, 0);
});

test('packaged runtime detaches helper stdin and stdout and keeps stderr', async () => {
  const spawnCalls = [];
  const fakeChild = new FakeChildProcess();
  const manager = createLocalBackendHelperManager({
    isDevRuntime: false,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => '/tmp/resources/local_backend_helper',
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn(command, args, options) {
      spawnCalls.push({ command, args, options });
      return fakeChild;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => ({
      kind: 'owned',
      status: buildOwnedStatus(helperInstanceId),
    }),
  });

  await manager.ensureStarted();

  assert.equal(spawnCalls.length, 1);
  assert.equal(spawnCalls[0].command, '/tmp/resources/local_backend_helper');
  assert.deepStrictEqual(spawnCalls[0].options.stdio, ['ignore', 'ignore', 'pipe']);
  assert.equal(spawnCalls[0].options.env.PYTHONDONTWRITEBYTECODE, '1');
  assert.equal(spawnCalls[0].options.env.PYTHONPATH, process.env.PYTHONPATH);
  // A cache prefix would make Python ignore the bytecode shipped in the bundle.
  assert.equal(spawnCalls[0].options.env.PYTHONPYCACHEPREFIX, undefined);
  assert.deepStrictEqual(spawnCalls[0].args.slice(-2), ['--port', '0']);
});

test('a failing start records one stderr tail, bounded, for helpers that died before ready', async () => {
  const errors = [];
  const firstDead = new FakeChildProcess();
  const secondDead = new FakeChildProcess();
  const ready = new FakeChildProcess();
  const children = [firstDead, secondDead, ready];
  const die = (child, text) => {
    child.stderr.emit('data', Buffer.from(text, 'utf8'));
    child.exitCode = 1;
    child.emit('exit', 1, null);
    child.emit('close', 1, null);
  };
  let probeCount = 0;
  const manager = createLocalBackendHelperManager({
    isDevRuntime: false,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => '/tmp/resources/local_backend_helper',
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn: () => {
      const child = children.shift();
      assert.ok(child);
      return child;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => {
      probeCount += 1;
      if (probeCount === 1) {
        die(firstDead, `${'x'.repeat(20_000)}\nMigrationError: LOCAL_RUNTIME_ALREADY_ACTIVE\n`);
        return { kind: 'unavailable', error: new Error('connect ENOENT') };
      }
      if (probeCount === 2) {
        die(secondDead, 'second failure\n');
        return { kind: 'unavailable', error: new Error('connect ENOENT') };
      }
      return { kind: 'owned', status: buildOwnedStatus(helperInstanceId) };
    },
    logger: { error: (message, payload) => errors.push({ message, payload }) },
  });

  await manager.ensureStarted();
  ready.emit('close', 0, 'SIGTERM');

  assert.deepStrictEqual(
    errors.map(({ message }) => message),
    ['LOCAL_BACKEND_HELPER_EXITED_BEFORE_READY']
  );
  const { payload } = errors[0];
  assert.equal(payload.code, 1);
  assert.ok(payload.stderrTail.endsWith('MigrationError: LOCAL_RUNTIME_ALREADY_ACTIVE\n'));
  assert.ok(Buffer.byteLength(payload.stderrTail) <= 8 * 1024);
});

test('terminateCurrentHelper resolves only after the helper process actually exits', async () => {
  const fakeChild = new FakeChildProcess({ exitDelayMs: 120 });
  const manager = createLocalBackendHelperManager({
    isDevRuntime: true,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => null,
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn: () => fakeChild,
    probeReadiness: async (socketPath, helperInstanceId) => ({
      kind: 'owned',
      status: buildOwnedStatus(helperInstanceId),
    }),
  });

  await manager.ensureStarted();
  await manager.terminateCurrentHelper();

  assert.deepEqual(fakeChild.signals, ['SIGTERM']);
  assert.notEqual(fakeChild.exitCode, null);
});

test('a ready helper that exits on its own is reported once as unavailable', async () => {
  const child = new FakeChildProcess();
  let unexpectedExits = 0;
  const manager = createReadyManager([child], () => {
    unexpectedExits += 1;
  });

  await manager.ensureStarted();
  // クラッシュ / OOM / 外部からの kill。main は何も要求していない。
  child.exitCode = 1;
  child.emit('exit', 1, null);

  assert.equal(unexpectedExits, 1);
  assert.equal(manager.getLocalApiToken(), null);
});

for (const method of ['terminateCurrentHelper', 'stopForShutdown']) {
  test(`${method} does not report the helper it stopped`, async () => {
    const child = new FakeChildProcess({ exitDelayMs: 20 });
    let unexpectedExits = 0;
    const manager = createReadyManager([child], () => {
      unexpectedExits += 1;
    });

    await manager.ensureStarted();
    await manager[method]();

    assert.deepEqual(child.signals, ['SIGTERM']);
    assert.equal(unexpectedExits, 0);
  });
}

test('a replaced helper exiting late does not report the live one as unavailable', async () => {
  const firstChild = new FakeChildProcess({ exitDelayMs: 20 });
  const secondChild = new FakeChildProcess();
  secondChild.pid = 5678;
  let unexpectedExits = 0;
  const manager = createReadyManager([firstChild, secondChild], () => {
    unexpectedExits += 1;
  });

  const first = await manager.ensureStarted();
  const stopping = manager.terminateCurrentHelper();
  const second = await manager.ensureStarted();
  await stopping;

  assert.notEqual(second.helperInstanceId, first.helperInstanceId);
  assert.notEqual(firstChild.exitCode, null);
  assert.equal(unexpectedExits, 0);
  assert.equal(manager.getLocalApiToken(), `local-api-token-${second.helperInstanceId}`);
});

test('a helper that cannot be spawned fails the start instead of the main process', async () => {
  // node は spawn できなかった子に負の exit status を入れて 'error' だけを出す。
  // 購読していないと EventEmitter がその場で投げ、main が uncaught で落ちる。
  const failedChild = new FakeChildProcess();
  const startedChild = new FakeChildProcess();
  const children = [failedChild, startedChild];
  let unexpectedExits = 0;
  let probeCount = 0;
  const manager = createLocalBackendHelperManager({
    isDevRuntime: false,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => '/tmp/resources/missing_local_backend_helper',
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn: () => {
      const child = children.shift();
      assert.ok(child);
      return child;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => {
      probeCount += 1;
      if (probeCount === 1) {
        // spawn が返ったあとに届く起動失敗。exit は出ない。
        failedChild.exitCode = -2;
        failedChild.emit(
          'error',
          new Error('spawn /tmp/resources/missing_local_backend_helper ENOENT')
        );
        return { kind: 'unavailable', error: new Error('socket unavailable') };
      }
      return { kind: 'owned', status: buildOwnedStatus(helperInstanceId) };
    },
    onUnexpectedExit: () => {
      unexpectedExits += 1;
    },
  });

  const started = await manager.ensureStarted();

  assert.equal(children.length, 0);
  assert.equal(manager.getLocalApiToken(), `local-api-token-${started.helperInstanceId}`);
  // 起動前に消えた helper は spawn ループが引き取る。degraded にはしない。
  assert.equal(unexpectedExits, 0);
});

test('getLocalApiToken follows the helper process that minted it', async () => {
  const firstChild = new FakeChildProcess();
  const secondChild = new FakeChildProcess();
  const children = [firstChild, secondChild];
  const manager = createLocalBackendHelperManager({
    isDevRuntime: true,
    agentsRoot: '/tmp/agents',
    resourcesPath: '/tmp/resources',
    getControlSocketPath: () => '/tmp/user/local-backend/control.sock',
    getHelperExecutablePath: () => null,
    getLoopbackBinding: () => ({ bindHost: '127.0.0.1', bindPort: 8005 }),
    spawnFn: () => {
      const child = children.shift();
      assert.ok(child);
      return child;
    },
    probeReadiness: async (_socketPath, helperInstanceId) => ({
      kind: 'owned',
      status: buildOwnedStatus(helperInstanceId),
    }),
  });

  assert.equal(manager.getLocalApiToken(), null);

  const first = await manager.ensureStarted();
  assert.equal(manager.getLocalApiToken(), `local-api-token-${first.helperInstanceId}`);

  await manager.terminateCurrentHelper();
  assert.equal(manager.getLocalApiToken(), null);

  const second = await manager.ensureStarted();
  assert.notEqual(second.helperInstanceId, first.helperInstanceId);
  assert.equal(manager.getLocalApiToken(), `local-api-token-${second.helperInstanceId}`);
});
