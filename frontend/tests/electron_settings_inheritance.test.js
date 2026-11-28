const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const {
  initializeAccountSettingsScope,
  resolveSettingsScopeDirectory,
  SCOPED_PREFERENCE_FILES,
} = require('../electron/dist/settings/scope.js');
const { loadUiLanguage } = require('../electron/dist/ui/uiLanguage.js');
const { createCapturePrivacyManager } = require('../electron/dist/privacy/capturePrivacy.js');

function profile(t) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-settings-inheritance-'));
  t.after(() => fs.rmSync(userDataDir, { recursive: true, force: true }));
  const source = resolveSettingsScopeDirectory({ userDataDir, userId: null });
  fs.writeFileSync(path.join(source, SCOPED_PREFERENCE_FILES.ui), '{"ui_language":"ja"}');
  fs.writeFileSync(
    path.join(source, SCOPED_PREFERENCE_FILES.recording),
    '{"enabled":false,"recorder_paused":true}'
  );
  fs.writeFileSync(
    path.join(source, SCOPED_PREFERENCE_FILES.meetingTranscription),
    '{"locale":"ja-JP"}'
  );
  const privacy = createCapturePrivacyManager({ userDataDir, resolveAppBundleId: () => null });
  privacy.updateCaptureSettings({
    apps: { mode: 'include_only', entries: [{ name: 'Editor', bundleId: 'dev.editor' }] },
  });
  return { userDataDir, source, destination: path.join(path.dirname(source), 'account-a') };
}

test('first account inherits usable preferences without history, and the two scopes then diverge', (t) => {
  const { userDataDir, source, destination } = profile(t);
  fs.writeFileSync(path.join(source, 'action-read-state.json'), '{"version":1,"entries":[]}');
  fs.writeFileSync(path.join(source, 'unrelated.json'), '{"private":"not a preference"}');
  const guestFiles = Object.fromEntries(
    Object.values(SCOPED_PREFERENCE_FILES).map((name) => [
      name,
      fs.readFileSync(path.join(source, name)),
    ])
  );
  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });

  assert.deepEqual(
    fs.readdirSync(destination).sort(),
    Object.values(SCOPED_PREFERENCE_FILES).sort()
  );
  for (const [name, bytes] of Object.entries(guestFiles)) {
    assert.deepEqual(fs.readFileSync(path.join(destination, name)), bytes);
    assert.deepEqual(fs.readFileSync(path.join(source, name)), bytes);
    assert.equal(fs.statSync(path.join(destination, name)).mode & 0o777, 0o600);
  }
  assert.equal(loadUiLanguage(path.join(destination, SCOPED_PREFERENCE_FILES.ui), 'en-US'), 'ja');
  const accountPrivacy = createCapturePrivacyManager({
    userDataDir,
    initialUserId: 'account-a',
    resolveAppBundleId: () => null,
  });
  assert.deepEqual(accountPrivacy.getCaptureSettings().apps, {
    mode: 'include_only',
    entries: [{ name: 'Editor', bundleId: 'dev.editor' }],
  });

  fs.writeFileSync(path.join(destination, SCOPED_PREFERENCE_FILES.ui), '{"ui_language":"en"}');
  fs.unlinkSync(path.join(destination, SCOPED_PREFERENCE_FILES.recording));
  fs.writeFileSync(path.join(source, SCOPED_PREFERENCE_FILES.recording), '{"enabled":true}');
  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });
  assert.equal(loadUiLanguage(path.join(destination, SCOPED_PREFERENCE_FILES.ui), 'ja-JP'), 'en');
  assert.equal(fs.existsSync(path.join(destination, SCOPED_PREFERENCE_FILES.recording)), false);
  assert.equal(loadUiLanguage(path.join(source, SCOPED_PREFERENCE_FILES.ui), 'en-US'), 'ja');

  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-b' });
  const second = path.join(path.dirname(source), 'account-b');
  assert.equal(loadUiLanguage(path.join(second, SCOPED_PREFERENCE_FILES.ui), 'en-US'), 'ja');
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(second, SCOPED_PREFERENCE_FILES.recording))).enabled,
    true
  );
});

test('an empty guest scope is initialized once without inventing preferences', (t) => {
  const { userDataDir, source, destination } = profile(t);
  for (const name of Object.values(SCOPED_PREFERENCE_FILES)) fs.unlinkSync(path.join(source, name));
  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });
  assert.deepEqual(fs.readdirSync(destination), []);
  fs.writeFileSync(path.join(source, SCOPED_PREFERENCE_FILES.ui), '{"ui_language":"ja"}');
  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });
  assert.deepEqual(fs.readdirSync(destination), []);
});

for (const operation of ['readFileSync', 'writeFileSync', 'fsyncSync', 'renameSync']) {
  test(`a ${operation} failure leaves no partial account scope and can be retried`, (t) => {
    const { userDataDir, source, destination } = profile(t);
    const before = Object.fromEntries(
      fs.readdirSync(source).map((name) => [name, fs.readFileSync(path.join(source, name))])
    );
    const original = fs[operation];
    const failure = Object.assign(new Error('Settings storage unavailable.'), { code: 'EIO' });
    const injected = t.mock.method(fs, operation, (...args) => {
      // Fail after the UI preference has been staged when testing file reads/writes.
      if (
        operation === 'fsyncSync'
          ? fs.fstatSync(args[0]).isDirectory()
          : operation === 'renameSync' ||
            String(args[0]).endsWith(SCOPED_PREFERENCE_FILES.capturePrivacy)
      )
        throw failure;
      return original(...args);
    });
    assert.throws(
      () => initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' }),
      (error) => error === failure
    );
    injected.mock.restore();
    assert.equal(fs.existsSync(destination), false);
    assert.deepEqual(fs.readdirSync(path.dirname(source)), ['__logged_out__']);
    for (const [name, bytes] of Object.entries(before))
      assert.deepEqual(fs.readFileSync(path.join(source, name)), bytes);
    initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });
    for (const [name, bytes] of Object.entries(before))
      assert.deepEqual(fs.readFileSync(path.join(destination, name)), bytes);
  });
}

test('an existing account is not touched even when the guest preferences cannot be read', (t) => {
  const { userDataDir, destination } = profile(t);
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, SCOPED_PREFERENCE_FILES.ui), '{"ui_language":"en"}');
  const injected = t.mock.method(fs, 'readFileSync', () => {
    throw new Error('Guest read is forbidden.');
  });
  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });
  injected.mock.restore();
  assert.equal(loadUiLanguage(path.join(destination, SCOPED_PREFERENCE_FILES.ui), 'ja-JP'), 'en');
});

test('a restart completes an interrupted copy without leaving stale preference copies', (t) => {
  const { userDataDir, source, destination } = profile(t);
  const child = spawnSync(
    process.execPath,
    [
      '-e',
      `
    const fs = require('node:fs');
    const { initializeAccountSettingsScope } = require(process.argv[1]);
    fs.renameSync = () => process.exit(17);
    initializeAccountSettingsScope({ userDataDir: process.argv[2], accountUserId: 'account-a' });
  `,
      require.resolve('../electron/dist/settings/scope.js'),
      userDataDir,
    ],
    { encoding: 'utf8' }
  );
  assert.equal(child.status, 17, child.stderr);
  assert.equal(fs.existsSync(destination), false);
  initializeAccountSettingsScope({ userDataDir, accountUserId: 'account-a' });
  assert.deepEqual(fs.readdirSync(path.dirname(source)).sort(), ['__logged_out__', 'account-a']);
  assert.equal(loadUiLanguage(path.join(destination, SCOPED_PREFERENCE_FILES.ui), 'en-US'), 'ja');
});
