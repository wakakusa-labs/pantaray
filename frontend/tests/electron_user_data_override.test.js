const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  DEV_USER_DATA_DIR_ENV,
  applyDevUserDataDirOverride,
} = require('../electron/dist/runtime/userDataOverride.js');

function createApp({ isPackaged }) {
  const calls = [];
  return {
    app: {
      isPackaged,
      setPath: (name, value) => {
        calls.push({ name, value });
      },
      setAppLogsPath: (value) => {
        calls.push({ name: 'logs', value });
      },
    },
    calls,
  };
}

test('applyDevUserDataDirOverride は development で userData とログの置き場所を env 指定先へ差し替える', () => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-user-data-'));
  const { app, calls } = createApp({ isPackaged: false });

  const applied = applyDevUserDataDirOverride({
    app,
    env: { [DEV_USER_DATA_DIR_ENV]: userDataDir },
  });

  assert.equal(applied, userDataDir);
  assert.deepEqual(calls, [
    { name: 'userData', value: userDataDir },
    { name: 'logs', value: path.join(userDataDir, 'logs') },
  ]);
});

test('applyDevUserDataDirOverride は missing directory を作成する', () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-user-data-parent-'));
  const userDataDir = path.join(parent, 'profile');
  const { app } = createApp({ isPackaged: false });

  applyDevUserDataDirOverride({
    app,
    env: { [DEV_USER_DATA_DIR_ENV]: userDataDir },
  });

  assert.equal(fs.statSync(userDataDir).isDirectory(), true);
});

test('applyDevUserDataDirOverride は packaged app では env を無視する', () => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-user-data-'));
  const { app, calls } = createApp({ isPackaged: true });

  const applied = applyDevUserDataDirOverride({
    app,
    env: { [DEV_USER_DATA_DIR_ENV]: userDataDir },
  });

  assert.equal(applied, null);
  assert.deepEqual(calls, []);
});

test('applyDevUserDataDirOverride は相対 path を拒否する', () => {
  const { app } = createApp({ isPackaged: false });

  assert.throws(
    () =>
      applyDevUserDataDirOverride({
        app,
        env: { [DEV_USER_DATA_DIR_ENV]: 'relative-profile' },
      }),
    /PANTARAY_USER_DATA_DIR must be an absolute path/
  );
});
