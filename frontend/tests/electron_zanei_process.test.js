const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { test } = require('node:test');
const { createZaneiProcess, ZaneiPermissionRequired } = require('../electron/dist/context/zaneiProcess');

function fixture(t, options = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-zanei-process-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const child = new EventEmitter(); child.pid = 42;
  child.stdin = new PassThrough();
  child.kill = () => { queueMicrotask(() => child.emit('close', 0)); return true; };
  const calls = []; let statusReads = 0; let exitCalls = 0;
  const manifest = { executable_path: '/synthetic/zanei', subject_root: dir,
    keychain_service_prefix: 'service', keychain_label_prefix: 'label', protocol_version: 1 };
  const report = { state: 'running', running: true, paused: false, permissions_ok: true,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', instance: '42@2026-09-07', degraded: {} };
  const manager = createZaneiProcess({ manifest, userId: 'alice', onExit: () => { exitCalls++; },
    requestPermissions: async missing => { calls.push({ permissionRequest: missing }); },
    holdsScreenRecording: () => options.screenRecording ?? true,
    spawnProcess: (binary, args, opts) => { calls.push({ binary, args, opts }); return child; },
    runCommand: (binary, args, opts, callback) => {
      const request = new PassThrough();
      calls.push({ binary, args, opts, input: '' }); const call = calls.at(-1);
      request.on('data', data => { call.input += data.toString(); });
      queueMicrotask(() => {
        if (args.includes('doctor')) {
          callback(options.missing ? Object.assign(new Error('missing permissions'), { code: 3 }) : null,
            JSON.stringify({ capabilities: options.missing ?? { read_accessibility_tree: { required: true, state: 'available' }, observe_input: { required: true, state: 'available' } } }), '');
        } else if (args.includes('status')) {
          statusReads++;
          if (options.stoppedFirst && statusReads === 1) {
            callback(Object.assign(new Error('exit 4'), { code: 4 }), JSON.stringify({ ...report, running: false, state: 'stopped' }), '');
          } else callback(null, JSON.stringify({ ...report, ...options.report }), '');
        } else callback(null, JSON.stringify({ kind: 'page', store_identity: 'stable-store', protocol_version: options.protocol ?? 1 }), '');
      });
      return { stdin: request };
    },
  });
  return { manager, child, calls, exitCalls: () => exitCalls };
}

test('foreground owner reads stopped exit4 then fresh owned status, binds store, and awaits stdin EOF exit', async t => {
  const oldKey = process.env.ZANEI_STORE_KEY_FILE;
  process.env.ZANEI_STORE_KEY_FILE = '/must-not-inherit';
  t.after(() => { if (oldKey === undefined) delete process.env.ZANEI_STORE_KEY_FILE; else process.env.ZANEI_STORE_KEY_FILE = oldKey; });
  const f = fixture(t, { stoppedFirst: true });
  assert.deepEqual(await f.manager.start('[capture]\ntext_content = false\ncontent_snapshot = false\n'),
    { binding: { store_id: 'stable-store', protocol_version: 1 }, permissionsReady: true });
  const spawn = f.calls.find(call => call.args?.includes('start'));
  assert.deepEqual(spawn.args.slice(-3), ['start', '--foreground', '--exit-on-stdin-eof']);
  assert.equal(spawn.opts.env.ZANEI_STORE_KEY_FILE, undefined);
  assert.equal(spawn.opts.env.ZANEI_KEYCHAIN_NO_PROMPT, '1');
  assert.match(spawn.opts.env.ZANEI_KEYCHAIN_SERVICE, /^service\.[a-f0-9]{64}$/);
  const read = f.calls.find(call => call.args.includes('context-read'));
  assert.equal(JSON.parse(read.input).limit, 1);
  let stopped = false; const stop = f.manager.stop().then(() => { stopped = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.child.stdin.writableEnded, true); assert.equal(stopped, false);
  f.child.emit('close', 0); await stop;
  assert.equal(f.exitCalls(), 0);
});

test('partial AX degradation does not prevent a healthy owned recorder from starting', async t => {
  const f = fixture(t, { report: { degraded: { ax: 'AXObserverAddNotification failed' } } });
  await f.manager.start('[capture]\n');
  f.child.emit('close', 1);
  assert.equal(f.exitCalls(), 1);
});

test('incompatible context protocol stops child before rejecting activation input', async t => {
  const f = fixture(t, { protocol: 2 });
  f.child.stdin.on('finish', () => f.child.emit('close', 0));
  await assert.rejects(f.manager.start('[capture]\n'), /incompatible/);
  assert.equal(f.child.stdin.writableEnded, true);
});

test('auto start with missing permissions does not spawn or request OS permissions', async t => {
  const f = fixture(t, { missing: { observe_input: { required: true, state: 'action_required' } } });
  await assert.rejects(f.manager.start('[capture]\n'), ZaneiPermissionRequired);
  assert.ok(!f.calls.some(call => call.args?.includes('start')));
  assert.ok(!f.calls.some(call => call.permissionRequest));
});

test('auto start without Screen Recording neither spawns nor asks, though the recorder treats it as optional', async t => {
  const f = fixture(t, { screenRecording: false });
  const failure = await f.manager.start('[capture]\n').catch(error => error);
  assert.ok(failure instanceof ZaneiPermissionRequired);
  assert.deepEqual(failure.missing, ['capture_screen']);
  assert.ok(!f.calls.some(call => call.args?.includes('start')));
  assert.ok(!f.calls.some(call => call.permissionRequest));
});

test('manual start without Screen Recording asks for it and is not ready while the recorder reports ready', async t => {
  const f = fixture(t, { screenRecording: false });
  const result = await f.manager.start('[capture]\n', true);
  assert.deepEqual(f.calls.find(call => call.permissionRequest).permissionRequest, ['capture_screen']);
  assert.equal(result.permissionsReady, false);
  assert.equal((await f.manager.status()).permissions_ok, false);
  f.child.emit('close', 0);
});

test('manual start requests only required missing capabilities and still checks recorder readiness', async t => {
  const f = fixture(t, { missing: { observe_input: { required: true, state: 'action_required' },
    automate_safari: { required: false, state: 'deferred' } } });
  await f.manager.start('[capture]\n', true);
  assert.deepEqual(f.calls.find(call => call.permissionRequest).permissionRequest, ['observe_input']);
  f.child.emit('close', 0);
});

test('healthy recorder awaiting human permission is retained rather than timed out or stopped', async t => {
  const f = fixture(t, { report: { permissions_ok: false },
    missing: { observe_input: { required: true, state: 'action_required' } } });
  const result = await f.manager.start('[capture]\n', true);
  assert.equal(result.permissionsReady, false);
  assert.equal(f.child.stdin.writableEnded, false);
  const stopped = f.manager.stop(); f.child.emit('close', 0); await stopped;
});

test('deferred browser permission follows native readiness and does not launch an idle browser', async t => {
  const f = fixture(t, { missing: { automate_safari: { required: true, state: 'deferred' } } });
  await f.manager.start('[capture]\n');
  assert.ok(!f.calls.some(call => call.permissionRequest));
  f.child.emit('close', 0);
});

test('pause and resume address the subject store, and a stale pause never outlives its recorder', async t => {
  const f = fixture(t, { report: { paused: true } });
  await f.manager.start('[capture]\n');
  const index = (command) => f.calls.findIndex((call) => call.args?.includes(command));
  // The recorder carries a pause across restarts; the app owns the preference, so a
  // start always begins capturing, before the handshake that binds the store.
  assert.ok(index('resume') >= 0 && index('resume') < index('context-read'));
  await f.manager.pause();
  const paused = f.calls.at(-1);
  assert.deepEqual(paused.args.slice(-1), ['pause']);
  assert.equal(paused.args[0], '--config');
  assert.match(paused.args[3], /store\.sqlite3$/);
  f.child.emit('close', 0);
});

test('a recorder restored for a user with recording off never starts capturing', async t => {
  // The store still carries the pause the previous session left, so the restored
  // recorder must be left alone rather than resumed and paused again.
  const f = fixture(t, { report: { paused: true } });
  await f.manager.start('[capture]\n', false, true);
  assert.deepEqual(f.calls.filter(call => call.args?.at(-1) === 'resume'), []);
  f.child.emit('close', 0);
});

test('a restored recorder whose store lost the pause is taken down, not paused afterwards', async t => {
  // The daemon captures from the moment it runs, so a pause sent now would arrive
  // after it recorded. The failure is raised on the first owned report, before the
  // heartbeat and store health that reads wait for, and stops the recorder.
  const f = fixture(t, { report: { paused: false, heartbeat_freshness: 'stale' } });
  f.child.stdin.on('finish', () => f.child.emit('close', 0));
  await assert.rejects(f.manager.start('[capture]\n', false, true), /without the pause/);
  assert.ok(!f.calls.some(call => call.args?.at(-1) === 'pause'));
  assert.ok(!f.calls.some(call => call.args?.includes('context-read')));
  assert.equal(f.child.stdin.writableEnded, true);
});
