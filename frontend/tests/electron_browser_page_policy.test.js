const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const {
  decideBrowserRequest,
  decideFileAccess,
  foldPath,
  isSecretPath,
  isWithinRoot,
} = require('../electron/dist/browser/pagePolicy.js');
const { BROWSER_MAX_CONTACTED_HOSTS } = require('../electron/dist/browser/browserContract.js');

/** The runtime's rules for one home folder, its folds and its verdicts (Python reads it too). */
const SECRET_PATHS = JSON.parse(
  fs.readFileSync(path.join(__dirname, '../shared/secret_paths.fixtures.json'), 'utf8')
);

const notSameDirectory = () => false;

test('file access mirrors PrivateAppStorage.hides() under the read roots', () => {
  const scope = {
    fileRoots: ['/work'],
    privateRoots: ['/work/.pantaray'],
    privateRootExceptions: ['/work/.pantaray/published'],
  };
  const decide = (target) => decideFileAccess(target, scope, notSameDirectory);
  assert.equal(decide('/work/deck/img.png'), null);
  assert.equal(decide('/work'), null);
  assert.equal(decide('/workshop/img.png'), 'outside_read_scope');
  assert.equal(decide('/etc/passwd'), 'outside_read_scope');
  assert.equal(decide('/work/.pantaray/db.sqlite'), 'outside_read_scope');
  assert.equal(decide('/work/.pantaray'), 'outside_read_scope');
  assert.equal(decide('/work/.pantaray/published/chart.png'), null);
  assert.equal(
    decideFileAccess('/Users/me/secret.txt', { ...scope, fileRoots: ['/'] }, notSameDirectory),
    null
  );
});

test('a private root reached under another letter case is still private', () => {
  const sameDirectory = (left, right) => left.toLowerCase() === right.toLowerCase();
  const scope = {
    fileRoots: ['/'],
    privateRoots: ['/Users/me/Library/Pantaray'],
    privateRootExceptions: [],
  };
  assert.equal(
    decideFileAccess('/Users/me/library/pantaray/db.sqlite', scope, sameDirectory),
    'outside_read_scope'
  );
  // The name matches but the directory is another one.
  assert.equal(
    isWithinRoot('/other/pantaray/x', '/Users/me/Library/Pantaray', () => false),
    false
  );
});

test('a path folds as the runtime folds it', () => {
  for (const [text, folded] of SECRET_PATHS.folds) {
    assert.equal(foldPath(text), folded, JSON.stringify(text));
  }
});

test("the runtime's secret rules withhold here what its file tools withhold", () => {
  for (const [target, withheld] of SECRET_PATHS.cases) {
    assert.equal(isSecretPath([target], SECRET_PATHS.rules), withheld, JSON.stringify(target));
  }
});

const BACKEND_PORT = 8765;

function decide(url, method, { allowNetwork = true, writeHosts = [], contacted = [] } = {}) {
  return decideBrowserRequest(
    { url, method },
    {
      policy: { allow_network: allowNetwork, write_hosts: writeHosts },
      backendPort: BACKEND_PORT,
      contacted,
    }
  );
}

const refused = (reason) => ({ allowed: false, reason });
const reached = (host, port) => ({ allowed: true, reached: { host, port } });
const local = { allowed: true, reached: null };

// Catches: a request leaving the machine with the network off, loopback and WebSockets
// included, or a local document refused as if it were network.
test('with the network off nothing leaves the page, loopback included', () => {
  const cases = [
    ['https://example.com/', 'GET', refused('network_off')],
    ['http://localhost:3000/', 'GET', refused('network_off')],
    ['http://127.0.0.1:3000/api', 'POST', refused('network_off')],
    ['http://[::1]:3000/', 'GET', refused('network_off')],
    ['ws://localhost:3000/hmr', 'GET', refused('network_off')],
    ['wss://example.com/', 'GET', refused('network_off')],
    ['file:///work/deck.html', 'GET', local],
    ['data:image/png;base64,AA==', 'GET', local],
    ['blob:file:///0b1c', 'GET', local],
    ['about:blank', 'GET', local],
    ['not a url', 'GET', refused('not_allowed')],
  ];
  for (const [url, method, expected] of cases) {
    assert.deepEqual(decide(url, method, { allowNetwork: false }), expected, `${method} ${url}`);
  }
});

// Catches: a page reaching the backend's own API through any name for this machine, or a
// local dev server refused a write or its live-reload socket.
test('with the network on, loopback takes every method except at the backend', () => {
  const cases = [
    ['http://localhost:3000/', 'GET', reached('localhost', 3000)],
    ['http://localhost:3000/api/items', 'POST', reached('localhost', 3000)],
    ['http://127.0.0.1:5173/api', 'DELETE', reached('127.0.0.1', 5173)],
    ['http://[::1]:8080/', 'PUT', reached('::1', 8080)],
    ['http://app.localhost:3000/', 'PATCH', reached('app.localhost', 3000)],
    ['ws://localhost:5173/hmr', 'GET', reached('localhost', 5173)],
    ['http://LOCALHOST/', 'POST', reached('localhost', 80)],
    [`http://127.0.0.1:${BACKEND_PORT}/local/health`, 'GET', refused('not_allowed')],
    [`http://localhost:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`http://localhost.:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`http://[::1]:${BACKEND_PORT}/`, 'POST', refused('not_allowed')],
    [`http://127.1:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`http://2130706433:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`http://0.0.0.0:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`http://[::ffff:127.0.0.1]:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`http://api.localhost:${BACKEND_PORT}/`, 'GET', refused('not_allowed')],
    [`ws://127.0.0.1:${BACKEND_PORT}/ws`, 'GET', refused('not_allowed')],
  ];
  for (const [url, method, expected] of cases) {
    assert.deepEqual(decide(url, method), expected, `${method} ${url}`);
  }
});

// Catches: a page sending data to an external host it was not approved to write to, by
// method or WebSocket, a name that only looks like loopback treated as loopback, or an
// approved host missed by its letter case or IPv6 brackets.
test('an external host takes reads, and writes only once approved', () => {
  const approved = { writeHosts: ['Forms.Example.com', '2001:db8::1'] };
  const cases = [
    ['https://example.com/', 'GET', {}, reached('example.com', 443)],
    ['https://example.com/', 'HEAD', {}, reached('example.com', 443)],
    ['https://example.com/api', 'OPTIONS', {}, reached('example.com', 443)],
    ['http://Example.COM:8080/a', 'GET', {}, reached('example.com', 8080)],
    ['https://example.com/api', 'POST', {}, refused('write_not_approved')],
    ['https://example.com/api', 'PUT', {}, refused('write_not_approved')],
    ['https://example.com/api', 'DELETE', {}, refused('write_not_approved')],
    ['wss://example.com/live', 'GET', {}, refused('write_not_approved')],
    ['ws://example.com/live', 'GET', {}, refused('write_not_approved')],
    ['https://127.0.0.1.nip.example/api', 'POST', {}, refused('write_not_approved')],
    ['https://localhost.example/api', 'POST', {}, refused('write_not_approved')],
    ['https://forms.example.com/submit', 'POST', approved, reached('forms.example.com', 443)],
    ['wss://forms.example.com/live', 'GET', approved, reached('forms.example.com', 443)],
    ['http://[2001:DB8::1]/submit', 'POST', approved, reached('2001:db8::1', 80)],
    ['https://other.example.com/submit', 'POST', approved, refused('write_not_approved')],
    ['ftp://example.com/a', 'GET', {}, refused('not_allowed')],
    ['chrome://settings', 'GET', {}, refused('not_allowed')],
  ];
  for (const [url, method, options, expected] of cases) {
    assert.deepEqual(decide(url, method, options), expected, `${method} ${url}`);
  }
});

// Catches: a host reached without being reported past the cap, a host already reported
// refused, or a refused write counted against the cap.
test('past the host cap a new host is refused and a reported one still loads', () => {
  const contacted = Array.from({ length: BROWSER_MAX_CONTACTED_HOSTS }, (_, i) => ({
    host: `h${i}.example`,
    port: 443,
  }));
  assert.equal(contacted.length, 50);
  assert.deepEqual(
    decide('https://h50.example/a.js', 'GET', { contacted }),
    refused('too_many_hosts')
  );
  assert.deepEqual(
    decide('http://localhost:3000/', 'GET', { contacted }),
    refused('too_many_hosts')
  );
  // The same name on another port is another host.
  assert.deepEqual(
    decide('http://h0.example/a.js', 'GET', { contacted }),
    refused('too_many_hosts')
  );
  assert.deepEqual(
    decide('https://h0.example/a.js', 'GET', { contacted }),
    reached('h0.example', 443)
  );
  assert.deepEqual(decide('file:///work/a.png', 'GET', { contacted }), local);
  assert.deepEqual(
    decide('https://h0.example/api', 'POST', { contacted }),
    refused('write_not_approved')
  );
  assert.deepEqual(
    decide('https://h49.example/a.js', 'GET', { contacted: contacted.slice(0, 49) }),
    reached('h49.example', 443)
  );
});
