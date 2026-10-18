/**
 * External URL open policy
 *
 * 目的:
 * - renderer からの任意URL openExternal を fail-closed で制御する。
 * - main_legacy.js から切り出し、責務境界（security）を明確にする。
 */

import net from 'net';

export type ExternalUrlValidationResult = { ok: true; url: string } | { ok: false; reason: string };

export type ExternalUrlPolicyOptions = {
  isDev: boolean;
  webAppOrigin: string | null;
};

function isPrivateIp(ip: string): boolean {
  // NOTE: net.isIP は "1.2.3.4" / "::1" などの literal のみ判定する。
  const v = net.isIP(ip);
  if (!v) return false;
  const s = String(ip);
  if (v === 6) {
    // loopback / link-local / unique local (ざっくり)
    const lower = s.toLowerCase();
    return (
      lower === '::1' ||
      lower.startsWith('fe80:') ||
      lower.startsWith('fc') ||
      lower.startsWith('fd')
    );
  }
  // IPv4
  if (s === '127.0.0.1') return true;
  if (s.startsWith('10.')) return true;
  if (s.startsWith('192.168.')) return true;
  if (s.startsWith('169.254.')) return true;
  const m = s.match(/^172\.(\d+)\./);
  if (m) {
    const n = Number(m[1]);
    if (Number.isFinite(n) && n >= 16 && n <= 31) return true;
  }
  return false;
}

export function validateExternalUrl(
  rawUrl: unknown,
  opts: ExternalUrlPolicyOptions
): ExternalUrlValidationResult {
  const isDev = Boolean(opts.isDev);
  const urlStr = String(rawUrl || '').trim();
  if (!urlStr) return { ok: false, reason: 'empty' };
  if (urlStr.length > 2048) return { ok: false, reason: 'too_long' };

  let u: URL;
  try {
    u = new URL(urlStr);
  } catch {
    return { ok: false, reason: 'invalid_url' };
  }

  const protocol = String(u.protocol || '').toLowerCase();
  const allowHttp = isDev; // devのみ http 許容
  if (protocol !== 'https:' && !(allowHttp && protocol === 'http:')) {
    return { ok: false, reason: 'protocol_not_allowed' };
  }

  // user:pass@ のような userinfo は禁止
  if (u.username || u.password) return { ok: false, reason: 'userinfo_not_allowed' };

  const host = String(u.hostname || '').toLowerCase();
  if (!host) return { ok: false, reason: 'missing_host' };

  const webAppOrigin = opts.webAppOrigin;
  if (!webAppOrigin) return { ok: false, reason: 'web_app_origin_not_configured' };

  // 本番で localhost / private IP は拒否
  if (!isDev) {
    if (host === 'localhost' || host === '127.0.0.1' || host === '::1') {
      return { ok: false, reason: 'local_host_not_allowed' };
    }
    if (isPrivateIp(host)) return { ok: false, reason: 'private_ip_not_allowed' };
  }

  if (u.origin !== webAppOrigin) return { ok: false, reason: 'origin_not_allowed' };

  return { ok: true, url: u.toString() };
}
