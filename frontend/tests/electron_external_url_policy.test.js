const assert = require('assert');
const { test } = require('node:test');

const { validateExternalUrl } = require('../electron/dist/security/externalUrlPolicy.js');

test('externalUrlPolicy: missing webAppOrigin fails closed', () => {
  const res = validateExternalUrl('https://example.com', { isDev: false, webAppOrigin: null });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'web_app_origin_not_configured');
});

test('externalUrlPolicy: rejects invalid URL', () => {
  const res = validateExternalUrl('not-a-url', { isDev: true, webAppOrigin: 'https://example.com' });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'invalid_url');
});

test('externalUrlPolicy: rejects userinfo', () => {
  const res = validateExternalUrl('https://u:p@example.com', { isDev: true, webAppOrigin: 'https://example.com' });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'userinfo_not_allowed');
});

test('externalUrlPolicy: rejects protocol in prod', () => {
  const res = validateExternalUrl('http://example.com', { isDev: false, webAppOrigin: 'http://example.com' });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'protocol_not_allowed');
});

test('externalUrlPolicy: allows http in dev when origin matches', () => {
  const res = validateExternalUrl('http://example.com/path', { isDev: true, webAppOrigin: 'http://example.com' });
  assert.equal(res.ok, true);
  assert.ok(res.url.includes('/path'));
});

test('externalUrlPolicy: rejects origin mismatch', () => {
  const res = validateExternalUrl('https://evil.example.com', { isDev: true, webAppOrigin: 'https://example.com' });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'origin_not_allowed');
});

test('externalUrlPolicy: rejects localhost in prod', () => {
  const res = validateExternalUrl('https://localhost/login', { isDev: false, webAppOrigin: 'https://localhost' });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'local_host_not_allowed');
});

test('externalUrlPolicy: rejects private ip in prod', () => {
  const res = validateExternalUrl('https://10.0.0.1/x', { isDev: false, webAppOrigin: 'https://10.0.0.1' });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'private_ip_not_allowed');
});

test('externalUrlPolicy: allows private ip in dev when origin matches', () => {
  const res = validateExternalUrl('http://10.0.0.1/x', { isDev: true, webAppOrigin: 'http://10.0.0.1' });
  assert.equal(res.ok, true);
});


