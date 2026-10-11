function parseApiOrigin(value) {
  if (!value) return null;
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('apiOrigin must be an HTTP(S) origin without credentials.');
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new Error('apiOrigin must contain only an origin.');
  }
  return url;
}

function buildContentSecurityPolicy({ isDev, apiOrigin, accountLoginEnabled }) {
  const apiUrl = parseApiOrigin(apiOrigin);
  const supabase = accountLoginEnabled ? ' https://*.supabase.co wss://*.supabase.co' : '';
  const connectSrc = isDev
    ? `connect-src 'self' ws://localhost:* http://localhost:* ws://127.0.0.1:* http://127.0.0.1:*${supabase};`
    : apiUrl
      ? `connect-src 'self' ${apiUrl.origin} ${apiUrl.protocol === 'https:' ? 'wss:' : 'ws:'}//${apiUrl.host}${supabase};`
      : `connect-src 'self'${supabase};`;

  return [
    "default-src 'self';",
    "base-uri 'none';",
    "object-src 'none';",
    "form-action 'none';",
    "frame-ancestors 'none';",
    connectSrc,
    // pantaray-image: serves user-attached images out of the local artifact root. The scheme is
    // registered without bypassCSP, so it has to be named here to render at all.
    "img-src 'self' data: blob: pantaray-image:;",
    // pantaray-action-file: serves a PDF an Action names to the preview frame, which Chromium's
    // PDF viewer draws. Frames only; it never serves a script or a page.
    "frame-src 'self' pantaray-action-file:;",
    isDev ? "script-src 'self' 'unsafe-inline';" : "script-src 'self';",
    "style-src 'self' 'unsafe-inline';",
    "worker-src 'self' blob:;",
  ].join(' ');
}

module.exports = { buildContentSecurityPolicy };
