const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');

const RELEASE_PATH = path.resolve(__dirname, '../electron/zanei_release.json');
const ZANEI_STAGING_DIR = path.resolve(__dirname, '../.generated/zanei');
const PARITY_CASES_BASE_URL = 'https://raw.githubusercontent.com/KentoShimizu/zanei';
const PARITY_CASES_PATH = 'crates/zanei-core/tests/privacy_parity_cases.json';
const PARITY_CASES_VENDORED = 'tests/fixtures/zanei_privacy_parity_cases.json';
/**
 * The first recorder release that ships the call transcription helper beside `zanei`. The app
 * hosts transcription exactly when the staged helper is there, so a release that has it must
 * not stage without it; an earlier one has none to stage.
 */
const MEETING_HELPER_SINCE = [0, 8, 0];
const MEETING_HELPER = 'zanei-meeting';

async function stageZaneiRuntime({
  releasePath = RELEASE_PATH,
  stagingDir = ZANEI_STAGING_DIR,
  fetchArtifact = fetch,
  run = execFileSync,
} = {}) {
  const release = JSON.parse(fs.readFileSync(releasePath, 'utf8'));
  if (!/^\d+\.\d+\.\d+$/.test(release.version)) {
    throw new Error('Invalid pinned Zanei release version');
  }
  if (
    typeof release.artifactSHA256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(release.artifactSHA256)
  ) {
    throw new Error(
      'Zanei release artifact SHA256 is not pinned; finalize the release manifest first'
    );
  }
  await verifyPrivacyParityCases(release, fetchArtifact);
  const artifact = `zanei-${release.version}-macos-universal.tar.gz`;
  const url = `https://github.com/KentoShimizu/zanei/releases/download/v${release.version}/${artifact}`;
  const response = await fetchArtifact(url, { signal: AbortSignal.timeout(120000) });
  if (!response.ok) throw new Error(`Zanei artifact download failed: HTTP ${response.status}`);
  const archive = Buffer.from(await response.arrayBuffer());
  if (createHash('sha256').update(archive).digest('hex') !== release.artifactSHA256) {
    throw new Error('Zanei artifact SHA256 mismatch');
  }

  fs.mkdirSync(path.dirname(stagingDir), { recursive: true });
  const temporary = fs.mkdtempSync(path.join(path.dirname(stagingDir), '.zanei-stage-'));
  try {
    const archivePath = path.join(temporary, artifact);
    const unpacked = path.join(temporary, 'runtime');
    fs.mkdirSync(unpacked);
    fs.writeFileSync(archivePath, archive);
    run('/usr/bin/tar', ['-xzf', archivePath, '-C', unpacked], { stdio: 'pipe' });
    const app = path.join(unpacked, 'Zanei.app');
    const binary = path.join(app, 'Contents/MacOS/zanei');
    const helper = path.join(app, 'Contents/MacOS', MEETING_HELPER);
    if (
      !fs.statSync(app).isDirectory() ||
      !fs.statSync(path.join(unpacked, 'THIRD_PARTY_NOTICES.md')).isFile()
    ) {
      throw new Error('Zanei release is missing its app or notices');
    }
    if (
      shipsMeetingHelper(release.version) &&
      !fs.statSync(helper, { throwIfNoEntry: false })?.isFile()
    ) {
      throw new Error(`Zanei ${release.version} release is missing ${MEETING_HELPER}`);
    }
    run('/usr/bin/codesign', ['--verify', '--deep', '--strict', app], { stdio: 'pipe' });
    const version = run(binary, ['--version'], { encoding: 'utf8', timeout: 10000 }).trim();
    if (version !== `zanei ${release.version}`) {
      throw new Error('Zanei binary version differs from the pinned release');
    }
    fs.rmSync(stagingDir, { recursive: true, force: true });
    fs.renameSync(unpacked, stagingDir);
    return stagingDir;
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

function shipsMeetingHelper(version) {
  const parts = version.split('.').map(Number);
  const index = parts.findIndex((part, i) => part !== MEETING_HELPER_SINCE[i]);
  return index === -1 || parts[index] > MEETING_HELPER_SINCE[index];
}

/**
 * The recorder's own capture-policy cases, at the pinned release.
 *
 * `electron/src/privacy/browserUrlPolicy.ts` restates that policy for `capture_screen`,
 * and `tests/fixtures/zanei_privacy_parity_cases.json` is the copy its parity test runs.
 * Checking the pinned digest against the tag is what turns a recorder release that
 * changes the rule into a build failure instead of a silent divergence: bumping the
 * version means re-downloading the cases, and the parity test then judges the rule.
 */
async function verifyPrivacyParityCases(release, fetchArtifact) {
  const url = `${PARITY_CASES_BASE_URL}/v${release.version}/${PARITY_CASES_PATH}`;
  const response = await fetchArtifact(url, { signal: AbortSignal.timeout(120000) });
  if (!response.ok) {
    throw new Error(`Zanei privacy parity cases download failed: HTTP ${response.status}`);
  }
  const cases = Buffer.from(await response.arrayBuffer());
  if (createHash('sha256').update(cases).digest('hex') !== release.privacyParityCasesSHA256) {
    throw new Error(
      `Zanei privacy parity cases differ from the pin; re-vendor ${PARITY_CASES_VENDORED} from ${url} and update privacyParityCasesSHA256`
    );
  }
}

if (require.main === module) {
  stageZaneiRuntime().then(
    (directory) => process.stdout.write(`Staged Zanei runtime: ${directory}\n`),
    (error) => {
      process.stderr.write(`${error.message}\n`);
      process.exitCode = 1;
    }
  );
}

module.exports = { RELEASE_PATH, ZANEI_STAGING_DIR, stageZaneiRuntime };
