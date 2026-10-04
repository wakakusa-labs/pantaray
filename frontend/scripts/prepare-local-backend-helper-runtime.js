const nodeCrypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const {
  HELPER_RUNTIME_MANIFEST_FILENAME,
} = require('../electron/local_backend_runtime_bundle.js');

const FRONTEND_ROOT = path.resolve(__dirname, '..');
const AGENTS_ROOT = path.resolve(FRONTEND_ROOT, '..', 'agents');
const HELPER_RUNTIME_DIRNAME = 'local_backend_helper';
const APP_RUNTIME_MANIFEST_FILENAME = 'app-runtime-manifest.json';
const HELPER_RUNTIME_CACHE_ROOT = path.join(
  FRONTEND_ROOT,
  '.cache',
  'local_backend_helper'
);
const UV_LOCK_PATH = path.join(AGENTS_ROOT, 'uv.lock');
const PYTHON_STANDALONE_RELEASE_TAG = '20260325';
const PYTHON_VERSION = '3.12.13';
const PYTHON_STANDALONE_ARCHIVE_NAME =
  `cpython-${PYTHON_VERSION}+${PYTHON_STANDALONE_RELEASE_TAG}` +
  '-aarch64-apple-darwin-install_only_stripped.tar.gz';
const PYTHON_STANDALONE_ARCHIVE_URL =
  'https://github.com/astral-sh/python-build-standalone/releases/download/' +
  `${PYTHON_STANDALONE_RELEASE_TAG}/${encodeURIComponent(PYTHON_STANDALONE_ARCHIVE_NAME)}`;
const PYTHON_STANDALONE_ARCHIVE_SHA256 =
  'c33a34853ae48d54fbac15cbb84ad67ccd8a639ce2cef866ecf474ebd02f1286';
const PYTHON_RUNTIME_EXECUTABLE_RELATIVE_PATH = path.join('bin', 'python3');
const PIP_INSTALL_ARGS = ['-m', 'pip', 'install'];
const PIP_INSTALL_WHEEL_ARGS = [...PIP_INSTALL_ARGS, '--no-index', '--no-deps'];
const CURL_DOWNLOAD_ARGS = ['-fL', '--retry', '3', '--retry-delay', '1'];
const POSIX_PERMISSION_MASK = 0o777;
// The helper imports thousands of modules at every start and runs with
// PYTHONDONTWRITEBYTECODE inside a signed, read-only bundle, so bytecode has to
// ship. unchecked-hash bytecode never compares against source mtimes, which
// staging and packaging copies do not preserve. -f rewrites the timestamp-based
// files that pip and the verification step leave behind.
const COMPILE_BYTECODE_ARGS = [
  '-I',
  '-m',
  'compileall',
  '-q',
  '-f',
  '-j',
  '0',
  '--invalidation-mode',
  'unchecked-hash',
];
const UV_EXPORT_ARGS = [
  '--quiet',
  'export',
  '--project',
  AGENTS_ROOT,
  '--locked',
  '--no-dev',
  '--package',
  'pantaray-agents',
  '--format',
  'requirements.txt',
  '--no-emit-workspace',
];
const UV_BUILD_ARGS = ['--quiet', 'build', '--wheel'];
// Workspace members that must ship in the helper runtime, built one wheel at a
// time so that the bundle's contents are named here rather than inferred from
// whatever the workspace happens to contain.
const BUNDLED_WORKSPACE_PACKAGES = ['pantaray-agents', 'pantaray-llm'];
// Wheel file name prefixes of every workspace member that must ship in the helper runtime.
const WORKSPACE_WHEEL_PREFIXES = ['pantaray_agents-', 'pantaray_llm-'];
const VERIFY_MIGRATIONS_INLINE_SCRIPT = `
import sqlite3
from importlib.util import find_spec

import sqlite_vec
# Importing pypdfium2 loads libpdfium.dylib, so a build whose bundled copy of
# the library is missing or unloadable fails here rather than the first time a
# PDF page has to be drawn.
import pypdfium2
import pantaray_llm.errors
# Reads Pantaray's default AGENTS.md at import, so a wheel without it fails here.
import pantaray_agents.agents.action_agent.runtime.agents_md
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

# The Cloud distribution is commercial and its SDKs are Cloud-only: none may ship.
for cloud_only_module in ('pantaray_cloud', 'supabase', 'boto3'):
    assert find_spec(cloud_only_module) is None, cloud_only_module

migrations = load_default_migrations()
assert migrations, 'local runtime migrations must not be empty'
assert migrations[0].name == '0001_core_runtime.sql'

with sqlite3.connect(':memory:') as connection:
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)
    extension_version = connection.execute('SELECT vec_version()').fetchone()[0]
    assert extension_version == 'v0.1.9', extension_version
    connection.execute(
        'CREATE VIRTUAL TABLE vec_smoke '
        'USING vec0(embedding FLOAT[2] DISTANCE_METRIC=cosine)'
    )
    connection.executemany(
        'INSERT INTO vec_smoke(rowid, embedding) VALUES (?, ?)',
        ((1, '[1,0]'), (2, '[0,1]')),
    )
    match = connection.execute(
        'SELECT rowid, distance FROM vec_smoke '
        'WHERE embedding MATCH ? AND k = 1 ORDER BY distance',
        ('[1,0]',),
    ).fetchone()
    assert match is not None and match[0] == 1 and abs(match[1]) < 1e-6, match
    # The memory catalog indexes fragments with the trigram tokenizer, which a
    # differently built sqlite may leave out.
    connection.execute(
        "CREATE VIRTUAL TABLE fts_smoke USING fts5(text, tokenize='trigram')"
    )
    connection.execute("INSERT INTO fts_smoke(text) VALUES ('経費精算を申請した')")
    trigram_hits = connection.execute(
        'SELECT count(*) FROM fts_smoke WHERE fts_smoke MATCH ?', ('"費精算"',)
    ).fetchone()[0]
    assert trigram_hits == 1, trigram_hits

print(f'local runtime migrations verified: {len(migrations)} files')
print(f'sqlite-vec verified: {extension_version}')
print('fts5 trigram tokenizer verified')
`.trim();

function ensureSupportedBuildHost() {
  if (process.platform !== 'darwin') {
    throw new Error('local backend helper runtime packaging is supported only on darwin.');
  }
  if (process.arch !== 'arm64') {
    throw new Error('local backend helper runtime packaging requires arm64 build host.');
  }
}

function requireFile(filePath) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`Missing required file: ${filePath}`);
  }
  return filePath;
}

function execRequired(command, args, options = {}) {
  try {
    return execFileSync(command, args, {
      stdio: 'inherit',
      ...options,
    });
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      throw new Error(`Required command is not available: ${command}`);
    }
    throw error;
  }
}

function sha256File(filePath) {
  const digest = nodeCrypto.createHash('sha256');
  digest.update(fs.readFileSync(filePath));
  return digest.digest('hex');
}

function buildHelperRuntimePaths() {
  const archivePath = path.join(
    HELPER_RUNTIME_CACHE_ROOT,
    PYTHON_STANDALONE_ARCHIVE_NAME
  );
  return {
    archivePath,
    helperRuntimeRoot: HELPER_RUNTIME_CACHE_ROOT,
    helperRuntimeExecutable: path.join(
      HELPER_RUNTIME_CACHE_ROOT,
      PYTHON_RUNTIME_EXECUTABLE_RELATIVE_PATH
    ),
    helperRuntimeManifestPath: path.join(
      HELPER_RUNTIME_CACHE_ROOT,
      HELPER_RUNTIME_MANIFEST_FILENAME
    ),
    appRuntimeManifestPath: path.join(
      HELPER_RUNTIME_CACHE_ROOT,
      APP_RUNTIME_MANIFEST_FILENAME
    ),
  };
}

function ensureArchive(archivePath) {
  fs.mkdirSync(path.dirname(archivePath), { recursive: true });
  if (fs.existsSync(archivePath)) {
    const digest = sha256File(archivePath);
    if (digest === PYTHON_STANDALONE_ARCHIVE_SHA256) {
      return;
    }
    fs.rmSync(archivePath, { force: true });
  }
  execRequired('curl', [...CURL_DOWNLOAD_ARGS, '-o', archivePath, PYTHON_STANDALONE_ARCHIVE_URL]);
  const digest = sha256File(archivePath);
  if (digest !== PYTHON_STANDALONE_ARCHIVE_SHA256) {
    fs.rmSync(archivePath, { force: true });
    throw new Error(
      [
        'Downloaded helper runtime archive digest mismatch.',
        `expected=${PYTHON_STANDALONE_ARCHIVE_SHA256}`,
        `actual=${digest}`,
      ].join(' ')
    );
  }
}

function extractHelperRuntime(archivePath, destinationRoot) {
  const stagingParent = fs.mkdtempSync(
    path.join(os.tmpdir(), 'pantaray-local-backend-helper-')
  );
  try {
    execRequired('tar', ['-xzf', archivePath, '-C', stagingParent]);
    const extractedRuntimeRoot = path.join(stagingParent, 'python');
    copyExtractedRuntime(extractedRuntimeRoot, destinationRoot);
  } finally {
    fs.rmSync(stagingParent, { recursive: true, force: true });
  }
}

function copyExtractedRuntime(extractedRuntimeRoot, destinationRoot) {
  requireFile(path.join(extractedRuntimeRoot, PYTHON_RUNTIME_EXECUTABLE_RELATIVE_PATH));
  fs.rmSync(destinationRoot, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(destinationRoot), { recursive: true });
  copyRuntimeTree({
    sourcePath: extractedRuntimeRoot,
    destinationPath: destinationRoot,
    runtimeRootRealPath: fs.realpathSync(extractedRuntimeRoot),
  });
  assertNoRuntimeSymlinks(destinationRoot);
}

function copyRuntimeTree({
  sourcePath,
  destinationPath,
  runtimeRootRealPath,
  activeDirectoryRealPaths = new Set(),
  activeSymlinkPaths = new Set(),
}) {
  const sourceStat = fs.lstatSync(sourcePath);
  if (sourceStat.isSymbolicLink()) {
    const symlinkPath = path.resolve(sourcePath);
    if (activeSymlinkPaths.has(symlinkPath)) {
      throw new Error(`Detected symlink cycle while copying helper runtime: ${sourcePath}`);
    }
    activeSymlinkPaths.add(symlinkPath);
    const resolvedTarget = resolveRuntimeSymlinkTarget({
      linkPath: sourcePath,
      runtimeRootRealPath,
    });
    try {
      copyRuntimeTree({
        sourcePath: resolvedTarget,
        destinationPath,
        runtimeRootRealPath,
        activeDirectoryRealPaths,
        activeSymlinkPaths,
      });
    } finally {
      activeSymlinkPaths.delete(symlinkPath);
    }
    return;
  }
  if (sourceStat.isDirectory()) {
    const realPath = fs.realpathSync(sourcePath);
    if (activeDirectoryRealPaths.has(realPath)) {
      throw new Error(`Detected symlink cycle while copying helper runtime: ${sourcePath}`);
    }
    activeDirectoryRealPaths.add(realPath);
    const permissions = sourceStat.mode & POSIX_PERMISSION_MASK;
    fs.mkdirSync(destinationPath, { recursive: true, mode: permissions });
    for (const entryName of fs.readdirSync(sourcePath)) {
      copyRuntimeTree({
        sourcePath: path.join(sourcePath, entryName),
        destinationPath: path.join(destinationPath, entryName),
        runtimeRootRealPath,
        activeDirectoryRealPaths,
        activeSymlinkPaths,
      });
    }
    activeDirectoryRealPaths.delete(realPath);
    fs.chmodSync(destinationPath, permissions);
    return;
  }
  if (sourceStat.isFile()) {
    const permissions = sourceStat.mode & POSIX_PERMISSION_MASK;
    fs.mkdirSync(path.dirname(destinationPath), { recursive: true });
    fs.copyFileSync(sourcePath, destinationPath);
    fs.chmodSync(destinationPath, permissions);
    return;
  }
  throw new Error(`Unsupported helper runtime file type: ${sourcePath}`);
}

function resolveRuntimeSymlinkTarget({ linkPath, runtimeRootRealPath }) {
  const linkTarget = fs.readlinkSync(linkPath);
  const resolvedTarget = path.isAbsolute(linkTarget)
    ? linkTarget
    : path.resolve(path.dirname(linkPath), linkTarget);
  let targetRealPath;
  try {
    targetRealPath = fs.realpathSync(resolvedTarget);
  } catch (error) {
    if (error && error.code === 'ELOOP') {
      throw new Error(`Detected symlink cycle while copying helper runtime: ${linkPath}`);
    }
    throw new Error(
      `Invalid helper runtime symlink target: ${linkPath} -> ${linkTarget}`,
      { cause: error }
    );
  }
  if (!isPathInsideOrEqual(targetRealPath, runtimeRootRealPath)) {
    throw new Error(
      `Helper runtime symlink target escapes runtime root: ${linkPath} -> ${linkTarget}`
    );
  }
  return resolvedTarget;
}

function isPathInsideOrEqual(candidatePath, rootPath) {
  const relativePath = path.relative(rootPath, candidatePath);
  return (
    relativePath === '' ||
    (!relativePath.startsWith('..') && !path.isAbsolute(relativePath))
  );
}

function findRuntimeSymlinkPaths(runtimeRoot) {
  const pending = [runtimeRoot];
  const symlinks = [];
  while (pending.length > 0) {
    const currentDir = pending.pop();
    for (const entry of fs.readdirSync(currentDir, { withFileTypes: true })) {
      const entryPath = path.join(currentDir, entry.name);
      if (entry.isSymbolicLink()) {
        symlinks.push(entryPath);
        continue;
      }
      if (entry.isDirectory()) {
        pending.push(entryPath);
      }
    }
  }
  return symlinks.sort();
}

function assertNoRuntimeSymlinks(runtimeRoot) {
  const symlinks = findRuntimeSymlinkPaths(runtimeRoot);
  if (symlinks.length > 0) {
    throw new Error(
      [
        'Local backend helper runtime must not contain symlinks.',
        `symlinks=${symlinks.join(',')}`,
      ].join(' ')
    );
  }
}

function compilePythonBytecode(runtimePython, runtimeRoot) {
  execRequired(runtimePython, [...COMPILE_BYTECODE_ARGS, runtimeRoot]);
}

function exportLockedRequirements(outputPath) {
  execRequired('uv', [...UV_EXPORT_ARGS, '--output-file', outputPath], {
    cwd: FRONTEND_ROOT,
  });
}

function installLockedDependencies(runtimePython, requirementsPath) {
  execRequired(runtimePython, [...PIP_INSTALL_ARGS, '--require-hashes', '-r', requirementsPath], {
    cwd: AGENTS_ROOT,
    env: {
      ...process.env,
      PIP_DISABLE_PIP_VERSION_CHECK: '1',
      PYTHONNOUSERSITE: '1',
    },
  });
}

function buildAndInstallAgentsPackage(runtimePython) {
  const wheelDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-agents-wheel-'));
  try {
    // The agents workspace builds one wheel per bundled member (pantaray-llm,
    // pantaray-agents). Locked third-party dependencies are already installed, so
    // every workspace wheel installs with --no-deps regardless of order.
    for (const packageName of BUNDLED_WORKSPACE_PACKAGES) {
      execRequired(
        'uv',
        [...UV_BUILD_ARGS, '--package', packageName, '--out-dir', wheelDirectory],
        { cwd: AGENTS_ROOT }
      );
    }
    const wheelNames = fs
      .readdirSync(wheelDirectory)
      .filter((name) => name.endsWith('.whl'))
      .sort();
    const missing = WORKSPACE_WHEEL_PREFIXES.filter(
      (prefix) => !wheelNames.some((name) => name.startsWith(prefix))
    );
    if (missing.length > 0 || wheelNames.length !== WORKSPACE_WHEEL_PREFIXES.length) {
      throw new Error(
        `Expected workspace wheels ${WORKSPACE_WHEEL_PREFIXES.join(', ')}, found ${wheelNames.join(', ') || 'none'}.`
      );
    }
    execRequired(
      runtimePython,
      [...PIP_INSTALL_WHEEL_ARGS, ...wheelNames.map((name) => path.join(wheelDirectory, name))],
      {
        cwd: AGENTS_ROOT,
        env: {
          ...process.env,
          PIP_DISABLE_PIP_VERSION_CHECK: '1',
          PYTHONNOUSERSITE: '1',
        },
      }
    );
  } finally {
    fs.rmSync(wheelDirectory, { recursive: true, force: true });
  }
}

function verifyInstalledLocalRuntimeMigrations(runtimePython) {
  execRequired(runtimePython, ['-c', VERIFY_MIGRATIONS_INLINE_SCRIPT], {
    cwd: AGENTS_ROOT,
    env: {
      ...process.env,
      PYTHONNOUSERSITE: '1',
    },
  });
}

function buildHelperRuntimeManifest() {
  return {
    python_version: PYTHON_VERSION,
    python_executable_relative_path: PYTHON_RUNTIME_EXECUTABLE_RELATIVE_PATH,
    python_executable_sha256: sha256File(
      requireFile(path.join(HELPER_RUNTIME_CACHE_ROOT, PYTHON_RUNTIME_EXECUTABLE_RELATIVE_PATH))
    ),
    python_standalone_release_tag: PYTHON_STANDALONE_RELEASE_TAG,
    python_standalone_archive_name: PYTHON_STANDALONE_ARCHIVE_NAME,
    python_standalone_archive_url: PYTHON_STANDALONE_ARCHIVE_URL,
    python_standalone_archive_sha256: PYTHON_STANDALONE_ARCHIVE_SHA256,
    uv_lock_sha256: sha256File(requireFile(UV_LOCK_PATH)),
  };
}

function writeHelperRuntimeManifest(manifestPath) {
  fs.writeFileSync(
    manifestPath,
    `${JSON.stringify(buildHelperRuntimeManifest(), null, 2)}\n`,
    {
      encoding: 'utf8',
      mode: 0o600,
    }
  );
}

function writeAppRuntimeManifest(manifestPath, runtimePython) {
  fs.writeFileSync(
    manifestPath,
    `${JSON.stringify(
      {
        python_path: path.resolve(runtimePython),
        python_version: PYTHON_VERSION,
        python_sha256: sha256File(requireFile(runtimePython)),
      },
      null,
      2
    )}\n`,
    {
      encoding: 'utf8',
      mode: 0o600,
    }
  );
}

function prepareLocalBackendHelperRuntime() {
  ensureSupportedBuildHost();
  requireFile(UV_LOCK_PATH);
  const paths = buildHelperRuntimePaths();
  ensureArchive(paths.archivePath);
  extractHelperRuntime(paths.archivePath, paths.helperRuntimeRoot);
  const requirementsPath = path.join(paths.helperRuntimeRoot, 'requirements.lock.txt');
  exportLockedRequirements(requirementsPath);
  installLockedDependencies(paths.helperRuntimeExecutable, requirementsPath);
  buildAndInstallAgentsPackage(paths.helperRuntimeExecutable);
  verifyInstalledLocalRuntimeMigrations(paths.helperRuntimeExecutable);
  fs.rmSync(requirementsPath, { force: true });
  compilePythonBytecode(paths.helperRuntimeExecutable, paths.helperRuntimeRoot);
  writeHelperRuntimeManifest(paths.helperRuntimeManifestPath);
  writeAppRuntimeManifest(paths.appRuntimeManifestPath, paths.helperRuntimeExecutable);
  process.stdout.write(`Prepared local backend helper runtime: ${paths.helperRuntimeRoot}\n`);
}

if (require.main === module) {
  try {
    prepareLocalBackendHelperRuntime();
  } catch (error) {
    console.error(String(error && error.stack ? error.stack : error));
    process.exitCode = 1;
  }
}

module.exports = {
  HELPER_RUNTIME_DIRNAME,
  HELPER_RUNTIME_MANIFEST_FILENAME,
  APP_RUNTIME_MANIFEST_FILENAME,
  PYTHON_VERSION,
  PYTHON_STANDALONE_RELEASE_TAG,
  PYTHON_STANDALONE_ARCHIVE_NAME,
  PYTHON_STANDALONE_ARCHIVE_URL,
  PYTHON_STANDALONE_ARCHIVE_SHA256,
  UV_EXPORT_ARGS,
  UV_BUILD_ARGS,
  BUNDLED_WORKSPACE_PACKAGES,
  WORKSPACE_WHEEL_PREFIXES,
  PIP_INSTALL_WHEEL_ARGS,
  buildHelperRuntimePaths,
  buildHelperRuntimeManifest,
  assertNoRuntimeSymlinks,
  copyExtractedRuntime,
  findRuntimeSymlinkPaths,
  compilePythonBytecode,
  prepareLocalBackendHelperRuntime,
  VERIFY_MIGRATIONS_INLINE_SCRIPT,
  PYTHON_RUNTIME_EXECUTABLE_RELATIVE_PATH,
};
