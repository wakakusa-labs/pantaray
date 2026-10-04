const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { test } = require('node:test');

// NOTE:
// - IPC allowlist は Electron(main)/preload のセキュリティ境界の中核。
// - ここでは「許可/拒否が意図通り」かつ「リストがズレない」ことを回帰テストで固定する。

const channels = require('../electron/dist/ipc/channels.js');
const bridge = require('../electron/dist/ipc/bridge.js');

function assertUnique(list, name) {
  const items = Array.from(list || []);
  const set = new Set(items);
  assert.equal(set.size, items.length, `${name} has duplicated entries`);
}

test('IPC allowlist: channel lists are unique', () => {
  assertUnique(channels.validSendChannels, 'validSendChannels');
  assertUnique(channels.validReceiveChannels, 'validReceiveChannels');
  assertUnique(channels.validInvokeChannels, 'validInvokeChannels');
});

test('IPC allowlist: assertValidInvokeChannel allows only known invoke channels', () => {
  assert.doesNotThrow(() => bridge.assertValidInvokeChannel('window:getPosition'));
  assert.doesNotThrow(() => bridge.assertValidInvokeChannel('auth:getState'));

  assert.throws(() => bridge.assertValidInvokeChannel('evil:invoke'), /Unauthorized IPC channel/i);
});

test('IPC allowlist: isValidSendChannel / isValidReceiveChannel', () => {
  assert.equal(bridge.isValidSendChannel('ws:send'), true);
  assert.equal(bridge.isValidSendChannel('ws:event'), false);

  assert.equal(bridge.isValidReceiveChannel('ws:event'), true);
  assert.equal(bridge.isValidReceiveChannel('ws:send'), false);
});

test('IPC preload exposes workspace and shortcut settings', () => {
  const { createSettingsApi } = require('../electron/preload/settings_api');
  const calls = [];
  const api = createSettingsApi({
    ipcRenderer: {
      invoke: (...args) => {
        calls.push(args);
        return Promise.resolve({});
      },
    },
  });

  void api.workspaceSettings.getCommandNetwork();
  void api.workspaceSettings.updateCommandNetwork(false);
  void api.workspaceSettings.getReadAccessScope();
  void api.workspaceSettings.updateReadAccessScope('workspace');
  void api.workspaceSettings.reorderProjects({ projectIds: ['project-b', 'project-a'] });
  void api.shortcut.getState();
  void api.shortcut.setAccelerator('Option+Space');

  assert.deepEqual(calls, [
    ['workspaceSettings:getCommandNetwork'],
    ['workspaceSettings:updateCommandNetwork', false],
    ['workspaceSettings:getReadAccessScope'],
    ['workspaceSettings:updateReadAccessScope', 'workspace'],
    ['workspaceSettings:reorderProjects', { projectIds: ['project-b', 'project-a'] }],
    ['shortcut:getState'],
    ['shortcut:setAccelerator', 'Option+Space'],
  ]);
});

// preload は renderer に晒す唯一の面。ここで使うチャンネルが allowlist から外れると、
// 「allowlist にない = main が拒否する」死んだ API が残るか、逆に allowlist だけが残る。
const PRELOAD_SOURCES = [
  'electron/preload.js',
  'electron/preload_overlay_interaction.js',
  ...fs
    .readdirSync(path.join(__dirname, '..', 'electron', 'preload'))
    .filter((entry) => entry.endsWith('.js'))
    .map((entry) => path.join('electron', 'preload', entry)),
];

// `domain:action` 形式のチャンネル名（CSS クラスなどの dash 形式は誤検知するので対象外）。
const NAMESPACED_CHANNEL = /^[a-z][A-Za-z0-9]*:[A-Za-z0-9]+$/;

function readPreloadSources() {
  return PRELOAD_SOURCES.map((relativePath) => ({
    relativePath,
    source: fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8'),
  }));
}

function collect(source, pattern) {
  return Array.from(source.matchAll(pattern), (match) => match[match.length - 1]);
}

test('IPC allowlist: preload が使うチャンネルはすべて allowlist に載っている', () => {
  const byKind = {
    invoke: channels.validInvokeChannels,
    send: channels.validSendChannels,
    receive: channels.validReceiveChannels,
  };

  for (const { relativePath, source } of readPreloadSources()) {
    const used = {
      invoke: [
        ...collect(source, /ipcRenderer\.invoke\(\s*'([^']+)'/g),
        ...collect(source, /assertValidInvokeChannel\(\s*'([^']+)'/g),
      ],
      send: [
        ...collect(source, /ipcRenderer\.send\(\s*'([^']+)'/g),
        ...collect(source, /isValidSendChannel\(\s*'([^']+)'/g),
        ...collect(source, /channel === '([^']+)'/g),
      ],
      receive: [
        ...collect(source, /ipcRenderer\.(?:on|removeListener)\(\s*'([^']+)'/g),
        ...collect(source, /isValidReceiveChannel\(\s*'([^']+)'/g),
        ...collect(source, /channel:\s*'([^']+)'/g),
      ],
    };
    for (const [kind, usedChannels] of Object.entries(used)) {
      for (const channel of usedChannels) {
        assert.ok(
          byKind[kind].includes(channel),
          `${relativePath} uses unlisted ${kind} channel: ${channel}`
        );
      }
    }

    // 間接参照で上のパターンから漏れても、名前空間付きチャンネル名は必ず allowlist にある。
    for (const literal of collect(source, /'([^'\s]+)'/g)) {
      if (!NAMESPACED_CHANNEL.test(literal)) continue;
      assert.ok(
        [...byKind.invoke, ...byKind.send, ...byKind.receive].includes(literal),
        `${relativePath} references unlisted channel-shaped literal: ${literal}`
      );
    }
  }
});

test('IPC allowlist: receive チャンネルには main 側の送信元がある', () => {
  const electronDir = path.join(__dirname, '..', 'electron');
  const sources = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === 'dist' || entry.name === 'preload') continue;
        walk(full);
        continue;
      }
      if (!entry.name.endsWith('.js') && !entry.name.endsWith('.ts')) continue;
      if (full.endsWith(path.join('src', 'ipc', 'channels.ts'))) continue;
      if (path.basename(full).startsWith('preload')) continue;
      sources.push(fs.readFileSync(full, 'utf8'));
    }
  };
  walk(electronDir);

  for (const channel of channels.validReceiveChannels) {
    assert.ok(
      sources.some((source) => source.includes(`'${channel}'`)),
      `no main-process sender for receive channel: ${channel}`
    );
  }
});
