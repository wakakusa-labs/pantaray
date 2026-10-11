const assert = require('assert');
const { test } = require('node:test');

const { buildContentSecurityPolicy } = require('../electron/content_security_policy.js');

const REQUIRED_HARDENING_DIRECTIVES = [
  "base-uri 'none';",
  "object-src 'none';",
  "form-action 'none';",
  "frame-ancestors 'none';",
];

test('production CSP is fail-closed and allows only the configured API origin', () => {
  const csp = buildContentSecurityPolicy({
    isDev: false,
    apiOrigin: 'http://127.0.0.1:8005',
  });

  for (const directive of REQUIRED_HARDENING_DIRECTIVES) assert.ok(csp.includes(directive));
  assert.ok(csp.includes("script-src 'self';"));
  assert.ok(csp.includes("connect-src 'self' http://127.0.0.1:8005 ws://127.0.0.1:8005"));
  assert.ok(!csp.includes("script-src 'self' 'unsafe-inline';"));
});

test('development CSP permits only loopback development transports', () => {
  const csp = buildContentSecurityPolicy({ isDev: true, apiOrigin: null });

  for (const directive of REQUIRED_HARDENING_DIRECTIVES) assert.ok(csp.includes(directive));
  assert.ok(csp.includes('http://127.0.0.1:*'));
  assert.ok(csp.includes('http://localhost:*'));
  assert.ok(csp.includes("script-src 'self' 'unsafe-inline';"));
});

test('CSP rejects an API URL containing path or query data', () => {
  assert.throws(() =>
    buildContentSecurityPolicy({ isDev: false, apiOrigin: 'https://api.example.test/v1?token=x' })
  );
});

test('CSP rejects non-HTTP API URLs and embedded credentials', () => {
  assert.throws(
    () => buildContentSecurityPolicy({ isDev: false, apiOrigin: 'javascript:alert(1)' }),
    /HTTP\(S\) origin/
  );
  assert.throws(
    () =>
      buildContentSecurityPolicy({
        isDev: false,
        apiOrigin: 'https://user:password@api.example.test',
      }),
    /without credentials/
  );
});

test('CSP allows the stored-image scheme for images only', () => {
  for (const isDev of [false, true]) {
    const csp = buildContentSecurityPolicy({ isDev, apiOrigin: null });

    assert.ok(csp.includes("img-src 'self' data: blob: pantaray-image:;"));
    assert.ok(
      csp.includes(isDev ? "script-src 'self' 'unsafe-inline';" : "script-src 'self';"),
      'script-src must not gain the image scheme'
    );
    assert.ok(!csp.includes("default-src 'self' pantaray-image:"));
    assert.ok(!csp.includes('connect-src') || !csp.match(/connect-src[^;]*pantaray-image/));
  }
});

test('CSP allows the Action file scheme for frames only', () => {
  for (const isDev of [false, true]) {
    const csp = buildContentSecurityPolicy({ isDev, apiOrigin: null });

    assert.ok(csp.includes("frame-src 'self' pantaray-action-file:;"));
    assert.ok(csp.includes("object-src 'none';"));
    for (const directive of csp.split(';').filter((part) => !part.includes('frame-src'))) {
      assert.ok(!directive.includes('pantaray-action-file'), directive);
    }
  }
});

test('connect-src names Supabase only while account login is enabled', () => {
  for (const isDev of [false, true]) {
    const params = { isDev, apiOrigin: 'http://127.0.0.1:8005' };
    assert.ok(
      !buildContentSecurityPolicy({ ...params, accountLoginEnabled: false }).includes('supabase')
    );
    assert.ok(
      buildContentSecurityPolicy({ ...params, accountLoginEnabled: true }).includes(
        'https://*.supabase.co wss://*.supabase.co;'
      )
    );
  }
});
