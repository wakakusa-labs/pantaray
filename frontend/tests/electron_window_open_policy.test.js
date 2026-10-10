const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');

const {
  keepWindowOnItsDocument,
  openNewWindowsInDefaultBrowser,
} = require('../electron/dist/security/windowOpenPolicy.js');

function openFromPage(url) {
  const app = new EventEmitter();
  const opened = [];
  openNewWindowsInDefaultBrowser({
    app,
    openExternal: async (target) => {
      opened.push(target);
    },
    logger: null,
  });
  let handler = null;
  app.emit('web-contents-created', {}, { setWindowOpenHandler: (fn) => (handler = fn) });
  return { result: handler({ url }), opened };
}

test('a web link from any page opens in the default browser, never in an app window', () => {
  const { result, opened } = openFromPage('https://example.com/docs');
  assert.deepEqual(result, { action: 'deny' });
  assert.deepEqual(opened, ['https://example.com/docs']);
});

test('a non-web link is neither opened externally nor in an app window', () => {
  for (const url of ['file:///etc/passwd', 'javascript:alert(1)', 'pantaray://auth', 'not a url']) {
    const { result, opened } = openFromPage(url);
    assert.deepEqual(result, { action: 'deny' });
    assert.deepEqual(opened, []);
  }
});

function navigateFrom(currentUrl, url) {
  const contents = new EventEmitter();
  contents.getURL = () => currentUrl;
  keepWindowOnItsDocument(contents);
  let prevented = false;
  contents.emit('will-navigate', { preventDefault: () => (prevented = true) }, url);
  return prevented ? 'dropped' : 'allowed';
}

test('a link that would load another document in the window is dropped', () => {
  const dev = 'http://localhost:3011/#/history';
  // A relative link in an answer resolves against the app's own address.
  assert.equal(navigateFrom(dev, 'http://localhost:3011/report/Xer.html'), 'dropped');
  assert.equal(navigateFrom(dev, 'https://example.com/'), 'dropped');
  assert.equal(navigateFrom(dev, 'file:///etc/passwd'), 'dropped');
  const packaged =
    'file:///Applications/Pantaray.app/Contents/Resources/app.asar/dist/index.html#/';
  assert.equal(
    navigateFrom(
      packaged,
      'file:///Applications/Pantaray.app/Contents/Resources/app.asar/dist/a.html'
    ),
    'dropped'
  );
});

test('the document the window loaded stays reachable: a reload and a hash route', () => {
  assert.equal(
    navigateFrom('http://localhost:3011/#/history', 'http://localhost:3011/'),
    'allowed'
  );
  assert.equal(
    navigateFrom('http://localhost:3011/#/history', 'http://localhost:3011/#/settings'),
    'allowed'
  );
  const packaged = 'file:///Applications/Pantaray.app/Contents/Resources/app.asar/dist/index.html';
  assert.equal(navigateFrom(`${packaged}#/`, `${packaged}#/chat`), 'allowed');
});
