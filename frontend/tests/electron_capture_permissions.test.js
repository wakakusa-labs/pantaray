const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');

// Electron adds `process.getSystemVersion`; plain Node, which runs these tests, has none.
let systemVersion = '15.0.0';
process.getSystemVersion = () => systemVersion;

function loadPermissions({ error = null, isPackaged = false, accessibilityTrusted = false,
  screenRecording = 'granted', macosVersion = '15.0.0' } = {}) {
  const calls = [];
  systemVersion = macosVersion;
  const target = require.resolve('../electron/dist/windows/macosPermissions');
  delete require.cache[target];
  const load = Module._load;
  Module._load = function(request, parent, isMain) {
    if (request === 'electron') return {
      app: { isPackaged },
      shell: { openExternal: async url => { calls.push(url); } },
      systemPreferences: { isTrustedAccessibilityClient: prompt => {
        calls.push({ ax: prompt }); return accessibilityTrusted;
      }, getMediaAccessStatus: media => { calls.push({ media }); return screenRecording; } },
    };
    if (request === 'node:child_process') return {
      execFile: (binary, args, options, callback) => {
        calls.push({ binary, args }); callback(error, 'name', '');
      },
    };
    return load.call(this, request, parent, isMain);
  };
  try {
    const module = require(target);
    return { request: module.requestCapturePermissions,
      holds: module.holdsCaptureOsPermissions,
      openSettings: module.openCapturePermissionSettings, calls };
  }
  finally { Module._load = load; delete require.cache[target]; }
}

const loadPermissionRequester = error => loadPermissions({ error });

/** The gate reads a macOS API, so the tests run on Linux CI must claim that platform. */
function onDarwin(body) {
  const original = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
  try { return body(); } finally { Object.defineProperty(process, 'platform', original); }
}

test('manual permission helper opens AX settings and requests only the required browser AppleEvent', async () => {
  const f = loadPermissionRequester();
  await f.request(['read_accessibility_tree', 'automate_safari']);
  assert.deepEqual(f.calls[0], { binary: '/usr/bin/osascript', args: ['-e', 'tell application id "com.apple.Safari" to get name'] });
  assert.match(f.calls[1], /Privacy_Accessibility$/);
  assert.ok(!JSON.stringify(f.calls).includes('Chrome'));
});

test('Input Monitoring guide opens the native pane without requesting browser access', async () => {
  const f = loadPermissionRequester(); await f.request(['observe_input']);
  assert.deepEqual(f.calls, ['x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent']);
});

test('Automation denial still opens permission settings; unrelated command errors propagate', async () => {
  const denied = Object.assign(new Error('denied'), { stderr: 'Not authorized (-1743)' });
  const f = loadPermissionRequester(denied); await f.request(['automate_browser']);
  assert.match(f.calls.at(-1), /Privacy_Automation$/);
  const bad = loadPermissionRequester(new Error('command unavailable'));
  await assert.rejects(bad.request(['automate_browser']), /command unavailable/);
});

test('the capture gate reads the live macOS Accessibility grant', () => {
  const denied = loadPermissions({ accessibilityTrusted: false });
  assert.equal(onDarwin(denied.holds), false);
  // `false` never prompts: the gate is read on every conversation request, and a
  // system prompt on each of them would be its own denial-of-service.
  assert.deepEqual(denied.calls, [{ ax: false }]);

  const granted = loadPermissions({ accessibilityTrusted: true });
  assert.equal(onDarwin(granted.holds), true);
});

test('the E2E override stands in for TCC only in an unpackaged build', () => {
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '1';
  try {
    const dev = loadPermissions({ accessibilityTrusted: false });
    assert.equal(dev.holds(), true);
    // The real permission is never consulted, so a CI runner needs no grant.
    assert.deepEqual(dev.calls, []);

    // A shipped app reads macOS whatever its environment claims.
    const packaged = loadPermissions({ isPackaged: true, accessibilityTrusted: false });
    assert.equal(onDarwin(packaged.holds), false);
    assert.deepEqual(packaged.calls, [{ ax: false }]);
  } finally {
    delete process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS;
  }
});

test('the recording screen opens the pane of the gate permission still missing', async () => {
  const f = loadPermissions();
  await f.openSettings();
  assert.match(f.calls.at(-1), /Privacy_Accessibility$/);

  const screen = loadPermissions({ accessibilityTrusted: true, screenRecording: 'denied' });
  await screen.openSettings();
  assert.match(screen.calls.at(-1), /Privacy_ScreenCapture$/);
});

test('the capture gate also requires Screen Recording where macOS can grant it', () => {
  const denied = loadPermissions({ accessibilityTrusted: true, screenRecording: 'denied' });
  assert.equal(onDarwin(denied.holds), false);
  const undecided = loadPermissions({ accessibilityTrusted: true, screenRecording: 'not-determined' });
  assert.equal(onDarwin(undecided.holds), false);
  const granted = loadPermissions({ accessibilityTrusted: true, screenRecording: 'granted' });
  assert.equal(onDarwin(granted.holds), true);
  assert.deepEqual(granted.calls, [{ ax: false }, { media: 'screen' }]);

  // Below macOS 14 the recorder cannot use it, so there is nothing to grant and
  // recording must not wait on it.
  const older = loadPermissions({ accessibilityTrusted: true, screenRecording: 'denied',
    macosVersion: '13.6.1' });
  assert.equal(onDarwin(older.holds), true);
  assert.deepEqual(older.calls, [{ ax: false }]);
});

test('a start missing Screen Recording opens its pane only after the required ones', async () => {
  const f = loadPermissionRequester();
  await f.request(['capture_screen']);
  assert.deepEqual(f.calls, ['x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture']);
  const both = loadPermissionRequester();
  await both.request(['observe_input', 'capture_screen']);
  assert.match(both.calls.at(-1), /Privacy_ListenEvent$/);
});
