const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');

// Electron adds `process.getSystemVersion`; plain Node, which runs these tests, has none.
let systemVersion = '15.0.0';
process.getSystemVersion = () => systemVersion;

function loadPermissions({ error = null, isPackaged = false, accessibilityTrusted = false,
  screenRecording = 'granted', macosVersion = '15.0.0', captureError = null } = {}) {
  const calls = [];
  systemVersion = macosVersion;
  const target = require.resolve('../electron/dist/windows/macosPermissions');
  delete require.cache[target];
  const load = Module._load;
  Module._load = function(request, parent, isMain) {
    if (request === 'electron') return {
      app: { isPackaged },
      desktopCapturer: { getSources: async options => {
        calls.push({ capture: options }); if (captureError) throw captureError; return [];
      } },
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
      read: module.readCapturePermissions,
      ask: module.askForCapturePermission, calls };
  }
  finally { Module._load = load; delete require.cache[target]; }
}

const loadPermissionRequester = error => loadPermissions({ error });

/** The gate reads a macOS API, so the tests run on Linux CI must claim that platform. */
function onPlatform(platform, body) {
  const original = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: platform, configurable: true });
  try { return body(); } finally { Object.defineProperty(process, 'platform', original); }
}
const onDarwin = body => onPlatform('darwin', body);

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

// System Settings lists only apps that asked, so each pane opens after its own ask.
test('each gate permission asks macOS for itself, then opens its own pane', async () => {
  const accessibility = loadPermissions();
  await accessibility.ask('accessibility');
  assert.deepEqual(accessibility.calls, [
    { ax: true },
    'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility',
  ]);

  const screen = loadPermissions();
  await screen.ask('screen_recording');
  assert.deepEqual(screen.calls, [
    { capture: { types: ['screen'], thumbnailSize: { width: 1, height: 1 } } },
    'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
  ]);
});

test('a capture refused for want of the grant still opens the Screen Recording pane', async (t) => {
  t.mock.method(console, 'error', () => {});
  const f = loadPermissions({ captureError: new Error('Failed to get sources.') });
  await f.ask('screen_recording');
  assert.match(f.calls.at(-1), /Privacy_ScreenCapture$/);
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

test('a start missing Screen Recording opens its pane only after the required ones', async () => {
  const f = loadPermissionRequester();
  await f.request(['capture_screen']);
  assert.deepEqual(f.calls, ['x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture']);
  const both = loadPermissionRequester();
  await both.request(['observe_input', 'capture_screen']);
  assert.match(both.calls.at(-1), /Privacy_ListenEvent$/);
});
