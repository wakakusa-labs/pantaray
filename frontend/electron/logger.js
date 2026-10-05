const { createHash } = require('crypto');
const path = require('path');
const { LEVELS, normalizeLevel } = require('./log_levels');
const fileSink = require('./log_file_sink');

/**
 * Electron(main) 向けの安全なロガー。
 *
 * ポリシー:
 * - 配布版（packaged）は原則ログを出さない（no-op）
 * - 開発時もセンシティブ値は常にマスクする
 *
 * NOTE:
 * - ここでいう「ログを出さない」は主にアプリ実装のログ（stdout/stderr）を指す。
 *   依存ライブラリやOS側のログを完全に0にすることまでは保証しない。
 */

function getIsPackagedFromEnv() {
  const v = String(process.env.PANTARAY_PACKAGED || '').trim();
  return v === '1' || v.toLowerCase() === 'true';
}

function getLogSalt() {
  const raw = String(process.env.PANTARAY_LOG_SALT || '').trim();
  if (raw) return raw;
  throw new Error('PANTARAY_LOG_SALT is required.');
}

function shortSha256(input) {
  try {
    const s = typeof input === 'string' ? input : JSON.stringify(input);
    return createHash('sha256').update(s).digest('hex').slice(0, 12);
  } catch {
    return createHash('sha256').update(String(input)).digest('hex').slice(0, 12);
  }
}

function fingerprint(value) {
  const salt = getLogSalt();
  return `fp:${shortSha256(`${salt}:${String(value)}`)}`;
}

function looksLikeJwt(token) {
  const s = String(token || '').trim();
  return /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+={0,2}$/.test(s);
}

function safeJwtSummary(token) {
  const raw = String(token || '').trim();
  const len = raw.length;
  const fp = fingerprint(raw);
  if (!looksLikeJwt(raw)) return { present: Boolean(raw), jwt: false, len, fp };
  // exp/sub などを使うとデバッグが楽だが、PIIを出さないため fingerprint のみ返す
  // （必要なら呼び出し側で claim を追加。ここでは生値を出さない。）
  return { present: true, jwt: true, len, fp };
}

function safeUrlSummary(urlStr, { redactUserIdsInPath = true } = {}) {
  try {
    const u = new URL(String(urlStr || ''));
    const queryKeys = [];
    try {
      u.searchParams.forEach((_v, k) => queryKeys.push(k));
    } catch {}
    const origin = u.origin;
    let pathname = u.pathname || '/';
    if (redactUserIdsInPath) {
      // UUID を含むパス（/users/{uuid}/... 等）を丸ごと露出させない
      pathname = pathname.replace(
        /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/g,
        '<redacted>'
      );
    }
    return {
      origin,
      path: pathname,
      query_keys: queryKeys,
      fp: fingerprint(`${origin}${u.pathname}?${queryKeys.sort().join('&')}`),
    };
  } catch {
    return { fp: fingerprint(String(urlStr || '')), invalid: true };
  }
}

function safePathSummary(p) {
  const s = String(p || '');
  const base = path.basename(s);
  const fp = fingerprint(s);
  // 典型: /Users/<name>/... をプレースホルダ化
  const dirHint = s.includes('/Library/Application Support/') ? '<userData>' : '<path>';
  return { base, dir_hint: dirHint, fp };
}

const REDACTED = '<redacted>';
const SENSITIVE_QUERY_KEYS = new Set([
  'token',
  'access_token',
  'refresh_token',
  'id_token',
  'authorization',
  'apikey',
  'api_key',
  'key',
  'secret',
  'signature',
  'x-amz-signature',
  'x-amz-security-token',
  'x-amz-credential',
]);

function redactUrlInText(urlStr) {
  try {
    const u = new URL(urlStr);
    if (!u.search) return urlStr;
    for (const key of [...u.searchParams.keys()]) {
      const lower = key.toLowerCase();
      if (
        SENSITIVE_QUERY_KEYS.has(lower) ||
        ['token', 'secret', 'signature'].some((marker) => lower.includes(marker))
      ) {
        u.searchParams.set(key, REDACTED);
      }
    }
    return u.toString();
  } catch {
    return urlStr;
  }
}

/**
 * 自由文字列（例: Error.message）に紛れ込む秘密値をマスクする。
 * URL クエリの秘密キー / Bearer / token=... を伏せる（Python 側 redact_text と同方針）。
 */
function redactText(text) {
  let s = String(text || '');
  if (!s) return s;
  s = s.replace(/https?:\/\/[^\s'"<>]+/gi, (url) => redactUrlInText(url));
  s = s.replace(/(bearer\s+)[A-Za-z0-9._~+/=-]+/gi, `$1${REDACTED}`);
  s = s.replace(
    /((?:token|access_token|refresh_token|id_token|signature)\s*=\s*)[^\s,&"']+/gi,
    `$1${REDACTED}`
  );
  return s;
}

function safeStoragePathSummary(storagePath) {
  const raw = String(storagePath || '').trim();
  const fp = fingerprint(raw);
  const parts = raw.split('/').filter(Boolean);
  // 期待: {userId}/{YYYY-MM-DD}/{uuid}.ext
  let userId = parts.length >= 1 ? parts[0] : '';
  let date = parts.length >= 2 ? parts[1] : '';
  let file = parts.length >= 3 ? parts[2] : '';
  let ext = '';
  let objectId = '';
  try {
    const m = file.match(/^([0-9a-fA-F-]{36})\.(\w+)$/);
    if (m) {
      objectId = m[1];
      ext = m[2];
    }
  } catch {}
  return {
    user_fp: userId ? fingerprint(userId) : null,
    date: date || null,
    object_id: objectId || null,
    ext: ext || null,
    fp,
  };
}

function shouldAlwaysRedactKey(key) {
  const k = String(key || '').toLowerCase();
  return (
    k.includes('service_key') ||
    k.includes('service_role') ||
    k.includes('refresh_token') ||
    k.includes('password') ||
    k.includes('secret') ||
    k.includes('private_key') ||
    k.includes('code_verifier')
  );
}

function shouldRedactKey(key) {
  const k = String(key || '').toLowerCase();
  return (
    shouldAlwaysRedactKey(k) ||
    k.includes('authorization') ||
    k === 'token' ||
    k.endsWith('_token') ||
    k.includes('access_token') ||
    k.includes('refresh_token') ||
    k.includes('exchange_code') ||
    k.includes('attempt_id') ||
    k.includes('storage_path') ||
    k.includes('user_id') ||
    (k.includes('user') && k.includes('id')) ||
    k === 'userid' ||
    k === 'userId'.toLowerCase() ||
    k.includes('path')
  );
}

function safeStringValue(key, value) {
  const k = String(key || '').toLowerCase();
  const v = String(value || '');

  // "生のレスポンス" 等で access_token が混入し得る文字列は、キー名に関わらずマスクする
  if (v.includes('access_token') || v.includes('refresh_token') || v.includes('Authorization')) {
    return { fp: fingerprint(v), len: v.length };
  }

  // URLs（query は絶対出さない）
  if (v.startsWith('http://') || v.startsWith('https://') || v.startsWith('ws://') || v.startsWith('wss://')) {
    return safeUrlSummary(v);
  }

  // Authorization: Bearer ...
  if (k.includes('authorization') && v.toLowerCase().startsWith('bearer ')) {
    const token = v.replace(/^Bearer\s+/i, '');
    return { scheme: 'Bearer', ...safeJwtSummary(token) };
  }

  // JWT token
  if (looksLikeJwt(v)) {
    return { ...safeJwtSummary(v) };
  }

  // storage_path
  if (k.includes('storage_path')) {
    return { ...safeStoragePathSummary(v) };
  }

  // storage_path が "引数の生文字列" として渡されたケース（console.log('...', storagePath) など）
  // 期待: {uuid}/{YYYY-MM-DD}/{uuid}.ext
  if (/^[0-9a-fA-F-]{36}\/\d{4}-\d{2}-\d{2}\/[0-9a-fA-F-]{36}\.\w+$/.test(v)) {
    return { ...safeStoragePathSummary(v) };
  }

  // file path
  if (k.includes('path') || v.startsWith('/') || v.startsWith('file://')) {
    return { ...safePathSummary(v) };
  }

  // user_id / attempt_id / exchange_code
  // NOTE:
  // - exchange_code はログに出すと“その瞬間”に悪用され得るため、生値は出さない。
  if (k.includes('exchange_code')) {
    return { fp: fingerprint(v), len: v.length };
  }
  if (k.includes('user_id') || k.includes('attempt_id')) {
    return { fp: fingerprint(v), len: v.length };
  }

  // default
  if (shouldRedactKey(k)) {
    return { fp: fingerprint(v), len: v.length };
  }

  return v;
}

function redact(value, { depth = 0, maxDepth = 4 } = {}) {
  if (value == null) return value;
  if (depth > maxDepth) return { type: 'truncated', fp: fingerprint(String(value)) };

  const t = typeof value;
  if (t === 'string') return safeStringValue('', value);
  if (t === 'number' || t === 'boolean') return value;
  if (value instanceof Error) {
    return {
      class: value.name,
      // Error.message には token/URL 等が混入し得るため、永続化前にマスクする。
      msg_short: redactText(String(value.message || '')).slice(0, 200),
      fp: fingerprint(`${value.name}:${value.message}`),
    };
  }
  if (Array.isArray(value)) {
    const out = [];
    const limit = Math.min(value.length, 20);
    for (let i = 0; i < limit; i++) {
      out.push(redact(value[i], { depth: depth + 1, maxDepth }));
    }
    if (value.length > limit) out.push({ type: 'truncated_array', omitted: value.length - limit });
    return out;
  }
  if (t === 'object') {
    const obj = /** @type {Record<string, any>} */ (value);
    const out = {};
    const keys = Object.keys(obj);
    const limit = Math.min(keys.length, 50);
    for (let i = 0; i < limit; i++) {
      const k = keys[i];
      const v = obj[k];
      if (shouldAlwaysRedactKey(k)) {
        const s = typeof v === 'string' ? v : JSON.stringify(v);
        out[k] = { present: Boolean(s), fp: fingerprint(s), len: String(s || '').length };
        continue;
      }
      if (typeof v === 'string') {
        out[k] = safeStringValue(k, v);
      } else {
        out[k] = redact(v, { depth: depth + 1, maxDepth });
      }
    }
    if (keys.length > limit) out.__truncated_keys__ = keys.length - limit;
    return out;
  }

  return { type: t, fp: fingerprint(String(value)) };
}

function createLogger() {
  const isPackaged = getIsPackagedFromEnv();
  const levelName = normalizeLevel(process.env.PANTARAY_LOG_LEVEL || (isPackaged ? 'silent' : 'info'));
  const minLevel = LEVELS[levelName] ?? LEVELS.info;

  function shouldLog(level) {
    // NOTE:
    // - 配布版はデフォルト silent だが、PANTARAY_LOG_LEVEL を明示すれば
    //   マスク済みログを最小限出せる（運用デバッグ用）。
    // - console.* は配布版で no-op にしているため、依存ライブラリの生ログは出ない。
    return (LEVELS[level] ?? 999) >= minLevel;
  }

  function emit(level, evt, meta) {
    // stdout と file sink は独立に判定する。
    // - stdout: 配布版は silent（既存挙動を維持）。
    // - file: 配布版でも error 以上を記録する（stdout が silent でも残す）。
    const writeToStdout = shouldLog(level);
    const writeToFile = fileSink.isFileSinkEnabledFor(level);
    if (!writeToStdout && !writeToFile) return;

    // redact/シリアライズは一度だけ行う。
    let line;
    try {
      const payload = {
        ts: new Date().toISOString(),
        level,
        evt,
        ...redact(meta || {}),
      };
      // 1行JSONで出す（grepしやすい）
      line = `${JSON.stringify(payload)}\n`;
    } catch {
      line = `${String(evt)}\n`;
    }

    if (writeToStdout) {
      try {
        process.stdout.write(line);
      } catch {}
    }
    if (writeToFile) {
      fileSink.appendLine(line);
    }
  }

  return {
    isPackaged,
    level: levelName,
    event: (evt, meta, level = 'info') => emit(level, evt, meta),
    debug: (evt, meta) => emit('debug', evt, meta),
    info: (evt, meta) => emit('info', evt, meta),
    warn: (evt, meta) => emit('warn', evt, meta),
    error: (evt, meta) => emit('error', evt, meta),
    redact: (meta) => redact(meta),
    fingerprint,
    safeUrlSummary,
    safeJwtSummary,
    safeStoragePathSummary,
  };
}

/**
 * 配布版の console を no-op 化する。
 *
 * NOTE:
 * - 依存ライブラリが console メソッドの存在を前提にしているケースがあるため、
 *   `undefined` にはせず no-op 関数へ差し替える。
 */
function disableConsoleForRelease() {
  if (!getIsPackagedFromEnv()) return;
  const noop = () => {};
  try {
    for (const k of ['log', 'info', 'warn', 'error', 'debug', 'trace']) {
      if (typeof console[k] === 'function') console[k] = noop;
    }
  } catch {
    // no-op
  }
}

module.exports = {
  LEVELS,
  normalizeLevel,
  createLogger,
  disableConsoleForRelease,
  redact,
  fingerprint,
};
