const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { test } = require('node:test');
const { createScreenshotSyncManager } = require('../electron/dist/screenshot/screenshotSync');
const { createCapturePrivacyManager } = require('../electron/dist/privacy/capturePrivacy');
const { zaneiConfig, zaneiSubjectPaths } = require('../electron/dist/context/zaneiConfig');
const {
  ALWAYS_DENIED_APP_NAMES, ALWAYS_DENIED_BUNDLE_IDS,
} = require('../electron/dist/privacy/alwaysDeniedCaptureApps');
const {
  initializeAccountSettingsScope, resolveScopedSettingsPath,
} = require('../electron/dist/settings/scope');

function fixture(t, overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-zanei-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const executable = path.join(dir, 'binary'); fs.writeFileSync(executable, 'synthetic');
  const manifest = { executable_path: executable,
    executable_sha256: createHash('sha256').update('synthetic').digest('hex'),
    protocol_version: 1, subject_root: dir,
    keychain_service_prefix: 'service', keychain_label_prefix: 'label' };
  const manifestPath = path.join(dir, 'manifest.json');
  fs.writeFileSync(manifestPath, JSON.stringify({ zanei: manifest }));
  const privacy = createCapturePrivacyManager({
    userDataDir: dir, initialUserId: 'alice', resolveAppBundleId: () => null });
  let state = { kind: 'stopped', epoch: 'initial', policy_revision: 'old', reason: 'disabled' };
  const calls = [];
  const transitions = [];
  let report = { state: 'running', running: true, paused: false, permissions_ok: true,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {}, last_event_ts: null };
  const starts = [];
  // Stands in for a backend that answers slowly: every request waits on it.
  let backendGate = null;
  const process = { start: async (_config, _manual, startPaused = false) => {
      calls.push('start'); starts.push(startPaused);
      return { binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: true }; },
    stop: async () => { calls.push('stop'); }, status: async () => report,
    pause: async () => { calls.push('pause'); report = { ...report, paused: true }; },
    resume: async () => { calls.push('resume'); report = { ...report, paused: false }; } };
  const manager = createScreenshotSyncManager({
    isMac: true, userDataDir: dir, getMainWindow: () => null,
    isBackendRuntimeReady: () => true,
    capturePrivacy: privacy,
    getManifestPath: () => manifestPath,
    readSource: async () => { await backendGate; return state; },
    transitionSource: async (user, request) => {
      await backendGate;
      calls.push(request.kind);
      transitions.push(request);
      if (request.kind === 'set_capture_paused') {
        if (state.kind !== 'ready') {
          return { kind: 'blocked', state: { kind: 'blocked', epoch: request.expected_epoch,
            policy_revision: 'policy', reason: 'recorder_unavailable' } };
        }
        state = { ...state, capture_paused: request.paused };
        return { kind: 'applied', state };
      }
      state = request.kind === 'suspend'
        ? { kind: 'stopped', epoch: 'issued', policy_revision: request.policy_revision, reason: request.reason }
        // Activation carries the capture state, exactly as the backend applies it.
        : { kind: 'ready', capture_paused: request.capture_paused,
          binding: { ...request.recorder_binding, user_id: user,
            epoch: request.issued_epoch, policy_revision: 'policy' } };
      return { kind: 'applied', state };
    }, containSource: async () => { calls.push('contain'); },
    createProcess: () => process, ...overrides,
  });
  manager.setOwner({ id: 'alice', kind: 'account' });
  const settingsPath = resolveScopedSettingsPath({
    userDataDir: dir, userId: 'alice', fileName: 'screenshot-settings.json' });
  const enabled = () => JSON.parse(fs.readFileSync(settingsPath, 'utf8')).enabled;
  const recorderPaused = () => JSON.parse(fs.readFileSync(settingsPath, 'utf8')).recorder_paused;
  // The preference file is written only once recording actually ran.
  const stored = () => fs.existsSync(settingsPath);
  return { manager, privacy, calls, transitions, process, enabled, recorderPaused, stored,
    manifest, starts, settingsPath, dir,
    source: () => state,
    holdBackend: promise => { backendGate = promise; },
    setSource: value => { state = value; },
    // Stands in for a preference a previous run of the app left behind.
    setStored: value => fs.writeFileSync(settingsPath,
      JSON.stringify({ enabled: value, recorder_paused: !value })),
    // The release where turning recording off stopped the recorder wrote no pause.
    setLegacyStored: () => fs.writeFileSync(settingsPath, JSON.stringify({ enabled: false })),
    setReport: value => { report = value; } };
}

test('start publishes only after actual collector readiness, and stop preserves ordering', async t => {
  const f = fixture(t);
  await f.manager.restoreRecorder(); assert.deepEqual(f.calls, []);
  assert.equal(await f.manager.start(), 'started');
  assert.deepEqual(f.calls, ['suspend', 'start', 'activate']);
  assert.equal(f.enabled(), true);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'capturing');
  await f.manager.stop();
  // Turning recording off pauses the recorder: the permit and the recorded data stay.
  assert.deepEqual(f.calls.slice(-2), ['pause', 'set_capture_paused']);
  assert.equal(f.enabled(), false);
  assert.equal(f.manager.getStatus(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'paused');
});

test('turning recording off stops capture before waiting on any backend request', async t => {
  const f = fixture(t);
  await f.manager.start();
  f.calls.length = 0;
  let release;
  // A backend request can take ten seconds; capture must not run for any of it.
  f.holdBackend(new Promise(resolve => { release = resolve; }));

  const stopping = f.manager.stop();
  await new Promise(resolve => setImmediate(resolve));

  assert.deepEqual(f.calls, ['pause']);
  assert.equal(f.manager.getStatus(), false);
  release();
  await stopping;
  assert.deepEqual(f.calls, ['pause', 'set_capture_paused']);
  assert.equal(f.source().capture_paused, true);
  assert.equal(f.enabled(), false);
});

test('a recorder that cannot be paused is taken down before the backend is told', async t => {
  const f = fixture(t);
  await f.manager.start();
  f.process.pause = async () => { f.calls.push('pause'); throw new Error('pause failed'); };
  f.calls.length = 0;
  let release;
  f.holdBackend(new Promise(resolve => { release = resolve; }));

  const stopping = assert.rejects(f.manager.stop(), /pause failed/);
  await new Promise(resolve => setImmediate(resolve));

  // The fallback stop ends capture first; only then does it publish the suspend.
  assert.deepEqual(f.calls, ['pause', 'stop']);
  release();
  await stopping;
  assert.deepEqual(f.calls, ['pause', 'stop', 'suspend']);
  assert.equal(f.manager.getStatus(), false);
  assert.equal(f.enabled(), false);
});

test('recording turned back on resumes the same recorder without a new source generation', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  f.calls.length = 0;
  assert.equal(await f.manager.start(), 'started');
  assert.deepEqual(f.calls, ['set_capture_paused', 'resume']);
  assert.equal(f.enabled(), true);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'capturing');
});

test('a recorder that cannot be paused is stopped, so recording never outlives the toggle', async t => {
  const f = fixture(t);
  await f.manager.start();
  f.process.pause = async () => { throw new Error('pause failed'); };
  await assert.rejects(f.manager.stop(), /pause failed/);
  assert.deepEqual(f.calls.slice(-2), ['stop', 'suspend']);
  assert.equal(f.manager.getStatus(), false);
  // The failure is reported rather than hidden behind the paused copy.
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'unavailable');

  // The stop is what this run left behind, so the preference has to say so: a
  // preference still reading as on would restore a capturing recorder next launch.
  assert.equal(f.enabled(), false);
  assert.equal(f.recorderPaused(), false);
  // A later launch decides from that preference alone; scoping the user again is
  // what clears the failure this run reported.
  f.manager.setOwner({ id: 'alice', kind: 'account' });
  f.calls.length = 0;
  await f.manager.restoreRecorder();
  assert.deepEqual(f.calls, []);
  assert.equal(f.manager.getStatus(), false);
});

test('a suspend while recording is off tells the backend recording is off', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  f.calls.length = 0;

  await f.manager.pause('shutdown');

  assert.deepEqual(f.calls, ['stop', 'suspend']);
  // This row outlives the app: the next launch reads it before any preference is
  // restored, and only `disabled` says the user turned recording off. `shutdown`
  // would let a Suggestion job queued before the toggle run on a launch where
  // recording is still off.
  assert.equal(f.source().reason, 'disabled');
});

test('a suspend while recording is on keeps the reason that triggered it', async t => {
  const f = fixture(t);
  await f.manager.start();

  await f.manager.pause('shutdown');

  assert.equal(f.source().reason, 'shutdown');
});

test('an off that could not be saved keeps saying so while the recorder stays paused', async t => {
  const f = fixture(t);
  await f.manager.start();
  const realWriteFileSync = fs.writeFileSync;
  t.after(() => { fs.writeFileSync = realWriteFileSync; });
  fs.writeFileSync = (target, ...rest) => {
    if (target === f.settingsPath) throw new Error('disk full');
    return realWriteFileSync(target, ...rest);
  };

  await assert.rejects(f.manager.stop(), /disk full/);

  // The recorder paused cleanly, so its report clears the transient failure and
  // nothing else would mention the save. The preference still reads as on, which
  // is what the toggle reports, so the status has to say the off was not stored.
  const snapshot = await f.manager.getCaptureStatusSnapshot();
  assert.equal(snapshot.kind, 'unavailable');
  assert.equal(snapshot.reasonLabel, 'capture_preference_write_failed');
  assert.equal(f.manager.getStatus(), true);

  // Turning recording back on leaves nothing unsaved, so the warning goes.
  assert.equal(await f.manager.start(), 'started');
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'capturing');
});

test('a resume that fails puts the published capture state back to paused', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  f.process.resume = async () => { throw new Error('resume failed'); };
  f.calls.length = 0;

  await assert.rejects(f.manager.start(), /resume failed/);

  // Without the rollback the source would say capture is on while the recorder
  // stays paused, and the tail recorded before the pause could still be summarized.
  assert.deepEqual(f.calls, ['set_capture_paused', 'pause', 'set_capture_paused']);
  assert.equal(f.source().capture_paused, true);
  assert.equal(f.manager.getStatus(), false);
  assert.equal(f.enabled(), false);
});

test('a resume with the capture permission revoked restarts instead of reporting started', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  // macOS revoked the permission while capture was paused: the recorder is up
  // and holds its permit, but resuming it would capture nothing.
  f.setReport({ state: 'running', running: true, paused: true, permissions_ok: false,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {}, last_event_ts: null });
  f.process.start = async (_config, _manual, startPaused = false) => {
    f.calls.push('start'); f.starts.push(startPaused);
    return { binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: false };
  };
  f.calls.length = 0;

  // Resuming in place would report the toggle as on until the next status poll.
  assert.equal(await f.manager.start(), 'permission_pending');

  assert.deepEqual(f.calls, ['stop', 'suspend', 'suspend', 'start']);
  // Nothing was recorded, so the user's "on" is not stored either.
  assert.equal(f.enabled(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'permission_required');
});

test('a filter change while paused restarts the recorder and pauses it again', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  f.calls.length = 0; f.starts.length = 0;
  await f.manager.updatePrivacy(() => f.privacy.updateCaptureSettings({ ...f.privacy.getCaptureSettings(), websites: { mode: 'exclude', hosts: ['example.com'] } }));
  assert.deepEqual(f.calls, ['stop', 'suspend', 'suspend', 'start', 'activate']);
  // The new recorder is started paused, so it never captures between the spawn
  // and the activation that publishes that pause.
  assert.deepEqual(f.starts, [true]);
  assert.equal(f.source().capture_paused, true);
  // The restart must not read as the user turning recording back on.
  assert.equal(f.enabled(), false);
  assert.equal(f.manager.getStatus(), false);
});

test('editing filters while recording is off brings the paused recorder back afterwards', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  // Opening the filter editor takes the recorder down for the whole edit.
  await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'editor' });
  assert.equal(f.source().kind, 'stopped');
  f.calls.length = 0; f.starts.length = 0;

  await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'editor' });

  // The stored preference, not the transient pause flag, decides what comes back.
  assert.deepEqual(f.calls, ['suspend', 'start', 'activate']);
  assert.deepEqual(f.starts, [true]);
  assert.equal(f.source().capture_paused, true);
  assert.equal(f.enabled(), false);
  assert.equal(f.manager.getStatus(), false);
});

test('a source that lost its permit is fully restarted instead of pause-toggled', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  // The local backend restarted under this recorder: it keeps the epoch but no
  // longer holds the permit, so it reports the source as stopped.
  f.setSource({ kind: 'stopped', epoch: 'issued', policy_revision: 'policy', reason: 'shutdown' });
  f.calls.length = 0;

  assert.equal(await f.manager.start(), 'started');

  // A pause transition cannot hand the permit back, so only a full activation can.
  assert.deepEqual(f.calls, ['stop', 'suspend', 'suspend', 'start', 'activate']);
  assert.equal(f.source().kind, 'ready');
  assert.equal(f.enabled(), true);
  assert.equal(f.manager.getStatus(), true);
});

test('turning recording off without a permit stops the recorder outright', async t => {
  const f = fixture(t);
  await f.manager.start();
  f.setSource({ kind: 'stopped', epoch: 'issued', policy_revision: 'policy', reason: 'shutdown' });
  f.calls.length = 0;

  await f.manager.stop();

  // Capture stops locally first, so nothing is recorded while the backend read
  // that reveals the lost permit is in flight.
  assert.deepEqual(f.calls, ['pause', 'stop', 'suspend']);
  assert.equal(f.enabled(), false);
  assert.equal(f.manager.getStatus(), false);
  // The recorder was stopped, not paused, so its store carries no pause and the
  // preference must not invite the next launch to restore a paused recorder.
  assert.equal(f.recorderPaused(), false);
  f.calls.length = 0; f.starts.length = 0;
  await f.manager.restoreRecorder();
  assert.deepEqual(f.calls, []);
});

test('editing waits for stop, preserves enabled preference, and resumes using changed policy', async t => {
  const f = fixture(t); await f.manager.start();
  let release; f.process.stop = () => new Promise(resolve => { release = resolve; });
  let granted = false;
  const edit = f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'editor' }).then(result => { granted = result; });
  await new Promise(resolve => setImmediate(resolve));
  // The recorder is taken down before the suspend is published, so nothing has
  // reached the backend while its stop is still in flight.
  assert.equal(granted, false); assert.equal(f.calls.at(-1), 'activate');
  release(); await edit;
  assert.equal(f.enabled(), true);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'editing_paused');
  f.process.stop = async () => {};
  await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'editor' });
  assert.equal(f.calls.at(-1), 'activate');
});

test('restoreRecorder brings the recorder back capturing from a preference that is on', async t => {
  const f = fixture(t);
  // A user who never turned recording on has no preference and gets no recorder.
  await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), false);
  await f.manager.start();
  await f.manager.pause('signed_out');
  assert.equal(f.enabled(), true);
  await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), true);
});

test('a preference that is off still restores the recorder, paused, so reads keep working', async t => {
  const f = fixture(t);
  // The previous run of the app left recording turned off.
  f.setStored(false);

  await f.manager.restoreRecorder();

  // One transition makes the source ready, and it already carries the pause: a
  // ready-and-capturing state published first would let a Suggestion job queued
  // for this user run while the saved preference says recording is off.
  assert.deepEqual(f.calls, ['suspend', 'start', 'activate']);
  assert.equal(f.transitions.at(-1).capture_paused, true);
  // Started paused, so the restored recorder captures nothing at any point.
  assert.deepEqual(f.starts, [true]);
  // The source is ready with a live permit, which is what lets the zanei tools
  // read the activity the store still holds; only capture is off.
  assert.deepEqual(f.source(), { kind: 'ready', capture_paused: true,
    binding: { store_id: 'store', protocol_version: 1, user_id: 'alice',
      epoch: 'issued', policy_revision: 'policy' } });
  // Restoring the recorder is not the user turning recording back on.
  assert.equal(f.enabled(), false);
  assert.equal(f.manager.getStatus(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'paused');
});

test('a preference from the release that stopped the recorder brings nothing back', async t => {
  const f = fixture(t);
  // Nothing left a pause in that recorder's store, so restoring it would record
  // until a pause reached the running daemon.
  f.setLegacyStored();

  await f.manager.restoreRecorder();

  assert.deepEqual(f.calls, []);
  assert.deepEqual(f.starts, []);
  assert.equal(f.manager.getStatus(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'paused');

  // Turning recording on and off again leaves the pause the next restore needs.
  await f.manager.start(); await f.manager.stop();
  assert.equal(f.recorderPaused(), true);
  await f.manager.pause('signed_out');
  f.calls.length = 0; f.starts.length = 0;
  await f.manager.restoreRecorder();
  assert.deepEqual(f.starts, [true]);
  assert.equal(f.manager.getStatus(), false);
});

test('a restored recorder still waiting for macOS permission never reads as recording', async t => {
  const f = fixture(t);
  f.setStored(false);
  f.process.start = async () => { f.calls.push('start'); return {
    binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: false,
  }; };
  // The recorder is started paused, so its report says so from the first poll.
  f.setReport({ running: true, permissions_ok: false, paused: true,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {} });

  await f.manager.restoreRecorder();

  // Permission is missing, so the source is never activated and the gate keeps
  // the overlay closed, exactly as for a manual start.
  assert.deepEqual(f.calls, ['suspend', 'start']);
  assert.equal(f.source().kind, 'stopped');
  assert.equal(f.manager.getStatus(), false);
  assert.equal(f.enabled(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'permission_required');

  // The user grants the permission. The deliberate pause must not stop the
  // activation the reads depend on.
  f.setReport({ running: true, permissions_ok: true, paused: true,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {} });
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'paused');
  assert.equal(f.source().kind, 'ready');
  assert.equal(f.source().capture_paused, true);
  assert.equal(f.enabled(), false);
  assert.equal(f.manager.getStatus(), false);
});

test('subject pause blocks a queued manual start without changing saved preference', async t => {
  const f = fixture(t); await f.manager.start();
  const stopping = f.manager.pause('signed_out');
  const starting = f.manager.start();
  await stopping; assert.equal(await starting, 'failed'); assert.equal(f.enabled(), true);
  f.manager.setOwner({ id: 'guest', kind: 'guest' }); await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), false);
});

test('source revocation failure still stops collector and uses backend containment', async t => {
  let fail = false;
  const f = fixture(t, { readSource: async () => {
    if (fail) throw new Error('auth expired');
    return { kind: 'stopped', epoch: 'e', policy_revision: 'p' };
  } });
  await f.manager.start(); fail = true;
  await f.manager.pause('signed_out');
  assert.deepEqual(f.calls.slice(-2), ['stop', 'contain']);
  assert.equal(f.manager.getStatus(), false);
});

test('failed activation revokes an uncertain permit and stops the producer', async t => {
  const calls = [];
  const f = fixture(t, { transitionSource: async (_user, request) => {
    calls.push(request.kind);
    if (request.kind === 'activate') throw new Error('response lost');
    return { kind: 'applied', state: { kind: 'stopped', epoch: 'e', policy_revision: 'p' } };
  } });
  await assert.rejects(f.manager.start(), /response lost/);
  assert.deepEqual(calls, ['suspend', 'activate', 'suspend']);
  assert.equal(f.calls.at(-1), 'stop'); assert.equal(f.manager.getStatus(), false);
});

test('status cannot claim capture when current collector permissions or store health fail', async t => {
  const f = fixture(t); await f.manager.start();
  f.setReport({ running: true, permissions_ok: false });
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'permission_required');
  f.setReport({ running: true, permissions_ok: true, heartbeat_freshness: 'stale' });
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'unavailable');
});

test('policy conversion renders each filter mode and leaves body scopes at their defaults', t => {
  const f = fixture(t);
  const base = f.privacy.getCaptureSettings();

  const excluding = zaneiConfig({ ...base,
    apps: { mode: 'exclude', entries: [
      { name: 'Password Vault', bundleId: 'com.example."vault' },
      { name: 'Legacy App', bundleId: null }] },
    websites: { mode: 'exclude', hosts: ['private.example.com'] } });
  assert.match(excluding, /\nexclude_apps = \[.*"com\.example\.\\"vault", "Legacy App"\]\ninclude_only_apps = \[\]\n/);
  assert.match(excluding, /mode = "all_sites"\ndefault_policy = "allow"\non_url_unavailable = "allow"\n/);
  assert.match(excluding, /allow_list = \[\]\nblock_list = \[\{ host = "private\.example\.com", path_prefix = "", match_subdomains = true \}\]\n/);
  // The recorder's own defaults keep browser bodies out; overriding them here would not.
  assert.ok(!excluding.includes('[filter.text_content]'));
  assert.ok(!excluding.includes('[filter.content_snapshot]'));
  // App selection is the [filter] lists; a pinned allow list would add a second gate.
  assert.ok(!excluding.includes('allowed_apps'));
  assert.match(excluding, /retention_hours = 48/);
  assert.match(excluding, /text_content = true/);
  assert.match(excluding, /content_snapshot = true/);
  assert.match(excluding, /block_auth = true\nblock_payments = true\n/);
  assert.match(excluding, /block_env_files = true\non_file_name_unavailable = "allow"\n/);

  const includingOnly = zaneiConfig({ ...base,
    apps: { mode: 'include_only', entries: [{ name: 'Notes', bundleId: 'com.apple.Notes' }] },
    websites: { mode: 'include_only', hosts: ['github.com'] } });
  assert.match(includingOnly, /\ninclude_only_apps = \["com\.apple\.Notes"\]\n/);
  assert.match(includingOnly, /mode = "rules"\ndefault_policy = "block"\non_url_unavailable = "block"\n/);
  assert.match(includingOnly, /allow_list = \[\{ host = "github\.com", path_prefix = "", match_subdomains = true \}\]\nblock_list = \[\]\n/);

  const suffix = createHash('sha256').update('alice').digest('hex');
  const paths = zaneiSubjectPaths(f.manifest, 'alice');
  assert.equal(paths.store, path.join(f.manifest.subject_root, 'subjects', suffix, 'store.sqlite3'));
  assert.equal(paths.service, `service.${suffix}`);
});

test('an empty "only these apps" list records no app, unlike an empty exclusion list', t => {
  const f = fixture(t);
  const base = f.privacy.getCaptureSettings();
  // The recorder reads an empty include_only_apps as no restriction; only an explicit
  // empty allowed_apps denies every app.
  const recordsNothing = zaneiConfig({ ...base, apps: { mode: 'include_only', entries: [] } });
  assert.match(recordsNothing, /\[filter\.capture_policy\]\nallowed_apps = \[\]\n\[filter\.capture_policy\.browser\]\n/);
  const recordsEverything = zaneiConfig({ ...base, apps: { mode: 'exclude', entries: [] } });
  assert.ok(!recordsEverything.includes('allowed_apps'));

  // Settings that cannot be read fall back to recording nothing through the same path.
  const settingsPath = resolveScopedSettingsPath({
    userDataDir: f.dir, userId: 'bob', fileName: 'capture-privacy-settings.json' });
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  fs.writeFileSync(settingsPath, '{ this is not json');
  const unreadable = createCapturePrivacyManager({
    userDataDir: f.dir, initialUserId: 'bob', resolveAppBundleId: () => null });
  assert.match(zaneiConfig(unreadable.getCaptureSettings()), /\nallowed_apps = \[\]\n/);
});

/**
 * The recorder's own built-in exclusions cover 1Password and Keychain Access only, so
 * every other password manager stays out of the store solely because the configuration
 * lists it. `excludedApps` and `recorderSkips` mirror the recorder's documented
 * matching rule (Zanei ARCHITECTURE.md 11.3): an app is keyed by its `bundle_id` when
 * it reports one and by its display name otherwise, and that one key is compared
 * case-insensitively against `exclude_apps`, after `include_only_apps` is consulted.
 */
const excludedApps = config =>
  JSON.parse(/\nexclude_apps = (\[[^\n]*\])\n/.exec(config)[1]);

const recorderSkips = (config, app) => {
  const key = (app.bundleId ?? app.name).toLowerCase();
  return excludedApps(config).some(entry => entry.toLowerCase() === key);
};

test('password managers are excluded from recording whatever the user filter says', t => {
  const f = fixture(t);
  const base = f.privacy.getCaptureSettings();

  // A user who has set no filter at all is still not recorded inside a vault.
  const untouched = zaneiConfig(base);
  for (const bundleId of ALWAYS_DENIED_BUNDLE_IDS) {
    assert.ok(recorderSkips(untouched, { name: 'Vault', bundleId }), bundleId);
  }
  // A password manager reporting no bundle identifier is keyed by its display name.
  for (const name of ALWAYS_DENIED_APP_NAMES) {
    assert.ok(recorderSkips(untouched, { name, bundleId: null }), name);
  }
  // Case is not significant to the recorder, and the real display names are not lowercase.
  assert.ok(recorderSkips(untouched, { name: 'KeePassXC', bundleId: null }));
  assert.ok(recorderSkips(untouched, { name: 'Passwords', bundleId: 'com.apple.passwords' }));
  assert.ok(!recorderSkips(untouched, { name: 'Notes', bundleId: 'com.apple.Notes' }));

  // Only mode: the recorder reads exclude_apps after include_only_apps, so naming a
  // password manager as one of the recorded apps still does not record it.
  const allowed = zaneiConfig({ ...base, apps: { mode: 'include_only', entries: [
    { name: 'Notes', bundleId: 'com.apple.Notes' },
    { name: 'Bitwarden', bundleId: 'com.bitwarden.desktop' }] } });
  assert.match(allowed, /include_only_apps = \["com\.apple\.Notes", "com\.bitwarden\.desktop"\]/);
  assert.ok(recorderSkips(allowed, { name: 'Bitwarden', bundleId: 'com.bitwarden.desktop' }));
  assert.ok(!recorderSkips(allowed, { name: 'Notes', bundleId: 'com.apple.Notes' }));

  // Except mode: the user's own entries survive, and an entry that duplicates the
  // always-denied list is not written twice.
  const excluding = zaneiConfig({ ...base, apps: { mode: 'exclude', entries: [
    { name: 'Slack', bundleId: 'com.tinyspeck.slackmacgap' },
    { name: 'Bitwarden', bundleId: 'com.bitwarden.desktop' }] } });
  const entries = excludedApps(excluding);
  assert.ok(entries.includes('com.tinyspeck.slackmacgap'));
  assert.equal(entries.filter(entry => entry === 'com.bitwarden.desktop').length, 1);
  assert.equal(new Set(entries).size, entries.length);
});

test('partial AX failure keeps the recorder available and displays incomplete observations', async t => {
  const f = fixture(t); await f.manager.start();
  f.setReport({ state: 'running', running: true, paused: false, permissions_ok: true,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy',
    degraded: { ax: 'AXObserverAddNotification failed' }, last_event_ts: null });
  const snapshot = await f.manager.getCaptureStatusSnapshot();
  assert.equal(snapshot.kind, 'degraded'); assert.equal(snapshot.screenshotsEnabled, true);
  assert.equal(snapshot.reasonLabel, 'ax'); assert.equal(f.calls.at(-1), 'activate');
});

test('permission pending keeps native requests alive and activates same collector after permission grant', async t => {
  const notifications = [];
  const f = fixture(t, { getMainWindow: () => ({ isDestroyed: () => false, webContents: { send: (_channel, enabled) => notifications.push(enabled) } }) });
  f.process.start = async () => { f.calls.push('start'); return {
    binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: false,
  }; };
  f.setReport({ running: true, permissions_ok: false, paused: false,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {} });
  // Permission is still missing, so nothing has been recorded yet.
  assert.equal(await f.manager.start(), 'permission_pending');
  assert.deepEqual(f.calls, ['suspend', 'start']);
  assert.equal(notifications.at(-1), true);
  // Nothing has been recorded, so nothing durable may read as a past grant.
  assert.equal(f.stored(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'permission_required');
  f.setReport({ running: true, permissions_ok: true, paused: false,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {} });
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'capturing');
  assert.deepEqual(f.calls, ['suspend', 'start', 'activate']);
  // Recording is running now, so the preference the next launch resumes from exists.
  assert.equal(f.enabled(), true);
});

test('a start left waiting for permission stores nothing until a later start records', async t => {
  const f = fixture(t);
  f.process.start = async () => { f.calls.push('start'); return {
    binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: false,
  }; };
  assert.equal(await f.manager.start(), 'permission_pending');
  assert.equal(f.stored(), false);
  // Turning it back off before anything ran must not leave a file either.
  await f.manager.stop();
  assert.equal(f.stored(), false);
  // The user grants the macOS permission and starts again.
  f.process.start = async () => { f.calls.push('start'); return {
    binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: true,
  }; };
  assert.equal(await f.manager.start(), 'started');
  assert.equal(f.stored(), true);
  assert.equal(f.enabled(), true);
});

test('a start is reported once it actually runs, and once per owner in a run', async t => {
  const starts = [];
  const f = fixture(t, { onRecordingStarted: userId => starts.push(userId) });
  f.process.start = async () => { f.calls.push('start'); return {
    binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: false,
  }; };
  // Waiting for macOS permission records nothing yet.
  assert.equal(await f.manager.start(), 'permission_pending');
  assert.deepEqual(starts, []);
  f.setReport({ running: true, permissions_ok: true, paused: false,
    heartbeat_freshness: 'fresh', store_write_state: 'healthy', degraded: {} });
  await f.manager.getCaptureStatusSnapshot();
  assert.deepEqual(starts, ['alice']);
  f.process.start = async () => { f.calls.push('start'); return {
    binding: { store_id: 'store', protocol_version: 1 }, permissionsReady: true,
  }; };
  // Turning it off and on, and a restart that restores it, ask the runtime nothing new.
  await f.manager.stop();
  assert.equal(await f.manager.start(), 'started');
  await f.manager.pause('signed_out');
  await f.manager.restoreRecorder();
  await f.manager.stop();
  await f.manager.pause('shutdown');
  await f.manager.restoreRecorder();
  assert.equal(await f.manager.start(), 'started');
  assert.deepEqual(starts, ['alice']);
});

test('recording restored from a stored "on" is reported, since the runtime decides', async t => {
  // A user who upgrades already has the preference; the runtime sees their data.
  const starts = [];
  const f = fixture(t, { onRecordingStarted: userId => starts.push(userId) });
  f.setStored(true);
  await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), true);
  assert.deepEqual(starts, ['alice']);
});

test('a recorder restored paused is reported only when the user turns capture on', async t => {
  const starts = [];
  const f = fixture(t, { onRecordingStarted: userId => starts.push(userId) });
  f.setStored(false);
  await f.manager.restoreRecorder();
  assert.deepEqual(starts, []);
  // Lifting the pause in place is the moment capture starts.
  assert.equal(await f.manager.start(), 'started');
  assert.deepEqual(starts, ['alice']);
});

test('an account a guest signs in to is reported although it inherited the guest settings', async t => {
  const starts = [];
  const f = fixture(t, { onRecordingStarted: userId => starts.push(userId) });
  f.manager.setOwner({ id: 'guest-1', kind: 'guest' });
  assert.equal(await f.manager.start(), 'started');
  // Signing in copies the guest's settings, recording preference included, to the account.
  await f.manager.pause('signed_out');
  initializeAccountSettingsScope({ userDataDir: f.dir, accountUserId: 'bob' });
  assert.equal(fs.existsSync(resolveScopedSettingsPath({
    userDataDir: f.dir, userId: 'bob', fileName: 'screenshot-settings.json' })), true);
  f.manager.setOwner({ id: 'bob', kind: 'account' });
  await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), true);
  assert.deepEqual(starts, ['guest-1', 'bob']);
});

test('a start hook that fails does not fail the start the user asked for', async t => {
  const f = fixture(t, { onRecordingStarted: () => { throw new Error('greeting failed'); } });
  const logged = [];
  t.mock.method(console, 'error', (...args) => logged.push(args[0]));
  assert.equal(await f.manager.start(), 'started');
  assert.equal(f.manager.getStatus(), true);
  assert.deepEqual(logged, ['Recording start hook failed:']);
});

test('a failed activation stores nothing and reports no start', async t => {
  const starts = [];
  const f = fixture(t, {
    onRecordingStarted: userId => starts.push(userId),
    transitionSource: async (_user, request) => request.kind === 'activate'
      ? { kind: 'conflict', current_epoch: 'other', reason: 'stale_epoch' }
      : { kind: 'applied', state: { kind: 'stopped', epoch: 'issued', policy_revision: 'p', reason: 'disabled' } },
  });
  t.mock.method(console, 'error', () => undefined);
  await assert.rejects(f.manager.start(), /activation conflict/);
  assert.equal(f.stored(), false);
  assert.deepEqual(starts, []);
});

test('disabling while permission pending stops producer without activating source', async t => {
  const f = fixture(t);
  f.process.start = async () => ({ binding: { store_id: 's', protocol_version: 1 }, permissionsReady: false });
  await f.manager.start(); await f.manager.stop();
  assert.ok(!f.calls.includes('activate'));
  assert.deepEqual(f.calls.slice(-2), ['stop', 'suspend']);
  assert.equal(f.stored(), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'paused');
});

test('guest recording uses the confirmed subject while its preference stays in the logged-out scope', async t => {
  const f = fixture(t);
  f.privacy.setSettingsScope(null);
  f.manager.setOwner({ id: 'guest-owner', kind: 'guest' });
  assert.equal(await f.manager.start(), 'started');
  assert.equal(f.source().binding.user_id, 'guest-owner');
  const guestPreferencePath = resolveScopedSettingsPath({
    userDataDir: f.dir, userId: null, fileName: 'screenshot-settings.json',
  });
  assert.equal(JSON.parse(fs.readFileSync(guestPreferencePath, 'utf8')).enabled, true);
  assert.equal(fs.existsSync(path.join(f.dir, 'settings', 'guest-owner', 'screenshot-settings.json')), false);
  await f.manager.stop();
  assert.equal(JSON.parse(fs.readFileSync(guestPreferencePath, 'utf8')).enabled, false);
});

test('a privacy write cancelled by same-owner syncing does not prevent automatic recorder restoration', async t => {
  const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory');
  let ready = true;
  const f = fixture(t, { isBackendRuntimeReady: () => ready });
  const context = buildMainContext({
    getMainWindow: () => null, isDevRuntime: () => false,
    frontendDistIndex: '/tmp/index.html',
    getRuntimeState: () => ({ status: ready ? 'ready' : 'syncing', message: null,
      owner: ready ? { id: 'alice', kind: 'account' } : null }),
    screenshotSync: f.manager, capturePrivacy: f.privacy,
  });
  await f.manager.start();
  const original = f.privacy.getCaptureSettings();
  let release;
  f.holdBackend(new Promise(resolve => { release = resolve; }));
  const updating = context.privacy.updateCaptureSettings({ apps: { mode: 'include_only', entries: [] } });
  const rejected = assert.rejects(updating, /Local owner (is unavailable|changed)/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.manager.getStatus(), false);
  ready = false;
  release();
  await rejected;
  assert.deepEqual(f.privacy.getCaptureSettings(), original);
  // The ready broadcast uses this path without setOwner on same-owner refresh.
  ready = true;
  await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), true);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'capturing');
});

for (const owner of [
  { id: 'alice', kind: 'account' },
  { id: 'guest-installation', kind: 'guest' },
]) {
  test(`ending ${owner.kind} editing during owner synchronization defers restart until ready`, async t => {
    let ready = true;
    const f = fixture(t, { isBackendRuntimeReady: () => ready });
    f.manager.setOwner(owner);
    await f.manager.start();
    assert.equal(await f.manager.setCaptureEditing({ kind: 'begin', ownerId: owner.id, sessionId: 'edit-A' }), true);
    ready = false;
    f.calls.length = 0;
    assert.equal(await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'edit-A' }), true);
    assert.deepEqual(f.calls, []);
    ready = true;
    await f.manager.restoreRecorder();
    assert.equal(f.manager.getStatus(), true);
    assert.ok(f.calls.includes('activate'));
  });
}

test('an old edit cannot release a newer edit for the same owner', async t => {
  const f = fixture(t);
  await f.manager.start();
  await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'old-edit' });
  await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'new-edit' });
  f.calls.length = 0;
  assert.equal(await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'old-edit' }), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'editing_paused');
  assert.deepEqual(f.calls, []);
  await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'new-edit' });
  assert.equal(f.manager.getStatus(), true);
});

test('an edit whose owner becomes unavailable while the recorder stops is not granted', async t => {
  let ready = true;
  const f = fixture(t, { isBackendRuntimeReady: () => ready });
  await f.manager.start();
  let release;
  f.process.stop = () => new Promise(resolve => { release = resolve; });
  const opening = f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'edit' });
  await new Promise(resolve => setImmediate(resolve));
  ready = false;
  release();
  assert.equal(await opening, false);
  ready = true;
  f.process.stop = async () => {};
  await f.manager.restoreRecorder();
  assert.equal(f.manager.getStatus(), true);
});

test('a failed editor restart retains the pause and allows the same end request to retry', async t => {
  const f = fixture(t);
  await f.manager.start();
  await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'edit' });
  const start = f.process.start;
  f.process.start = async () => { throw new Error('restart failed'); };
  await assert.rejects(f.manager.setCaptureEditing({ kind: 'end', sessionId: 'edit' }), /restart failed/);
  assert.equal(f.manager.getStatus(), false);
  f.process.start = start;
  assert.equal(await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'edit' }), true);
  assert.equal(f.manager.getStatus(), true);
});


test('a previous owner cannot begin an edit or release the next owner editor', async t => {
  const f = fixture(t);
  await f.manager.start();
  await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'alice-edit' });
  await f.manager.pause('signed_out');
  f.privacy.setSettingsScope('bob');
  f.manager.setOwner({ id: 'bob', kind: 'account' });
  await f.manager.restoreRecorder();
  await f.manager.start();
  f.calls.length = 0;
  assert.equal(await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'late-alice' }), false);
  assert.equal(f.manager.getStatus(), true);
  assert.deepEqual(f.calls, []);
  await f.manager.setCaptureEditing({ kind: 'begin', ownerId: 'bob', sessionId: 'bob-edit' });
  f.calls.length = 0;
  assert.equal(await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'alice-edit' }), false);
  assert.equal((await f.manager.getCaptureStatusSnapshot()).kind, 'editing_paused');
  assert.deepEqual(f.calls, []);
  await f.manager.setCaptureEditing({ kind: 'end', sessionId: 'bob-edit' });
  assert.equal(f.manager.getStatus(), true);
});

test('a start whose owner changes while the backend answers brings up no recorder', async t => {
  const f = fixture(t);
  let release;
  // 起動前の suspend は読みと書きの二往復。その間に owner が入れ替わる。
  f.holdBackend(new Promise(resolve => { release = resolve; }));

  const starting = f.manager.start();
  await new Promise(resolve => setImmediate(resolve));
  // セッション切れ。id はそのままで kind だけが変わる、別の owner。
  f.manager.setOwner({ id: 'alice', kind: 'guest' });
  release();

  assert.equal(await starting, 'failed');
  assert.deepEqual(f.calls, ['suspend']);
  assert.equal(f.manager.getStatus(), false);
});

test('a start whose owner changes while the paused recorder is checked leaves it paused', async t => {
  const f = fixture(t);
  await f.manager.start(); await f.manager.stop();
  f.calls.length = 0;
  let release;
  // 一時停止からの再開も、recorder の報告と permit の確認で backend を待つ。
  f.holdBackend(new Promise(resolve => { release = resolve; }));

  const starting = f.manager.start();
  await new Promise(resolve => setImmediate(resolve));
  // セッション切れ。id はそのままで kind だけが変わる、別の owner。
  f.manager.setOwner({ id: 'alice', kind: 'guest' });
  release();

  assert.equal(await starting, 'failed');
  // 前の owner の recorder は一時停止のまま。source も「停止中」のまま。
  assert.deepEqual(f.calls, []);
  assert.equal(f.source().capture_paused, true);
  assert.equal(f.manager.getStatus(), false);
  // 「録画オン」は、来た側の保存場所にも書かれない。
  assert.equal(fs.existsSync(resolveScopedSettingsPath({
    userDataDir: f.dir, userId: null, fileName: 'screenshot-settings.json' })), false);
});

test('a start whose owner changes while the recorder comes up takes the recorder back down', async t => {
  const f = fixture(t);
  let release;
  // 権限の確認はユーザーの操作を待つ。開始が終わるのはその後。
  const permissionPrompt = new Promise(resolve => { release = resolve; });
  const startRecorder = f.process.start;
  f.process.start = async (...args) => { await permissionPrompt; return startRecorder(...args); };

  const starting = f.manager.start();
  await new Promise(resolve => setImmediate(resolve));
  f.manager.setOwner({ id: 'bob', kind: 'account' });
  release();

  assert.equal(await starting, 'failed');
  // 起動してしまった recorder は止め、どちらの owner にも activate を出さない。
  assert.deepEqual(f.calls, ['suspend', 'start', 'stop']);
  assert.equal(f.manager.getStatus(), false);
  assert.equal(f.stored(), false);
});
