const assert = require('assert');
const { createHash } = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  answerScreenCapture,
  stableBrowserWindows,
} = require('../electron/dist/capture/screenCapture.js');
const { isAlwaysDeniedCaptureApp } = require('../electron/dist/privacy/alwaysDeniedCaptureApps.js');
const { isValidImageStoragePath } = require('../electron/dist/protocol/imageStoragePath.js');

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x70, 0x69, 0x78]);
const FRAME = { x: 100, y: 80, width: 800, height: 600 };

function settings(overrides = {}) {
  return {
    version: 2,
    apps: { mode: 'exclude', entries: [] },
    websites: { mode: 'exclude', hosts: [] },
    ideFileRules: {
      mode: 'on',
      onFileNameUnavailable: 'allow',
      sensitivePresets: { blockEnvFiles: true },
    },
    ...overrides,
  };
}

function win(windowId, appName, bundleId, overrides = {}) {
  return { windowId, appName, bundleId, title: 'Window', alpha: 1, bounds: FRAME, ...overrides };
}

const CHROME = win(21, 'Google Chrome', 'com.google.Chrome', { title: 'Docs' });
const SAFARI = win(31, 'Safari', 'com.apple.Safari', { title: 'Docs' });

function page(url, overrides = {}) {
  return { title: 'Docs', bounds: FRAME, url, mode: 'normal', ...overrides };
}

function harness(overrides = {}) {
  const localArtifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-capture-'));
  const captures = [];
  const written = [];
  const browserQueries = [];
  const deps = {
    readScreenRecordingStatus: () => 'granted',
    getCaptureSettings: () => settings(),
    isCaptureEditing: () => false,
    listWindows: async () => [win(11, 'Finder', 'com.apple.finder')],
    readBrowserWindows: async () => null,
    captureWindow: async (window) => {
      captures.push(window.windowId);
      return { png: PNG, widthPx: 800, heightPx: 600 };
    },
    getCurrentSubjectId: () => 'user-1',
    localArtifactRoot: () => localArtifactRoot,
    writeFileAtomic: (targetPath, _tempDirectoryPath, payload) => {
      fs.mkdirSync(path.dirname(targetPath), { recursive: true });
      fs.writeFileSync(targetPath, payload);
      written.push(targetPath);
    },
    now: () => new Date('2026-09-08T00:00:00.000Z'),
    ...overrides,
  };
  const readBrowserWindows = deps.readBrowserWindows;
  deps.readBrowserWindows = async (browser) => {
    browserQueries.push(browser);
    return readBrowserWindows(browser);
  };
  return { deps, captures, written, browserQueries };
}

function browserHarness(window, observed, overrides = {}) {
  return harness({
    listWindows: async () => [window],
    readBrowserWindows: async () => observed,
    ...overrides,
  });
}

test('a granted capture is written under the user image namespace and described exactly', async () => {
  const { deps, written, captures } = harness();

  const answer = await answerScreenCapture(deps, 'Finder');

  assert.equal(answer.status, 'captured');
  assert.ok(isValidImageStoragePath({ userId: 'user-1', storagePath: answer.storage_path }));
  assert.ok(answer.storage_path.startsWith('user-1/2026-09-08/'));
  assert.equal(answer.mime_type, 'image/png');
  assert.equal(answer.byte_size, PNG.byteLength);
  assert.equal(answer.sha256, createHash('sha256').update(PNG).digest('hex'));
  assert.equal(answer.app_name, 'Finder');
  assert.equal(answer.captured_at, '2026-09-08T00:00:00.000Z');
  assert.deepEqual(captures, [11]);
  assert.equal(written.length, 1);
  assert.deepEqual(fs.readFileSync(written[0]), PNG);
});

test('the named app is found by name and its frontmost window is captured, even behind others', async () => {
  const { deps, captures } = harness({
    listWindows: async () => [
      win(1, 'Notes', 'com.apple.Notes'),
      win(2, 'Finder', 'com.apple.finder', { bounds: { x: 0, y: 0, width: 1, height: 1 } }),
      win(3, 'Finder', 'com.apple.finder'),
      win(4, 'Finder', 'com.apple.finder'),
    ],
  });

  const answer = await answerScreenCapture(deps, ' finder ');

  assert.equal(answer.status, 'captured');
  // Window 2 is a helper surface too small to be what anyone reads.
  assert.deepEqual(captures, [3]);
});

test('a missing Screen Recording permission refuses before any window is read', async () => {
  let listed = false;
  const { deps, captures } = harness({
    readScreenRecordingStatus: () => 'denied',
    listWindows: async () => {
      listed = true;
      return [];
    },
  });

  const answer = await answerScreenCapture(deps, 'Finder');

  assert.deepEqual(answer, { status: 'refused', code: 'SCREEN_RECORDING_PERMISSION_REQUIRED' });
  assert.equal(listed, false);
  assert.deepEqual(captures, []);
});

test('editing the filter refuses until the rules are settled', async () => {
  const { deps, captures } = harness({ isCaptureEditing: () => true });

  const answer = await answerScreenCapture(deps, 'Finder');

  assert.equal(answer.axis, 'editing');
  assert.deepEqual(captures, []);
});

test('an app with no window on screen is not found, and only capturable apps are offered', async () => {
  const { deps, captures } = harness({
    getCaptureSettings: () =>
      settings({
        apps: {
          mode: 'exclude',
          entries: [{ name: 'Slack', bundleId: 'com.tinyspeck.slackmacgap' }],
        },
      }),
    listWindows: async () => [
      win(1, 'Notes', 'com.apple.Notes', { title: 'secret plans' }),
      win(2, 'Slack', 'com.tinyspeck.slackmacgap'),
      win(3, '1Password', 'com.1password.1password'),
      win(4, 'Unbundled', null),
      win(5, 'LINE', 'jp.naver.line.mac', { bounds: { x: 0, y: 0, width: 1, height: 1 } }),
      win(6, 'Notes', 'com.apple.Notes'),
    ],
  });

  const answer = await answerScreenCapture(deps, 'Preview');

  assert.deepEqual(answer, {
    status: 'refused',
    code: 'CAPTURE_TARGET_NOT_FOUND',
    available_apps: ['Notes'],
  });
  assert.deepEqual(captures, []);
});

test('a password manager is refused even when the filter names it as allowed', async () => {
  const { deps, captures } = harness({
    listWindows: async () => [win(1, '1Password', 'com.1password.1password')],
    getCaptureSettings: () =>
      settings({
        apps: {
          mode: 'include_only',
          entries: [{ name: '1Password', bundleId: 'com.1password.1password' }],
        },
      }),
  });

  const answer = await answerScreenCapture(deps, '1Password');

  assert.equal(answer.code, 'CAPTURE_REFUSED_PASSWORD_MANAGER');
  assert.equal(answer.app_name, '1Password');
  assert.deepEqual(captures, []);
});

test('an excluded app refuses and an "only these" list refuses everything else', async () => {
  const excluded = harness({
    getCaptureSettings: () =>
      settings({
        apps: { mode: 'exclude', entries: [{ name: 'Finder', bundleId: 'com.apple.finder' }] },
      }),
  });
  const notIncluded = harness({
    getCaptureSettings: () =>
      settings({
        apps: { mode: 'include_only', entries: [{ name: 'Notes', bundleId: 'com.apple.Notes' }] },
      }),
  });

  for (const { deps, captures } of [excluded, notIncluded]) {
    const answer = await answerScreenCapture(deps, 'Finder');
    assert.equal(answer.code, 'CAPTURE_REFUSED_BY_PRIVACY_FILTER');
    assert.equal(answer.axis, 'app');
    assert.equal(answer.app_name, 'Finder');
    assert.deepEqual(captures, []);
  }
});

test('an app with no readable bundle id refuses instead of passing an exclude list', async () => {
  const { deps, captures } = harness({ listWindows: async () => [win(1, 'Slack', null)] });

  const answer = await answerScreenCapture(deps, 'Slack');

  assert.equal(answer.code, 'CAPTURE_REFUSED_BY_PRIVACY_FILTER');
  assert.equal(answer.axis, 'app');
  assert.deepEqual(captures, []);
});

test('a Chrome Incognito window, or one whose mode is unknown, is refused', async () => {
  const cases = [
    ['incognito', 'CAPTURE_REFUSED_PRIVATE_WINDOW'],
    [null, 'CAPTURE_REFUSED_URL_UNAVAILABLE'],
    ['application', 'CAPTURE_REFUSED_URL_UNAVAILABLE'],
  ];
  for (const [mode, code] of cases) {
    const observed = [page('https://docs.example.com/', { mode })];
    const { deps, captures } = browserHarness(CHROME, observed);

    const answer = await answerScreenCapture(deps, 'Google Chrome');

    assert.equal(answer.code, code, String(mode));
    assert.deepEqual(captures, []);
  }
});

test('a page that cannot be tied to exactly one browser window is refused', async () => {
  const cases = [
    ['browser not answering', CHROME, null],
    [
      'no window with that frame',
      CHROME,
      [page('https://a.test/', { bounds: { ...FRAME, x: 0 } })],
    ],
    ['no window with that title', CHROME, [page('https://a.test/', { title: 'Other' })]],
    ['two identical windows', CHROME, [page('https://a.test/'), page('https://b.test/')]],
    ['no title to tie by', { ...CHROME, title: null }, [page('https://a.test/')]],
  ];
  for (const [label, window, observed] of cases) {
    const { deps, captures } = browserHarness(window, observed);

    const answer = await answerScreenCapture(deps, 'Google Chrome');

    assert.equal(answer.code, 'CAPTURE_REFUSED_URL_UNAVAILABLE', label);
    assert.deepEqual(captures, [], label);
  }
});

test('a browser window is tied by frame within a point and title, and Safari has no mode', async () => {
  const nudged = { x: 100.5, y: 79.5, width: 800, height: 600.5 };
  const safari = browserHarness(SAFARI, [
    page('https://other.test/', { title: 'Other' }),
    page('https://docs.example.com/', { bounds: nudged, mode: null }),
  ]);

  assert.equal((await answerScreenCapture(safari.deps, 'Safari')).status, 'captured');
  assert.deepEqual(safari.browserQueries, ['safari', 'safari']);
});

test('a browser whose page cannot be read is refused without asking it', async () => {
  const { deps, captures, browserQueries } = harness({
    listWindows: async () => [win(1, 'Firefox', 'org.mozilla.firefox')],
  });

  const answer = await answerScreenCapture(deps, 'Firefox');

  assert.equal(answer.code, 'CAPTURE_REFUSED_URL_UNAVAILABLE');
  assert.equal(answer.app_name, 'Firefox');
  assert.deepEqual(browserQueries, []);
  assert.deepEqual(captures, []);
});

test('a listed site covers its subdomains but not names that merely end the same way', async () => {
  const cases = [
    ['exclude', 'https://mail.example.com/inbox/secret-thread', 'refused'],
    ['exclude', 'https://example.com/', 'refused'],
    ['exclude', 'https://badexample.com/', 'captured'],
    ['exclude', 'https://example.com.evil.test/', 'captured'],
    ['include_only', 'https://docs.example.com/page', 'captured'],
    ['include_only', 'https://badexample.com/', 'refused'],
  ];
  for (const [mode, url, status] of cases) {
    const { deps } = browserHarness(CHROME, [page(url)], {
      getCaptureSettings: () => settings({ websites: { mode, hosts: ['example.com'] } }),
    });

    const answer = await answerScreenCapture(deps, 'Google Chrome');

    assert.equal(answer.status, status, `${mode} ${url}`);
    if (status === 'refused') {
      assert.equal(answer.axis, 'website');
      assert.equal(answer.host, new URL(url).hostname);
      assert.equal(JSON.stringify(answer).includes('secret-thread'), false);
      assert.equal(JSON.stringify(answer).includes('Docs'), false);
    }
  }
});

test('a sign-in or payment page on an allowed host refuses without naming it', async () => {
  for (const url of [
    'https://docs.example.com/login',
    'https://docs.example.com/settings/billing',
    'https://docs.example.com/app#/password/reset',
    'https://checkout.example.com/c/abc',
  ]) {
    const { deps, captures } = browserHarness(SAFARI, [page(url, { mode: null })]);

    const answer = await answerScreenCapture(deps, 'Safari');

    assert.equal(answer.code, 'CAPTURE_REFUSED_SENSITIVE_PAGE', url);
    // The route is what gave the page away; repeating it leaks what was protected.
    assert.deepEqual(Object.keys(answer).sort(), ['app_name', 'code', 'status']);
    assert.deepEqual(captures, []);
  }
});

test('a page that only reads like a sign-in route is still captured', async () => {
  for (const url of [
    'https://docs.example.com/blog/login-guide',
    'https://docs.example.com/oauth-client-library',
    'https://docs.example.com/checkout-history',
    'https://docs.example.com/docs#login-form',
  ]) {
    const { deps } = browserHarness(CHROME, [page(url)], {
      getCaptureSettings: () =>
        settings({ websites: { mode: 'include_only', hosts: ['docs.example.com'] } }),
    });

    assert.equal((await answerScreenCapture(deps, 'Google Chrome')).status, 'captured', url);
  }
});

test('a browser page that is not a website at all is refused, not filtered by its host', async () => {
  const { deps, captures } = browserHarness(CHROME, [page('chrome://password-manager/passwords')]);

  const answer = await answerScreenCapture(deps, 'Google Chrome');

  assert.equal(answer.code, 'CAPTURE_REFUSED_URL_UNAVAILABLE');
  assert.deepEqual(captures, []);
});

test('an editor window showing a .env file is refused, and its title is never repeated', async () => {
  const cases = [
    ['● .env.local — app', 'allow', true, 'refused'],
    ['README.md — app', 'block', true, 'captured'],
    ['Welcome — app', 'block', true, 'refused'],
    ['Welcome — app', 'allow', true, 'captured'],
    [null, 'block', true, 'refused'],
    ['.env — app', 'block', false, 'captured'],
  ];
  for (const [title, onFileNameUnavailable, blockEnvFiles, status] of cases) {
    const { deps } = harness({
      listWindows: async () => [win(1, 'Cursor', 'com.todesktop.230313mzl4w4u92', { title })],
      getCaptureSettings: () =>
        settings({
          ideFileRules: {
            mode: blockEnvFiles ? 'on' : 'off',
            onFileNameUnavailable,
            sensitivePresets: { blockEnvFiles },
          },
        }),
    });

    const answer = await answerScreenCapture(deps, 'Cursor');

    assert.equal(answer.status, status, `${title} ${onFileNameUnavailable} ${blockEnvFiles}`);
    if (status === 'refused') {
      assert.deepEqual(answer, {
        status: 'refused',
        code: 'CAPTURE_REFUSED_BY_PRIVACY_FILTER',
        axis: 'file',
        app_name: 'Cursor',
      });
    }
  }
});

test('a page that changes while the window is captured is discarded, not stored', async () => {
  let reads = 0;
  const { deps, captures, written } = browserHarness(CHROME, null, {
    readBrowserWindows: async () =>
      reads++ === 0 ? [page('https://docs.example.com/')] : [page('https://bank.test/login')],
  });

  const answer = await answerScreenCapture(deps, 'Google Chrome');

  assert.equal(answer.code, 'CAPTURE_REFUSED_SENSITIVE_PAGE');
  assert.deepEqual(captures, [21]);
  assert.deepEqual(written, []);
});

test('a filter changed or opened for editing during the capture discards the image', async () => {
  const cases = [
    [
      'app excluded',
      (state) => {
        state.settings = settings({
          apps: { mode: 'exclude', entries: [{ name: 'Finder', bundleId: 'com.apple.finder' }] },
        });
      },
      { axis: 'app', app_name: 'Finder' },
    ],
    [
      'editing started',
      (state) => {
        state.editing = true;
      },
      { axis: 'editing' },
    ],
  ];
  for (const [label, change, expected] of cases) {
    const state = { settings: settings(), editing: false };
    const { deps, captures, written } = harness({
      getCaptureSettings: () => state.settings,
      isCaptureEditing: () => state.editing,
      captureWindow: async (window) => {
        captures.push(window.windowId);
        change(state);
        return { png: PNG, widthPx: 800, heightPx: 600 };
      },
    });

    const answer = await answerScreenCapture(deps, 'Finder');

    assert.deepEqual(
      answer,
      { status: 'refused', code: 'CAPTURE_REFUSED_BY_PRIVACY_FILTER', ...expected },
      label
    );
    assert.deepEqual(captures, [11], label);
    assert.deepEqual(written, [], label);
  }
});

test('browser windows are kept only when no window opened, closed or moved during the probe', () => {
  const record = (id, url) => ({ id, ...page(url) });
  const windows = [record('7', 'https://a.test/'), record('9', 'https://b.test/')];

  assert.deepEqual(stableBrowserWindows({ before: ['7', '9'], windows, after: ['7', '9'] }), [
    page('https://a.test/'),
    page('https://b.test/'),
  ]);
  for (const [label, probe] of [
    ['a window opened', { before: ['7', '9'], windows, after: ['7', '9', '11'] }],
    ['a window closed', { before: ['7', '9'], windows, after: ['7'] }],
    ['windows reordered', { before: ['7', '9'], windows, after: ['9', '7'] }],
    ['records out of step', { before: ['9', '7'], windows, after: ['9', '7'] }],
  ]) {
    assert.equal(stableBrowserWindows(probe), null, label);
  }
});

test('a window that is gone after the capture, or yields no image, is answered as not found', async () => {
  let listings = 0;
  const vanished = harness({
    listWindows: async () => (listings++ === 0 ? [win(11, 'Finder', 'com.apple.finder')] : []),
  });
  const imageless = harness({ captureWindow: async () => null });

  for (const { deps, written } of [vanished, imageless]) {
    const answer = await answerScreenCapture(deps, 'Finder');
    assert.equal(answer.code, 'CAPTURE_TARGET_NOT_FOUND');
    assert.deepEqual(written, []);
  }
});

test('the always-denied list matches by bundle id and by name, not by prefix', () => {
  assert.equal(
    isAlwaysDeniedCaptureApp({ name: 'Whatever', bundleId: 'com.bitwarden.desktop' }),
    true
  );
  assert.equal(isAlwaysDeniedCaptureApp({ name: 'KeePassXC', bundleId: null }), true);
  assert.equal(
    isAlwaysDeniedCaptureApp({ name: 'Notes', bundleId: 'com.apple.keychainaccess.helper' }),
    false
  );
  assert.equal(isAlwaysDeniedCaptureApp({ name: 'Notes', bundleId: 'com.apple.Notes' }), false);
});
