/**
 * Whether one window may be captured for `capture_screen`, under the user's recording
 * filter.
 *
 * The filter is the one the always-on recorder applies (`zaneiConfig.ts`), and every
 * rule here means what the recorder's rule means: the app lists, the website lists
 * matched on domain boundaries, sign-in and payment pages refused whatever the lists
 * say (`browserUrlPolicy.ts`), and `.env` files kept out of editor windows
 * (`ideFilePolicy.ts`). Password managers are refused even when the user's list names
 * them (`alwaysDeniedCaptureApps.ts`).
 *
 * A refusal may name the application or the website host; it never carries the window
 * title, which is often the private thing the filter exists to protect.
 */

import { isAlwaysDeniedCaptureApp } from './alwaysDeniedCaptureApps';
import { decideBrowserUrl } from './browserUrlPolicy';
import { captureAppKey, type CapturePrivacySettings } from './capturePrivacy';
import { ideTitleOutcome, isIdeApp } from './ideFilePolicy';

export type CaptureApp = { name: string; bundleId: string | null };

export type WindowBounds = { x: number; y: number; width: number; height: number };

/** One browser window as the browser itself describes it over Apple Events. */
export type BrowserWindowObservation = {
  title: string | null;
  bounds: WindowBounds;
  /** Active tab URL, or null when it could not be read. */
  url: string | null;
  /** Chrome's window mode (`normal`, `incognito`); Safari reports none. */
  mode: string | null;
};

export type CaptureRefusal = {
  status: 'refused';
  code:
    | 'SCREEN_RECORDING_PERMISSION_REQUIRED'
    | 'CAPTURE_TARGET_NOT_FOUND'
    | 'CAPTURE_REFUSED_BY_PRIVACY_FILTER'
    | 'CAPTURE_REFUSED_PASSWORD_MANAGER'
    | 'CAPTURE_REFUSED_URL_UNAVAILABLE'
    | 'CAPTURE_REFUSED_PRIVATE_WINDOW'
    | 'CAPTURE_REFUSED_SENSITIVE_PAGE';
  axis?: 'app' | 'website' | 'file' | 'editing';
  app_name?: string;
  host?: string;
  available_apps?: string[];
};

/**
 * How a window's contents are judged beyond its app.
 *
 * Chrome and Safari are the browsers whose page the website filter applies to. The
 * other browsers the recorder knows can report no URL to judge, so their pages are
 * refused rather than captured unfiltered. Matching is by bundle identifier, which is
 * what the filter is keyed by.
 */
export type CaptureSurface = 'chrome' | 'safari' | 'unfilterable_browser' | 'ide' | 'app';

const CHROME_BUNDLE_ID = 'com.google.Chrome';
const SAFARI_BUNDLE_ID = 'com.apple.Safari';
const UNFILTERABLE_BROWSER_BUNDLE_IDS = new Set([
  'org.mozilla.firefox',
  'com.brave.Browser',
  'com.microsoft.edgemac',
  'com.vivaldi.Vivaldi',
  'company.thebrowser.Browser',
]);

export function captureSurface(app: CaptureApp): CaptureSurface {
  if (app.bundleId === CHROME_BUNDLE_ID) return 'chrome';
  if (app.bundleId === SAFARI_BUNDLE_ID) return 'safari';
  if (app.bundleId !== null && UNFILTERABLE_BROWSER_BUNDLE_IDS.has(app.bundleId)) {
    return 'unfilterable_browser';
  }
  return isIdeApp(app.name) ? 'ide' : 'app';
}

/** The app-level rules, which need nothing but the app's identity. */
export function decideAppCapture(
  settings: CapturePrivacySettings,
  app: CaptureApp
): CaptureRefusal | null {
  if (app.bundleId === null) {
    // Filter entries are keyed by bundle id; without one, an excluded app would read
    // as "not listed" and pass. The name alone is not evidence the filter accepts.
    return refusal('CAPTURE_REFUSED_BY_PRIVACY_FILTER', { axis: 'app', app_name: app.name });
  }
  if (isAlwaysDeniedCaptureApp(app)) {
    return refusal('CAPTURE_REFUSED_PASSWORD_MANAGER', { app_name: app.name });
  }
  if (!isAppAllowed(settings, app)) {
    return refusal('CAPTURE_REFUSED_BY_PRIVACY_FILTER', { axis: 'app', app_name: app.name });
  }
  return null;
}

/**
 * The rules for what the window shows, for an app that already passed
 * `decideAppCapture`. `page` is the browser window bound to the captured one, or null
 * when no single browser window could be bound to it.
 */
export function decideWindowContents(
  settings: CapturePrivacySettings,
  target: {
    app: CaptureApp;
    surface: CaptureSurface;
    title: string | null;
    page: BrowserWindowObservation | null;
  }
): CaptureRefusal | null {
  const appName = target.app.name;
  switch (target.surface) {
    case 'app':
      return null;
    case 'unfilterable_browser':
      return refusal('CAPTURE_REFUSED_URL_UNAVAILABLE', { app_name: appName });
    case 'ide':
      return decideIdeWindow(settings, appName, target.title);
    case 'chrome':
    case 'safari':
      return decideBrowserWindow(settings, appName, target.surface, target.page);
  }
}

function decideIdeWindow(
  settings: CapturePrivacySettings,
  appName: string,
  title: string | null
): CaptureRefusal | null {
  const rules = settings.ideFileRules;
  if (!rules.sensitivePresets.blockEnvFiles) return null;
  const outcome = ideTitleOutcome(title);
  if (
    outcome === 'env_file' ||
    (outcome === 'file_name_unavailable' && rules.onFileNameUnavailable === 'block')
  ) {
    return refusal('CAPTURE_REFUSED_BY_PRIVACY_FILTER', { axis: 'file', app_name: appName });
  }
  return null;
}

/**
 * Chrome Incognito windows are refused, and a Chrome window whose mode cannot be read
 * is not assumed to be a normal one. Safari reports no private-window state, and the
 * filter makes no promise about Safari private windows, so none is judged.
 */
function decideBrowserWindow(
  settings: CapturePrivacySettings,
  appName: string,
  surface: 'chrome' | 'safari',
  page: BrowserWindowObservation | null
): CaptureRefusal | null {
  if (page === null) return refusal('CAPTURE_REFUSED_URL_UNAVAILABLE', { app_name: appName });
  if (surface === 'chrome' && page.mode !== 'normal') {
    return page.mode === 'incognito'
      ? refusal('CAPTURE_REFUSED_PRIVATE_WINDOW', { app_name: appName })
      : refusal('CAPTURE_REFUSED_URL_UNAVAILABLE', { app_name: appName });
  }
  const decision = decideBrowserUrl(page.url);
  if (decision.kind === 'unavailable') {
    return refusal('CAPTURE_REFUSED_URL_UNAVAILABLE', { app_name: appName });
  }
  if (decision.kind === 'sensitive') {
    // The route is what gave the page away; naming host or path would leak it.
    return refusal('CAPTURE_REFUSED_SENSITIVE_PAGE', { app_name: appName });
  }
  if (!isHostAllowed(settings, decision.host)) {
    return refusal('CAPTURE_REFUSED_BY_PRIVACY_FILTER', {
      axis: 'website',
      app_name: appName,
      host: decision.host,
    });
  }
  return null;
}

function refusal(
  code: CaptureRefusal['code'],
  details: Omit<CaptureRefusal, 'status' | 'code'>
): CaptureRefusal {
  return { status: 'refused', code, ...details };
}

function isAppAllowed(settings: CapturePrivacySettings, app: CaptureApp): boolean {
  const listed = settings.apps.entries.some((entry) => {
    const key = captureAppKey(entry).toLowerCase();
    if (app.bundleId !== null && key === app.bundleId.toLowerCase()) return true;
    // A name-only entry is the recorder's fallback for a bundle with no identifier.
    return entry.bundleId === null && key === app.name.trim().toLowerCase();
  });
  return settings.apps.mode === 'exclude' ? !listed : listed;
}

/**
 * A listed host covers itself and every subdomain, exactly as the recorder matches
 * the rules `zaneiConfig.ts` writes (`match_subdomains = true`): `example.com` covers
 * `mail.example.com` but not `badexample.com`.
 */
function isHostAllowed(settings: CapturePrivacySettings, host: string): boolean {
  const listed = settings.websites.hosts.some((rule) => host === rule || host.endsWith(`.${rule}`));
  return settings.websites.mode === 'exclude' ? !listed : listed;
}
