/**
 * What a page in a hidden window may load: local files under the runtime's read rules, and
 * network requests under the command network setting. Every browser session, the task preview's
 * included, follows these rules; the session's tab in the user's browser follows
 * `decideUserBrowserRequest`.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  BROWSER_MAX_CONTACTED_HOSTS,
  type BrowserPageData,
  type BrowserPagePolicy,
} from './browserContract';

/**
 * One of the runtime's rules for files that hold secrets (`secret_paths`): a path matching
 * `withheld` and not `excepted`, POSIX EREs that read the same as JavaScript regexes.
 */
export type SecretPathRule = { withheld: string; excepted: string | null };

/**
 * The local files a page may load: under a file root, not in private app storage, and not
 * holding secrets.
 */
export type FileAccessScope = {
  fileRoots: string[];
  privateRoots: string[];
  privateRootExceptions: string[];
  secretPaths: SecretPathRule[];
};

/** Whether two paths name the same existing directory. */
export type SameDirectory = (left: string, right: string) => boolean;

/** Why a local file was not served. */
export type FileRefusal = 'outside_read_scope' | 'not_allowed';

/** A host the page reached; never the URL, whose path and query can carry tokens. */
export type ContactedHost = { host: string; port: number };

export type BrowserBlockedReason = BrowserPageData['blocked'][number]['reason'];

/** Schemes that reach no host; a `file` load is judged by `serveFile`. */
export const LOCAL_SCHEMES: ReadonlySet<string> = new Set(['file:', 'data:', 'blob:', 'about:']);
const DEFAULT_PORTS: Readonly<Record<string, number>> = {
  'http:': 80,
  'https:': 443,
  'ws:': 80,
  'wss:': 443,
};
const WEB_SOCKET_SCHEMES = new Set(['ws:', 'wss:']);
/** The methods that only read; anything else sends data a page could lose to its host. */
const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/** The types a deck loads; anything else is served without one and sniffed. */
const CONTENT_TYPES: Readonly<Record<string, string>> = {
  '.html': 'text/html',
  '.htm': 'text/html',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
};

/**
 * Every local document is sandboxed without `allow-modals`, so `print()` and the
 * other dialogs do nothing. The flags pass to every frame below it, including one
 * Chromium runs in its own renderer (a sandboxed iframe), where no script injected
 * from here would reach: `window.print()` there opens the system print panel even
 * for a hidden window. Same-origin keeps local fonts and data loadable.
 */
const DOCUMENT_SANDBOX = 'sandbox allow-scripts allow-same-origin';

/**
 * Mirror of the runtime's `is_within_any` for one root: lexically below it, or below
 * an ancestor that is the same directory under another letter case (APFS).
 */
export function isWithinRoot(target: string, root: string, sameDirectory: SameDirectory): boolean {
  const prefix = root.endsWith(path.sep) ? root : root + path.sep;
  if (target === root || target.startsWith(prefix)) return true;
  const rootName = path.basename(root).toLowerCase();
  for (let ancestor = target; path.dirname(ancestor) !== ancestor; ) {
    if (path.basename(ancestor).toLowerCase() === rootName && sameDirectory(ancestor, root)) {
      return true;
    }
    ancestor = path.dirname(ancestor);
  }
  return false;
}

/**
 * Whether the page may load this resolved local file: under a file root, and not in
 * private app storage unless under one of its exceptions (the runtime's
 * `PrivateAppStorage.hides()`). Null allows it.
 */
export function decideFileAccess(
  realPath: string,
  scope: FileAccessScope,
  sameDirectory: SameDirectory
): 'outside_read_scope' | null {
  const within = (roots: string[]) =>
    roots.some((root) => isWithinRoot(realPath, root, sameDirectory));
  if (!within(scope.fileRoots)) return 'outside_read_scope';
  if (within(scope.privateRoots) && !within(scope.privateRootExceptions)) {
    return 'outside_read_scope';
  }
  return null;
}

/** U+0131 uppercases to `I`, yet its case fold is itself. */
const DOTLESS_I = 'ı';

/**
 * A path as APFS compares names, the runtime's `_folded`: NFD, case fold, NFD. JavaScript has
 * no case fold; lower, upper, then lower again gives the fold's ASCII letters (`ſ` to `s`, `ß`
 * and `ẞ` to `ss`, `ﬁ` to `fi`, the Kelvin sign to `k`), and mapping each character alone folds
 * a final sigma to `σ` as the case fold does. Scripts whose fold is uppercase (Cherokee) fold
 * apart from Python's; no rule depends on that, since the rules bracket only ASCII letters and
 * the home folder in them is folded here as the paths are.
 */
export function foldPath(text: string): string {
  const fold = (char: string) =>
    char === DOTLESS_I ? char : char.toLowerCase().toUpperCase().toLowerCase();
  return Array.from(text.normalize('NFD'), fold).join('').normalize('NFD');
}

/**
 * Whether a file holds secrets by the name the page asked for or the real path it resolves to,
 * as the runtime's `withheld_reason` judges both. Each rule is folded like the paths: it spells
 * the home folder as the OS stores it, so `/Users/José` matches in either Unicode form, and
 * folding leaves its regex syntax alone. A directory is never served, so `directories_exempt`
 * changes nothing here and is not sent.
 */
export function isSecretPath(paths: string[], rules: SecretPathRule[]): boolean {
  const folded = paths.map(foldPath);
  return rules.some((rule) => {
    const withheld = new RegExp(foldPath(rule.withheld));
    const excepted = rule.excepted === null ? null : new RegExp(foldPath(rule.excepted));
    return folded.some((text) => withheld.test(text) && excepted?.test(text) !== true);
  });
}

export function sameDirectoryOnDisk(left: string, right: string): boolean {
  const a = fs.statSync(left, { throwIfNoEntry: false });
  const b = fs.statSync(right, { throwIfNoEntry: false });
  return (
    a !== undefined &&
    b !== undefined &&
    a.isDirectory() &&
    b.isDirectory() &&
    a.dev === b.dev &&
    a.ino === b.ino
  );
}

/**
 * Serves an allowed local file from the descriptor that was checked, so a link
 * swapped after the check cannot serve another file. A file that does not exist
 * fails as Chromium's loader would; a directory is never listed.
 */
export async function serveFile(
  url: string,
  scope: FileAccessScope,
  block: (reason: FileRefusal, url: string) => void
): Promise<Response> {
  const requestedPath = fileURLToPath(url);
  let file: fs.promises.FileHandle;
  try {
    // Non-blocking, so a FIFO cannot hold the open.
    file = await fs.promises.open(requestedPath, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
  } catch {
    return Response.error();
  }
  try {
    const opened = await file.stat();
    const realPath = await fs.promises.realpath(requestedPath);
    const reason = isSecretPath([requestedPath, realPath], scope.secretPaths)
      ? 'not_allowed'
      : decideFileAccess(realPath, scope, sameDirectoryOnDisk);
    const checked = await fs.promises.stat(realPath);
    if (reason !== null || checked.dev !== opened.dev || checked.ino !== opened.ino) {
      block(reason ?? 'outside_read_scope', url);
      return Response.error();
    }
    if (!opened.isFile()) {
      block('not_allowed', url);
      return Response.error();
    }
    const contentType = CONTENT_TYPES[path.extname(realPath).toLowerCase()];
    return new Response(await file.readFile(), {
      headers: {
        'Content-Security-Policy': DOCUMENT_SANDBOX,
        ...(contentType === undefined ? {} : { 'Content-Type': contentType }),
      },
    });
  } finally {
    await file.close();
  }
}

/**
 * The host and port a URL reaches, in the command network's form (IPv6 without brackets,
 * lowercase, as the URL parser normalizes it); null for a scheme that reaches no host.
 */
export function reachedHost(url: URL): ContactedHost | null {
  const defaultPort = DEFAULT_PORTS[url.protocol];
  if (defaultPort === undefined) return null;
  return {
    host: url.hostname.replace(/^\[(.*)\]$/, '$1'),
    port: url.port === '' ? defaultPort : Number(url.port),
  };
}

/**
 * A host name or IP literal (IPv6 without brackets) in `reachedHost`'s form, so an approved
 * host compares equal to the request that reaches it; null when it does not parse as a host.
 */
function normalizedHost(host: string): string | null {
  try {
    return reachedHost(new URL(`http://${host.includes(':') ? `[${host}]` : host}/`))?.host ?? null;
  } catch {
    return null;
  }
}

/**
 * Whether a normalized host names this machine: the names Chromium resolves to loopback, 127/8
 * and `::1`. Another name for it is judged as an external host, which only narrows it.
 */
function isLoopbackHost(host: string): boolean {
  const name = host.replace(/\.$/, '');
  return (
    name === 'localhost' ||
    name.endsWith('.localhost') ||
    host === '::1' ||
    /^127\.\d+\.\d+\.\d+$/.test(host)
  );
}

/**
 * Whether a normalized host reaches this machine by its address: a loopback name or address, the
 * unspecified address, which connects to this machine, or an IPv4-mapped form of either. A name
 * that resolves here is judged as an external host.
 */
function reachesThisMachine(host: string): boolean {
  return (
    isLoopbackHost(host) ||
    ['0.0.0.0', '::', '::ffff:0:0'].includes(host) ||
    /^::ffff:7f[0-9a-f]{2}:[0-9a-f]{1,4}$/.test(host)
  );
}

export type BrowserRequestDecision =
  | { allowed: true; reached: ContactedHost | null }
  | { allowed: false; reason: BrowserBlockedReason };

const refuse = (reason: BrowserBlockedReason): BrowserRequestDecision => ({
  allowed: false,
  reason,
});

/** Whether `host` (normalized) is one of `hosts`, as the runtime approved them. */
const listsHost = (hosts: readonly string[], host: string): boolean =>
  hosts.some((listed) => normalizedHost(listed) === host);

/** A method other than a read, or a WebSocket handshake, sends data to the host. */
const sendsData = (url: URL, method: string): boolean =>
  WEB_SOCKET_SCHEMES.has(url.protocol) || !READ_METHODS.has(method);

/** Past `BROWSER_MAX_CONTACTED_HOSTS` a new host is refused, so every host reached is reported. */
function withinHostCap(
  reached: ContactedHost,
  contacted: readonly ContactedHost[]
): BrowserRequestDecision {
  const known = contacted.some((host) => host.host === reached.host && host.port === reached.port);
  if (!known && contacted.length >= BROWSER_MAX_CONTACTED_HOSTS) return refuse('too_many_hosts');
  return { allowed: true, reached };
}

/**
 * Whether a browser session's page may send one request, and the host it reaches when it may.
 *
 * - A local scheme reaches no host; `serveFile` judges a file.
 * - With the network off nothing leaves the page, loopback included.
 * - With it on, nothing goes to the backend's port, under any host name: a name can resolve to
 *   this machine. Loopback takes every method. An external host takes reads only; another
 *   method or a WebSocket handshake needs the host among the run's `write_hosts`.
 * - Past the host cap a new host is refused. `contacted` is what the session reached so far.
 */
export function decideBrowserRequest(
  request: { url: string; method: string },
  context: {
    policy: Pick<BrowserPagePolicy, 'allow_network' | 'write_hosts'>;
    backendPort: number;
    contacted: readonly ContactedHost[];
  }
): BrowserRequestDecision {
  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return refuse('not_allowed');
  }
  if (LOCAL_SCHEMES.has(url.protocol)) return { allowed: true, reached: null };
  if (!context.policy.allow_network) return refuse('network_off');
  const reached = reachedHost(url);
  if (reached === null) return refuse('not_allowed');
  // The backend listens on a local high port, so an external site on that very port is refused
  // too; that is the accepted cost of judging without resolving names.
  if (reached.port === context.backendPort) return refuse('not_allowed');
  if (
    !isLoopbackHost(reached.host) &&
    sendsData(url, request.method) &&
    !listsHost(context.policy.write_hosts, reached.host)
  ) {
    return refuse('write_not_approved');
  }
  return withinHostCap(reached, context.contacted);
}

/**
 * Whether the tab of the session's own in the user's browser may send one request, judged on
 * the URL Chromium reports for it, so each hop of a redirect is judged too. The tab is signed in
 * to the user's real accounts, so it reaches less than the app's page:
 *
 * - nothing on this machine (loopback, or the backend's port under any name), and no file;
 * - a document in the tab's main frame only from a host the user approved this run to open
 *   (`read_hosts`); a frame or subresource reads from any host, as the opened site asks;
 * - writes and WebSocket handshakes only to the run's `write_hosts`;
 * - the host cap as on the app's page.
 *
 * The runtime sends user-browser requests only with the network on, so it is not judged here.
 */
export function decideUserBrowserRequest(
  request: { url: string; method: string; mainFrameDocument: boolean },
  context: {
    readHosts: readonly string[];
    writeHosts: readonly string[];
    backendPort: number;
    contacted: readonly ContactedHost[];
  }
): BrowserRequestDecision {
  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return refuse('not_allowed');
  }
  if (url.protocol !== 'file:' && LOCAL_SCHEMES.has(url.protocol)) {
    return { allowed: true, reached: null };
  }
  const reached = reachedHost(url);
  if (
    reached === null ||
    reached.port === context.backendPort ||
    reachesThisMachine(reached.host)
  ) {
    return refuse('not_allowed');
  }
  if (request.mainFrameDocument && !listsHost(context.readHosts, reached.host)) {
    return refuse('read_not_approved');
  }
  if (sendsData(url, request.method) && !listsHost(context.writeHosts, reached.host)) {
    return refuse('write_not_approved');
  }
  return withinHostCap(reached, context.contacted);
}
