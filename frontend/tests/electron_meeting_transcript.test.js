const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { test } = require('node:test');
const {
  createMeetingTranscript, MeetingSettingsSchema,
} = require('../electron/dist/context/meetingTranscript');
const { createZaneiProcess } = require('../electron/dist/context/zaneiProcess');

const CALL = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y3Z';
const OTHER = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y40';
const ZOOM = { name: 'zoom.us', bundle_id: 'us.zoom.xos' };
const line = event => `${JSON.stringify(event)}\n`;
const detected = (callId = CALL, expiresInMs = 120_000) => ({ type: 'call_detected', call_id: callId,
  app: ZOOM, site: null, expires_at: new Date(Date.now() + expiresInMs).toISOString() });
const recording = (callId = CALL) =>
  ({ type: 'recording', call_id: callId, app: ZOOM, started_at: '2026-11-28T01:00:00Z' });

function meeting(t, recorder = null) {
  const changes = [];
  const manager = createMeetingTranscript({ getRecorder: () => recorder,
    onChange: call => changes.push(call.kind), settingsPath: () => null,
    getManifestPath: () => assert.fail('settings are not read here') });
  t.after(() => manager.reset());
  const reader = manager.hostEventReader();
  const read = (...events) => reader(events.map(line).join(''));
  return { manager, reader, changes, read, kind: () => manager.getCall().kind };
}

test('stdout lines are read across chunk boundaries, and unreadable ones change nothing', t => {
  const { reader, read, kind } = meeting(t);
  const text = line(detected());
  reader(text.slice(0, 25));
  assert.equal(kind(), 'idle');
  reader(text.slice(25));
  assert.equal(kind(), 'awaiting_consent');
  // A newer recorder's event, a known event that breaks the contract, and noise.
  read({ type: 'call_summarized', call_id: CALL }, { ...recording(), call_id: 'not-a-call-id' });
  reader('{ not json\n\n');
  assert.equal(kind(), 'awaiting_consent');
  // Several events in one chunk apply in order.
  read(recording(), { type: 'recording_stopped', call_id: CALL, reason: 'user_stopped' });
  assert.equal(kind(), 'idle');
});

test('a call goes from consent to recording to ended, and a stale call id is ignored', t => {
  const { read, changes, kind } = meeting(t);
  read(detected(), { type: 'call_closed', call_id: OTHER, reason: 'call_ended' });
  assert.equal(kind(), 'awaiting_consent');
  // The recorder closes no consent for a call it records.
  read(recording(), { type: 'call_closed', call_id: CALL, reason: 'expired' });
  assert.equal(kind(), 'recording');
  read({ type: 'recording_stopped', call_id: CALL, reason: 'call_ended' });
  assert.deepEqual(changes, ['awaiting_consent', 'recording', 'idle']);
});

test('declining or letting the consent expire keeps the call recordable while it lasts', async t => {
  const { manager, read, kind } = meeting(t);
  read(detected());
  manager.decline(CALL);
  assert.deepEqual([kind(), manager.getCall().refused], ['not_recording', null]);
  read({ type: 'call_closed', call_id: CALL, reason: 'expired' });
  assert.equal(kind(), 'not_recording');
  read({ type: 'call_closed', call_id: CALL, reason: 'call_ended' });
  assert.equal(kind(), 'idle');
  // The prompt goes at the deadline even before the recorder's event arrives.
  read(detected(OTHER, 30));
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.deepEqual([kind(), manager.getCall().call_id], ['not_recording', OTHER]);
});

test('a refused record says why, and a recorder restart ends every call it held', t => {
  const { manager, read, kind } = meeting(t);
  read(detected(), { type: 'record_refused', call_id: CALL, reason: 'permission_missing' });
  assert.deepEqual([kind(), manager.getCall().refused], ['not_recording', 'permission_missing']);
  read({ type: 'record_refused', call_id: CALL, reason: 'not_active' });
  assert.equal(kind(), 'idle');
  read(detected()); manager.reset(); assert.equal(kind(), 'idle');
});

test('commands go to the running recorder, and without one nothing is queued', async t => {
  const sent = [];
  const { manager } = meeting(t, { meeting: async command => { sent.push(command); return 'requested'; } });
  assert.deepEqual([await manager.record(CALL), await manager.stop(CALL), await manager.prepare()],
    ['requested', 'requested', 'requested']);
  assert.deepEqual(sent.map(command => command.kind), ['record', 'stop', 'prepare']);
  assert.equal(await meeting(t).manager.record(CALL), 'no_recorder');
});

test('settings refuse entries the recorder would reject the whole configuration for', () => {
  const valid = { callApps: ['us.zoom.xos'], callSites: ['meet.google.com'], locale: 'ja-JP' };
  assert.ok(MeetingSettingsSchema.safeParse(valid).success);
  for (const invalid of [{ callApps: ['Zoom'] }, { callApps: ['us.zoom.xos', 'US.ZOOM.XOS'] },
    { callSites: ['https://meet.google.com'] }, { callSites: ['meet.google.com/abc'] }, { locale: 'ja_JP' }])
    assert.equal(MeetingSettingsSchema.safeParse({ ...valid, ...invalid }).success, false);
});

function recorderProcess(t, { helper = false, exitCode = 0 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-meeting-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const executable = path.join(dir, 'zanei'); fs.writeFileSync(executable, 'synthetic');
  if (helper) fs.writeFileSync(path.join(dir, 'zanei-meeting'), 'helper');
  const [commands, spawns, chunks] = [[], [], []];
  const stdin = { on: () => {}, end: () => {} };
  const child = Object.assign(new EventEmitter(), { pid: 7, stdout: new PassThrough(), stdin, kill: () => {} });
  const answers = { doctor: { capabilities: {} },
    status: { state: 'stopped', running: false, permissions_ok: true } };
  const process = createZaneiProcess({
    manifest: { executable_path: executable, subject_root: dir,
      keychain_service_prefix: 'service', keychain_label_prefix: 'label' },
    userId: 'alice', onExit: () => {}, onHostEvents: chunk => chunks.push(chunk),
    requestPermissions: async () => {}, holdsScreenRecording: () => true,
    spawnProcess: (_file, args, options) => {
      spawns.push({ args, stdio: options.stdio });
      // Writes a host event, then exits before startup is confirmed.
      setImmediate(() => {
        child.stdout.write(line(detected()));
        setImmediate(() => child.emit('close'));
      });
      return child;
    },
    runCommand: (_file, args, _options, callback) => {
      commands.push(args.slice(4));
      const answer = answers[args[4]];
      const error = exitCode ? Object.assign(new Error('exit'), { code: exitCode }) : null;
      setImmediate(() => answer ? callback(null, JSON.stringify(answer)) : callback(error, ''));
      return { stdin };
    },
  });
  return { process, commands, spawns, chunks };
}

test('meeting commands run the recorder CLI, and exit code 4 means no recorder', async t => {
  const { process, commands } = recorderProcess(t);
  for (const command of [{ kind: 'record', call_id: CALL }, { kind: 'stop', call_id: CALL }, { kind: 'prepare' }])
    assert.equal(await process.meeting(command), 'requested');
  assert.deepEqual(commands,
    [['meeting', 'record', CALL], ['meeting', 'stop', CALL], ['meeting', 'prepare']]);
  assert.equal(await recorderProcess(t, { exitCode: 4 }).process.meeting({ kind: 'prepare' }), 'no_recorder');
  // A refused call id (a usage error) is a failure, not a missing recorder.
  await assert.rejects(recorderProcess(t, { exitCode: 2 }).process.meeting({ kind: 'stop', call_id: CALL }));
});

test('a recorder release with the helper starts as the host and its stdout is read', async t => {
  const host = recorderProcess(t, { helper: true });
  await assert.rejects(host.process.start('config', true), /exited during startup/);
  assert.ok(host.spawns[0].args.includes('--host-events'));
  assert.deepEqual(host.spawns[0].stdio, ['pipe', 'pipe', 'ignore']);
  assert.match(host.chunks.join(''), /"call_detected"/);
  // An earlier release knows no `--host-events` and would refuse to start with it.
  const earlier = recorderProcess(t);
  await assert.rejects(earlier.process.start('config', true));
  assert.ok(!earlier.spawns[0].args.includes('--host-events'));
  assert.deepEqual(earlier.spawns[0].stdio, ['pipe', 'ignore', 'ignore']);
});

test('the default call lists are read from the recorder\'s own configuration', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-meeting-defaults-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  // `config show` with an empty file, as the recorder prints it.
  const shown = '[output]\nretention_hours = 48\n\n[meeting]\ncall_apps = [\n    "us.zoom.xos",\n    "com.microsoft.teams2",\n]\ncall_sites = [\n    "meet.google.com",\n]\n';
  const executable = path.join(dir, 'zanei');
  fs.writeFileSync(executable, `#!/bin/sh\n[ "$*" = "--config /dev/null config show" ] || exit 2\nprintf '%s' '${shown}'\n`, { mode: 0o755 });
  const manifestPath = path.join(dir, 'manifest.json');
  fs.writeFileSync(manifestPath, JSON.stringify({ zanei: { executable_path: executable, protocol_version: 1,
    executable_sha256: require('node:crypto').createHash('sha256').update(fs.readFileSync(executable)).digest('hex'),
    subject_root: dir, keychain_service_prefix: 'service', keychain_label_prefix: 'label' } }));
  const manager = createMeetingTranscript({ getRecorder: () => null, onChange: () => {},
    settingsPath: () => null, getManifestPath: () => manifestPath });
  assert.deepEqual(await manager.readDefaultCallLists(), {
    callApps: ['us.zoom.xos', 'com.microsoft.teams2'], callSites: ['meet.google.com'] });
});
