const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const { ideTitleOutcome, isIdeApp } = require('../electron/dist/privacy/ideFilePolicy.js');

// Vendored from the pinned recorder release; `electron_browser_url_policy.test.js` pins
// its digest, so these are the recorder's own cases.
const FIXTURE = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures/zanei_privacy_parity_cases.json'), 'utf8')
);

/** The recorder's outcome names under the policy the cases were recorded with. */
const OUTCOMES = {
  allow: 'Allow',
  env_file: 'EnvFile',
  file_name_unavailable: 'FileNameUnavailable',
};

test('the IDE cases were recorded with .env blocking on and unreadable names blocked', () => {
  // With unreadable names allowed instead, `FileNameUnavailable` reads as `Allow`;
  // `windowCapturePolicy.ts` applies that setting on top of the outcome tested here.
  assert.deepEqual(FIXTURE.policy.ide, {
    block_env_files: true,
    on_file_name_unavailable: 'block',
  });
});

test('every recorder IDE title decides the same way here', () => {
  for (const [title, expected] of FIXTURE.ide_cases) {
    assert.equal(OUTCOMES[ideTitleOutcome(title)], expected, JSON.stringify(title));
  }
});

test('the editors are the ones the recorder reads titles from, by display name', () => {
  for (const name of ['Cursor', 'Visual Studio Code', 'Code', ' code ']) {
    assert.equal(isIdeApp(name), true, name);
  }
  for (const name of ['Xcode', 'Code Helper', 'Visual Studio']) {
    assert.equal(isIdeApp(name), false, name);
  }
});
