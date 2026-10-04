const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { test } = require('node:test');

const {
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
  VERIFY_MIGRATIONS_INLINE_SCRIPT,
  buildHelperRuntimePaths,
  copyExtractedRuntime,
  findRuntimeSymlinkPaths,
  compilePythonBytecode,
} = require('../scripts/prepare-local-backend-helper-runtime.js');

function makeTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

test('helper runtime archive spec is pinned to a single bundled CPython release', () => {
  assert.equal(PYTHON_VERSION, '3.12.13');
  assert.equal(PYTHON_STANDALONE_RELEASE_TAG, '20260325');
  assert.equal(
    PYTHON_STANDALONE_ARCHIVE_NAME,
    'cpython-3.12.13+20260325-aarch64-apple-darwin-install_only_stripped.tar.gz'
  );
  assert.equal(
    PYTHON_STANDALONE_ARCHIVE_URL,
    'https://github.com/astral-sh/python-build-standalone/releases/download/20260325/' +
      'cpython-3.12.13%2B20260325-aarch64-apple-darwin-install_only_stripped.tar.gz'
  );
  assert.match(PYTHON_STANDALONE_ARCHIVE_SHA256, /^[0-9a-f]{64}$/);
});

test('helper runtime paths resolve to packaged resource locations', () => {
  const paths = buildHelperRuntimePaths();
  assert.equal(
    paths.helperRuntimeRoot,
    path.join(
      path.resolve(__dirname, '..'),
      '.cache',
      HELPER_RUNTIME_DIRNAME
    )
  );
  assert.equal(
    paths.helperRuntimeExecutable,
    path.join(paths.helperRuntimeRoot, 'bin', 'python3')
  );
  assert.equal(
    paths.helperRuntimeManifestPath,
    path.join(paths.helperRuntimeRoot, HELPER_RUNTIME_MANIFEST_FILENAME)
  );
  assert.equal(
    paths.appRuntimeManifestPath,
    path.join(paths.helperRuntimeRoot, APP_RUNTIME_MANIFEST_FILENAME)
  );
});

test('helper runtime exports only local backend dependencies from uv.lock', () => {
  assert.equal(UV_EXPORT_ARGS.includes('--extra'), false);
  assert.equal(UV_EXPORT_ARGS.includes('cloud_backend'), false);
  assert.equal(UV_EXPORT_ARGS.includes('pantaray-agents'), true);
});

test('helper runtime builds the local package before offline pip installation', () => {
  assert.equal(UV_EXPORT_ARGS.includes('--no-emit-workspace'), true);
  assert.deepEqual(UV_BUILD_ARGS, ['--quiet', 'build', '--wheel']);
  assert.deepEqual(BUNDLED_WORKSPACE_PACKAGES, ['pantaray-agents', 'pantaray-llm']);
  assert.deepEqual(WORKSPACE_WHEEL_PREFIXES, ['pantaray_agents-', 'pantaray_llm-']);
  assert.deepEqual(PIP_INSTALL_WHEEL_ARGS, ['-m', 'pip', 'install', '--no-index', '--no-deps']);
});

test('helper runtime verifies migrations and sqlite-vec after installation', () => {
  assert.match(
    VERIFY_MIGRATIONS_INLINE_SCRIPT,
    /load_default_migrations/
  );
  assert.match(
    VERIFY_MIGRATIONS_INLINE_SCRIPT,
    /0001_core_runtime\.sql/
  );
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /import sqlite_vec/);
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /import pypdfium2/);
  assert.match(
    VERIFY_MIGRATIONS_INLINE_SCRIPT,
    /import pantaray_agents\.agents\.action_agent\.runtime\.agents_md/
  );
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /enable_load_extension\(True\)/);
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /enable_load_extension\(False\)/);
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /vec_version\(\)/);
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /v0\.1\.9/);
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /DISTANCE_METRIC=cosine/);
  assert.match(VERIFY_MIGRATIONS_INLINE_SCRIPT, /embedding MATCH \?/);
});

test('copyExtractedRuntime materializes relative and absolute runtime symlinks', () => {
  const sourceRoot = path.join(makeTempDir('helper-runtime-source-'), 'python');
  const destinationRoot = path.join(makeTempDir('helper-runtime-destination-'), 'python');
  const binDir = path.join(sourceRoot, 'bin');
  const pkgconfigDir = path.join(sourceRoot, 'lib', 'pkgconfig');

  fs.mkdirSync(binDir, { recursive: true });
  fs.mkdirSync(pkgconfigDir, { recursive: true });
  fs.writeFileSync(path.join(binDir, 'python3.12'), 'python-binary', 'utf8');
  fs.writeFileSync(
    path.join(pkgconfigDir, 'python-3.12-embed.pc'),
    'pkg-config',
    'utf8'
  );
  fs.symlinkSync('python3.12', path.join(binDir, 'python3'));
  fs.symlinkSync(
    path.join(pkgconfigDir, 'python-3.12-embed.pc'),
    path.join(pkgconfigDir, 'python3-embed.pc')
  );

  copyExtractedRuntime(sourceRoot, destinationRoot);

  const pythonPath = path.join(destinationRoot, 'bin', 'python3');
  const pkgconfigPath = path.join(
    destinationRoot,
    'lib',
    'pkgconfig',
    'python3-embed.pc'
  );
  assert.equal(fs.lstatSync(pythonPath).isSymbolicLink(), false);
  assert.equal(fs.lstatSync(pkgconfigPath).isSymbolicLink(), false);
  assert.equal(fs.readFileSync(pythonPath, 'utf8'), 'python-binary');
  assert.equal(fs.readFileSync(pkgconfigPath, 'utf8'), 'pkg-config');
  assert.deepEqual(findRuntimeSymlinkPaths(destinationRoot), []);
});

test('copyExtractedRuntime rejects symlink targets outside the extracted runtime', () => {
  const sourceRoot = path.join(makeTempDir('helper-runtime-source-'), 'python');
  const destinationRoot = path.join(makeTempDir('helper-runtime-destination-'), 'python');
  const outsideFile = path.join(makeTempDir('helper-runtime-outside-'), 'outside.txt');

  fs.mkdirSync(path.join(sourceRoot, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(sourceRoot, 'bin', 'python3'), 'python-binary', 'utf8');
  fs.writeFileSync(outsideFile, 'outside', 'utf8');
  fs.symlinkSync(outsideFile, path.join(sourceRoot, 'bin', 'outside-link'));

  assert.throws(
    () => copyExtractedRuntime(sourceRoot, destinationRoot),
    /escapes runtime root/
  );
});

test('copyExtractedRuntime rejects symlink loops explicitly', () => {
  const sourceRoot = path.join(makeTempDir('helper-runtime-source-'), 'python');
  const destinationRoot = path.join(makeTempDir('helper-runtime-destination-'), 'python');
  const loopDir = path.join(sourceRoot, 'lib', 'loop');

  fs.mkdirSync(path.join(sourceRoot, 'bin'), { recursive: true });
  fs.mkdirSync(loopDir, { recursive: true });
  fs.writeFileSync(path.join(sourceRoot, 'bin', 'python3'), 'python-binary', 'utf8');
  fs.symlinkSync('b', path.join(loopDir, 'a'));
  fs.symlinkSync('a', path.join(loopDir, 'b'));

  assert.throws(
    () => copyExtractedRuntime(sourceRoot, destinationRoot),
    /Detected symlink cycle/
  );
});

// PEP 552: the flags word after the magic number. 0b01 is hash-based bytecode
// whose source hash the import system does not check.
const UNCHECKED_HASH_PYC_FLAGS = 0b01;

function readPycFlags(pycPath) {
  return fs.readFileSync(pycPath).readUInt32LE(4);
}

test('compilePythonBytecode ships unchecked-hash bytecode for every module', () => {
  const runtimeRoot = path.join(makeTempDir('helper-runtime-bytecode-'), 'python');
  const packageDir = path.join(runtimeRoot, 'lib', 'python3', 'site-packages', 'pkg');
  const installedModule = path.join(packageDir, 'installed.py');
  const freshModule = path.join(packageDir, 'fresh.py');
  fs.mkdirSync(packageDir, { recursive: true });
  fs.writeFileSync(installedModule, 'VALUE = 1\n', 'utf8');
  fs.writeFileSync(freshModule, 'VALUE = 2\n', 'utf8');
  // pip leaves timestamp-based bytecode behind for what it installs.
  execFileSync('python3', ['-I', '-m', 'py_compile', installedModule]);

  compilePythonBytecode('python3', runtimeRoot);

  const pycacheDir = path.join(packageDir, '__pycache__');
  const pycNames = fs.readdirSync(pycacheDir).sort();
  assert.deepEqual(
    pycNames.map((name) => name.split('.')[0]),
    ['fresh', 'installed']
  );
  for (const name of pycNames) {
    assert.equal(readPycFlags(path.join(pycacheDir, name)), UNCHECKED_HASH_PYC_FLAGS, name);
  }
});

test('compilePythonBytecode fails the build when a module does not compile', () => {
  const runtimeRoot = path.join(makeTempDir('helper-runtime-bytecode-'), 'python');
  fs.mkdirSync(runtimeRoot, { recursive: true });
  fs.writeFileSync(path.join(runtimeRoot, 'broken.py'), 'def broken(:\n', 'utf8');

  assert.throws(() => compilePythonBytecode('python3', runtimeRoot));
});
