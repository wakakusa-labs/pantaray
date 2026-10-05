// Simulate a release build: stdout is silent, the file sink records error-level.
process.env.PANTARAY_LOG_SALT = process.env.PANTARAY_LOG_SALT || 'test-log-salt';
process.env.PANTARAY_PACKAGED = '1';
process.env.PANTARAY_LOG_LEVEL = 'silent';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  configureFileSink,
  isFileSinkEnabledFor,
  resetFileSinkForTests,
  ELECTRON_LOG_FILENAME,
} = require('../electron/log_file_sink');
const { createLogger } = require('../electron/logger');

function freshDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-logsink-'));
}

function readLogLines(dir) {
  const file = path.join(dir, ELECTRON_LOG_FILENAME);
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean);
}

test('records error to file but skips info, independent of silent stdout', () => {
  resetFileSinkForTests();
  const dir = freshDir();
  configureFileSink({ dir, level: 'error' });
  const logger = createLogger();

  logger.error('BOOM', { detail: 'x' });
  logger.info('NOISE', { detail: 'y' });

  const lines = readLogLines(dir);
  assert.equal(lines.length, 1);
  const payload = JSON.parse(lines[0]);
  assert.equal(payload.level, 'error');
  assert.equal(payload.evt, 'BOOM');
});

test('redacts sensitive values before writing to file', () => {
  resetFileSinkForTests();
  const dir = freshDir();
  configureFileSink({ dir, level: 'error' });
  const logger = createLogger();

  logger.error('AUTH_ERR', {
    access_token: 'super-secret-value',
    url: 'https://api.example.com/users/abc?token=xyz',
  });

  const raw = fs.readFileSync(path.join(dir, ELECTRON_LOG_FILENAME), 'utf8');
  assert.equal(raw.includes('super-secret-value'), false);
  assert.equal(raw.includes('token=xyz'), false);
  assert.match(raw, /fp:/);
});

test('redacts secrets inside Error messages persisted to file', () => {
  resetFileSinkForTests();
  const dir = freshDir();
  configureFileSink({ dir, level: 'error' });
  const logger = createLogger();

  logger.error('REQUEST_FAILED', {
    err: new Error('fetch failed: https://api.example.com/x?token=super-secret-xyz'),
  });

  const raw = fs.readFileSync(path.join(dir, ELECTRON_LOG_FILENAME), 'utf8');
  assert.equal(raw.includes('super-secret-xyz'), false);
  assert.match(raw, /<redacted>/);
});

test('rotates when exceeding the size cap and caps file count', () => {
  resetFileSinkForTests();
  const dir = freshDir();
  configureFileSink({ dir, level: 'error' });
  const logger = createLogger();

  // 5 MiB cap; ~1 MiB per line forces several rotations.
  const bigDetail = 'x'.repeat(1024 * 1024);
  for (let i = 0; i < 20; i += 1) {
    logger.error('FILL', { detail: bigDetail });
  }

  assert.equal(fs.existsSync(path.join(dir, `${ELECTRON_LOG_FILENAME}.1`)), true);
  const files = fs.readdirSync(dir).filter((name) => name.startsWith(ELECTRON_LOG_FILENAME));
  // active + at most (LOG_FILE_MAX_FILES - 1) rotated files = 5
  assert.ok(files.length <= 5, `expected <= 5 files, got ${files.length}`);
});

test('isFileSinkEnabledFor reflects the configured level', () => {
  resetFileSinkForTests();
  assert.equal(isFileSinkEnabledFor('error'), false); // unconfigured

  configureFileSink({ dir: freshDir(), level: 'error' });
  assert.equal(isFileSinkEnabledFor('error'), true);
  assert.equal(isFileSinkEnabledFor('warn'), false);
  assert.equal(isFileSinkEnabledFor('info'), false);
});

test('writes nothing while the sink is unconfigured', () => {
  resetFileSinkForTests();
  const dir = freshDir();
  const logger = createLogger();

  logger.error('SHOULD_NOT_PERSIST', {});

  assert.equal(isFileSinkEnabledFor('error'), false);
  assert.equal(readLogLines(dir).length, 0);
});

test('configureFileSink fails fast on invalid input or double configuration', () => {
  resetFileSinkForTests();
  assert.throws(() => configureFileSink({ dir: '', level: 'error' }), /non-empty/);

  resetFileSinkForTests();
  assert.throws(() => configureFileSink({ dir: freshDir(), level: 'verbose' }), /unknown level/);

  resetFileSinkForTests();
  configureFileSink({ dir: freshDir(), level: 'error' });
  assert.throws(() => configureFileSink({ dir: freshDir(), level: 'error' }), /exactly once/);
});

test('configureFileSink leaves sink disabled when logs directory cannot be created', () => {
  resetFileSinkForTests();
  const originalMkdirSync = fs.mkdirSync;
  try {
    fs.mkdirSync = () => {
      throw new Error('permission denied');
    };

    assert.doesNotThrow(() => configureFileSink({ dir: '/unwritable/logs', level: 'error' }));
    assert.equal(isFileSinkEnabledFor('error'), false);
  } finally {
    fs.mkdirSync = originalMkdirSync;
    resetFileSinkForTests();
  }
});
