const assert = require('assert');
const { test } = require('node:test');

const { createDeepLinkManager } = require('../electron/dist/protocol/deepLink.js');

// A `pantaray://` link clicked in another app: a task's or a suggestion's link shows it in the main
// window, the login callback still completes, and anything else only brings the app forward.

function createHarness() {
  const listeners = new Map();
  const steps = [];
  const app = {
    requestSingleInstanceLock: () => true,
    quit: () => steps.push('quit'),
    on: (name, listener) => listeners.set(name, listener),
  };
  const mainWindow = {
    isDestroyed: () => false,
    isVisible: () => true,
    show: () => steps.push('show'),
    focus: () => steps.push('focus'),
  };
  const manager = createDeepLinkManager({
    app,
    getMainWindow: () => mainWindow,
    handleCallback: (payload) => {
      steps.push(['auth', payload]);
      return { ok: true };
    },
    showItem: async (item) => {
      steps.push(['showItem', item]);
    },
  });
  manager.installAppHandlers();
  const openUrl = (url) => listeners.get('open-url')({ preventDefault: () => {} }, url);
  const secondInstance = (argv) => listeners.get('second-instance')({}, argv);
  return { manager, steps, openUrl, secondInstance };
}

test('a task or suggestion link shows that item in the main window', () => {
  const { manager, steps, openUrl, secondInstance } = createHarness();
  manager.handleStartupArgs(['/Applications/Pantaray.app']);

  openUrl('pantaray://task/3f86-1ab4');
  secondInstance(['Pantaray.exe', 'pantaray://suggestion/s_1']);

  assert.deepStrictEqual(steps, [
    ['showItem', { kind: 'action', id: '3f86-1ab4' }],
    ['showItem', { kind: 'suggestion', id: 's_1' }],
    'focus',
  ]);
});

test('any other link, or a link with more than an id, only brings the app forward', () => {
  const { manager, steps, openUrl, secondInstance } = createHarness();
  manager.handleStartupArgs([]);

  for (const url of [
    'pantaray://task/',
    'pantaray://task/a-1/../../settings',
    'pantaray://task/a-1?x=1',
    'pantaray://task/a%2F1',
    'pantaray://folder/a-1',
    ' pantaray://task/a-1',
    'pantaray://task/a-1 pantaray://task/a-2',
    'https://example.com/pantaray://task/a-1',
  ]) {
    openUrl(url);
  }
  secondInstance(['Pantaray.exe', 'pantaray://task/a-1#x']);

  assert.deepStrictEqual(steps, ['focus']);
});

test('the login callback still completes', () => {
  const { manager, steps, openUrl } = createHarness();
  // Before startup too: the auth coordinator holds a callback until it can apply it.
  openUrl('pantaray://auth?attempt_id=attempt-1&exchange_code=exchange-1');
  manager.handleStartupArgs([]);

  assert.deepStrictEqual(steps, [
    [
      'auth',
      { transport: 'deep_link', attemptId: 'attempt-1', exchangeCode: 'exchange-1' },
    ],
    'focus',
  ]);
});

test('a link that launches the app waits for startup, and the latest one is shown', () => {
  const { manager, steps, openUrl } = createHarness();
  openUrl('pantaray://task/a-1');
  openUrl('pantaray://suggestion/s-2');
  assert.deepStrictEqual(steps, []);

  manager.handleStartupArgs(['/Applications/Pantaray.app']);
  assert.deepStrictEqual(steps, [['showItem', { kind: 'suggestion', id: 's-2' }]]);
});

test('a link in the launch arguments is shown at startup', () => {
  const { manager, steps } = createHarness();
  manager.handleStartupArgs(['Pantaray.exe', 'pantaray://task/a-1']);
  assert.deepStrictEqual(steps, [['showItem', { kind: 'action', id: 'a-1' }]]);
});
