const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');

// Electron adds `process.getSystemVersion`; plain Node, which runs these tests, has none.
let systemVersion = '15.0.0';
process.getSystemVersion = () => systemVersion;

function loadPermissions({ isPackaged = false, accessibilityTrusted = false,
  screenRecording = 'granted', macosVersion = '15.0.0', askError = null } = {}) {
  const calls = [];
  systemVersion = macosVersion;
  const target = require.resolve('../electron/dist/windows/macosPermissions');
  delete require.cache[target];
  const load = Module._load;
  Module._load = function(request, parent, isMain) {
    if (request === 'node:child_process') return {
      execFile: (command, args, options, callback) => {
        calls.push({ osascript: args.at(-1) }); callback(askError, '', '');
      },
    };
    if (request === 'electron') return {
      app: { isPackaged },
      shell: { openExternal: async url => { calls.push(url); } },
      systemPreferences: { isTrustedAccessibilityClient: prompt => {
        calls.push({ ax: prompt }); return accessibilityTrusted;
      }, getMediaAccessStatus: media => { calls.push({ media }); return screenRecording; } },
    };
    return load.call(this, request, parent, isMain);
  };
  try {
    const module = require(target);
    return { read: module.readCapturePermissions,
      ask: module.askForCapturePermission, open: module.openCapturePermissionSettings, calls };
  }
  finally { Module._load = load; delete require.cache[target]; }
}

/** The gate reads a macOS API, so the tests run on Linux CI must claim that platform. */
function onPlatform(platform, body) {
  const original = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: platform, configurable: true });
  try { return body(); } finally { Object.defineProperty(process, 'platform', original); }
}
const onDarwin = body => onPlatform('darwin', body);

test('the capture gate reads each permission live, without prompting', () => {
  const denied = loadPermissions({ accessibilityTrusted: false });
  assert.deepEqual(onDarwin(denied.read), { accessibility: false, screenRecording: true });
  // `false` never prompts: the gate is read on every conversation request, and a
  // system prompt on each of them would be its own denial-of-service.
  assert.deepEqual(denied.calls, [{ ax: false }, { media: 'screen' }]);

  const granted = loadPermissions({ accessibilityTrusted: true });
  assert.deepEqual(onDarwin(granted.read), { accessibility: true, screenRecording: true });

  // Elsewhere there is no recorder, so nothing reads as granted.
  assert.deepEqual(onPlatform('linux', granted.read), { accessibility: false, screenRecording: false });
});

test('the E2E override stands in for TCC only in an unpackaged build', () => {
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '1';
  try {
    const dev = loadPermissions({ accessibilityTrusted: false, screenRecording: 'denied' });
    assert.deepEqual(dev.read(), { accessibility: true, screenRecording: true });
    // The real permission is never consulted, so a CI runner needs no grant.
    assert.deepEqual(dev.calls, []);

    // A shipped app reads macOS whatever its environment claims.
    const packaged = loadPermissions({ isPackaged: true, accessibilityTrusted: false,
      screenRecording: 'denied' });
    assert.deepEqual(onDarwin(packaged.read), { accessibility: false, screenRecording: false });
  } finally {
    delete process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS;
  }
});

const PANE = 'x-apple.systempreferences:com.apple.preference.security?';

// macOS's own popup lists the app and offers the pane; a pane opened here would show it unlisted.
test('asking macOS for a permission it can still ask for opens no pane', async () => {
  const accessibility = loadPermissions();
  await accessibility.ask('read_accessibility_tree');
  assert.deepEqual(accessibility.calls, [{ ax: true }]);

  const input = loadPermissions();
  await input.ask('observe_input');
  assert.equal(input.calls.length, 1);
  assert.match(input.calls[0].osascript, /IOHIDRequestAccess\(1\)/);

  const screen = loadPermissions();
  await screen.ask('capture_screen');
  assert.equal(screen.calls.length, 1);
  assert.match(screen.calls[0].osascript, /CGRequestScreenCaptureAccess\(\)$/);
});

test('denied Automation is asked for through its pane, since macOS no longer asks', async () => {
  for (const permission of ['automate_browser', 'automate_safari']) {
    const f = loadPermissions();
    await f.ask(permission);
    assert.deepEqual(f.calls, [`${PANE}Privacy_Automation`], permission);
  }
});

test('opening a permission\'s settings opens only its own pane', async () => {
  for (const [permission, pane] of [
    ['read_accessibility_tree', 'Privacy_Accessibility'],
    ['observe_input', 'Privacy_ListenEvent'],
    ['capture_screen', 'Privacy_ScreenCapture'],
    ['automate_browser', 'Privacy_Automation'],
    ['automate_safari', 'Privacy_Automation'],
  ]) {
    const f = loadPermissions();
    await f.open(permission);
    assert.deepEqual(f.calls, [`${PANE}${pane}`], permission);
  }
});

// A recording start makes the same ask, and a failed ask must not fail the start.
test('a failed request is logged, not thrown', async (t) => {
  const logged = t.mock.method(console, 'error', () => {});
  const f = loadPermissions({ askError: new Error('osascript failed') });
  await f.ask('capture_screen');
  assert.equal(logged.mock.callCount(), 1);
});

test('Screen Recording reads as granted only when macOS says so, or cannot grant it', () => {
  for (const [status, expected] of [['denied', false], ['not-determined', false], ['granted', true]]) {
    const f = loadPermissions({ accessibilityTrusted: true, screenRecording: status });
    assert.equal(onDarwin(f.read).screenRecording, expected, status);
  }

  // Below macOS 14 the recorder cannot use it, so there is nothing to grant and
  // recording must not wait on it.
  const older = loadPermissions({ accessibilityTrusted: true, screenRecording: 'denied',
    macosVersion: '13.6.1' });
  assert.deepEqual(onDarwin(older.read), { accessibility: true, screenRecording: true });
  assert.deepEqual(older.calls, [{ ax: false }]);
});
