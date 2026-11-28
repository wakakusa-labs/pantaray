const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { stageZaneiRuntime } = require('../scripts/stage-zanei-runtime');

const PARITY_CASES = fs.readFileSync(
  path.join(__dirname, 'fixtures/zanei_privacy_parity_cases.json')
);
const isParityCasesUrl = (url) => url.includes('privacy_parity_cases.json');

function fixture(t, version = '0.5.0', { pinned = version, helper = false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'stage-zanei-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = path.join(root, 'source/Zanei.app/Contents/MacOS');
  fs.mkdirSync(app, { recursive: true });
  fs.writeFileSync(path.join(app, 'zanei'), `#!/bin/sh\nprintf 'zanei ${version}\\n'\n`, {
    mode: 0o755,
  });
  if (helper) fs.writeFileSync(path.join(app, 'zanei-meeting'), 'helper', { mode: 0o755 });
  fs.writeFileSync(path.join(root, 'source/THIRD_PARTY_NOTICES.md'), 'Notices');
  const archivePath = path.join(root, 'release.tar.gz');
  execFileSync('/usr/bin/tar', [
    '-czf',
    archivePath,
    '-C',
    path.join(root, 'source'),
    'Zanei.app',
    'THIRD_PARTY_NOTICES.md',
  ]);
  const archive = fs.readFileSync(archivePath);
  const releasePath = path.join(root, 'release.json');
  fs.writeFileSync(
    releasePath,
    JSON.stringify({
      version: pinned,
      artifactSHA256: createHash('sha256').update(archive).digest('hex'),
      privacyParityCasesSHA256: createHash('sha256').update(PARITY_CASES).digest('hex'),
    })
  );
  const stagingDir = path.join(root, 'staged');
  fs.mkdirSync(stagingDir);
  fs.writeFileSync(path.join(stagingDir, 'previous'), 'preserve');
  const serve =
    ({ archiveBytes = archive, casesBytes = PARITY_CASES } = {}) =>
    async (url) =>
      new Response(isParityCasesUrl(url) ? casesBytes : archiveBytes);
  return {
    root,
    releasePath,
    stagingDir,
    serve,
    fetchArtifact: serve(),
    run: (command, args, options) =>
      command === '/usr/bin/codesign' ? '' : execFileSync(command, args, options),
  };
}

test('stages authenticated app and notices with matching executable version', async (t) => {
  const options = fixture(t);
  await stageZaneiRuntime(options);
  assert.equal(
    fs.readFileSync(path.join(options.stagingDir, 'THIRD_PARTY_NOTICES.md'), 'utf8'),
    'Notices'
  );
  assert.equal(
    execFileSync(path.join(options.stagingDir, 'Zanei.app/Contents/MacOS/zanei'), ['--version'], {
      encoding: 'utf8',
    }).trim(),
    'zanei 0.5.0'
  );
  assert.ok(!fs.existsSync(path.join(options.stagingDir, 'previous')));
});

for (const failure of ['unpinned', 'checksum', 'parity', 'network', 'signature', 'version']) {
  test(`${failure} fails without replacing the existing runtime`, async (t) => {
    const options = fixture(t, failure === 'version' ? '0.4.0' : '0.5.0', { pinned: '0.5.0' });
    if (failure === 'unpinned')
      fs.writeFileSync(
        options.releasePath,
        JSON.stringify({ version: '0.5.0', artifactSHA256: null })
      );
    if (failure === 'checksum') options.fetchArtifact = options.serve({ archiveBytes: 'wrong' });
    // A recorder release that changed the shared capture policy, before the vendored
    // cases and `browserUrlPolicy.ts` were brought back in line with it.
    if (failure === 'parity') options.fetchArtifact = options.serve({ casesBytes: 'drifted' });
    if (failure === 'network')
      options.fetchArtifact = async () => new Response('', { status: 503 });
    if (failure === 'signature')
      options.run = (command, args, config) => {
        if (command === '/usr/bin/codesign') throw new Error('signature invalid');
        return execFileSync(command, args, config);
      };
    if (['unpinned', 'checksum', 'parity', 'network'].includes(failure))
      options.run = () => assert.fail('must not extract or execute unverified bytes');
    await assert.rejects(stageZaneiRuntime(options));
    assert.equal(fs.readFileSync(path.join(options.stagingDir, 'previous'), 'utf8'), 'preserve');
    assert.ok(!fs.readdirSync(options.root).some((name) => name.startsWith('.zanei-stage-')));
  });
}

test('a release that ships the transcription helper stages only with it', async (t) => {
  const withHelper = fixture(t, '0.8.0', { helper: true });
  await stageZaneiRuntime(withHelper);
  assert.ok(fs.existsSync(path.join(withHelper.stagingDir, 'Zanei.app/Contents/MacOS/zanei-meeting')));

  // The app hosts transcription exactly when the helper is staged, so a missing one fails.
  const missing = fixture(t, '0.10.0');
  await assert.rejects(stageZaneiRuntime(missing), /0\.10\.0 release is missing zanei-meeting/);
  assert.equal(fs.readFileSync(path.join(missing.stagingDir, 'previous'), 'utf8'), 'preserve');
});
