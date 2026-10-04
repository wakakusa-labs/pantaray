import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {
  ALWAYS_DENIED_APP_NAMES,
  ALWAYS_DENIED_BUNDLE_IDS,
} from '../privacy/alwaysDeniedCaptureApps';
import { captureAppKey, type CapturePrivacySettings } from '../privacy/capturePrivacy';

export type ZaneiManifest = {
  executable_path: string;
  executable_sha256: string;
  protocol_version: number;
  subject_root: string;
  keychain_service_prefix: string;
  keychain_label_prefix: string;
};

export function readZaneiManifest(manifestPath: string): ZaneiManifest {
  const value = JSON.parse(fs.readFileSync(manifestPath, 'utf8')).zanei as ZaneiManifest;
  if (
    !value ||
    value.protocol_version !== 1 ||
    !path.isAbsolute(value.executable_path) ||
    !path.isAbsolute(value.subject_root) ||
    !value.keychain_service_prefix ||
    !value.keychain_label_prefix
  ) {
    throw new Error('Invalid Zanei runtime manifest.');
  }
  const hash = createHash('sha256').update(fs.readFileSync(value.executable_path)).digest('hex');
  if (hash !== value.executable_sha256) throw new Error('Zanei executable hash mismatch.');
  return value;
}

export function zaneiSubjectPaths(manifest: ZaneiManifest, userId: string) {
  const suffix = createHash('sha256').update(userId).digest('hex');
  const directory = path.join(manifest.subject_root, 'subjects', suffix);
  return {
    directory,
    config: path.join(directory, 'config.toml'),
    store: path.join(directory, 'store.sqlite3'),
    service: `${manifest.keychain_service_prefix}.${suffix}`,
    label: `${manifest.keychain_label_prefix}.${suffix}`,
  };
}

// TOML basic strings share JSON escaping except for JSON's optional escaped slash.
const quote = (value: string) => JSON.stringify(value);
const quotedList = (values: readonly string[]) => `[${values.map(quote).join(', ')}]`;

/**
 * Website rules use one entry per host with an empty path prefix, matching subdomains:
 * the settings UI collects domains, not paths.
 */
const websiteRules = (hosts: readonly string[]) =>
  `[${hosts
    .map((host) => `{ host = ${quote(host)}, path_prefix = "", match_subdomains = true }`)
    .join(', ')}]`;

/**
 * The always-denied apps, in the form the recorder compares against.
 *
 * The recorder keys a running app by its `bundle_id` when it reports one and by its
 * display name otherwise, then matches that single key case-insensitively against the
 * configured list. Both forms therefore have to be written: the bundle identifiers
 * cover apps that report one, the names cover the rare app that does not.
 */
const ALWAYS_DENIED_APP_KEYS: readonly string[] = [
  ...ALWAYS_DENIED_BUNDLE_IDS,
  ...ALWAYS_DENIED_APP_NAMES,
];

const DENIED_KEYS_LOWERCASE = new Set(ALWAYS_DENIED_APP_KEYS.map((key) => key.toLowerCase()));

/**
 * The apps the recorder must skip: the always-denied ones, plus the user's own list
 * when the filter excludes rather than includes.
 *
 * The recorder consults `exclude_apps` after `include_only_apps`, so an entry here is
 * skipped even when the user's "only these apps" list names it — which is what the
 * always-denied list is for. It is written in both filter modes for that reason.
 */
function excludedAppKeys(settings: CapturePrivacySettings): string[] {
  if (settings.apps.mode === 'include_only') return [...ALWAYS_DENIED_APP_KEYS];
  const userKeys = settings.apps.entries
    .map(captureAppKey)
    .filter((key) => !DENIED_KEYS_LOWERCASE.has(key.toLowerCase()));
  return [...ALWAYS_DENIED_APP_KEYS, ...userKeys];
}

/**
 * Renders the recorder configuration for the current filter.
 *
 * App selection is the top-level `[filter]` lists. `capture_policy.allowed_apps` is a
 * second, stricter gate keyed by display name, so it is written only for "only these
 * apps" with no apps: the recorder reads an empty `include_only_apps` as no restriction,
 * whereas an empty `allowed_apps` denies every app. The unreadable-settings fallback takes
 * the same path. The `[filter.text_content]` and `[filter.content_snapshot]` scopes are
 * left out so the recorder's own defaults keep browser bodies (Safari, Firefox, Brave,
 * Edge, Vivaldi, Arc) out of the store.
 */
export function zaneiConfig(settings: CapturePrivacySettings): string {
  const isAppIncludeOnly = settings.apps.mode === 'include_only';
  const recordsNoApp = isAppIncludeOnly && settings.apps.entries.length === 0;
  const isWebsiteIncludeOnly = settings.websites.mode === 'include_only';
  const hosts = settings.websites.hosts;
  return `[capture]
text_content = true
content_snapshot = true
[output]
retention_hours = 48
[filter]
exclude_apps = ${quotedList(excludedAppKeys(settings))}
include_only_apps = ${quotedList(isAppIncludeOnly ? settings.apps.entries.map(captureAppKey) : [])}
[filter.capture_policy]
${recordsNoApp ? 'allowed_apps = []\n' : ''}[filter.capture_policy.browser]
mode = ${quote(isWebsiteIncludeOnly ? 'rules' : 'all_sites')}
default_policy = ${quote(isWebsiteIncludeOnly ? 'block' : 'allow')}
on_url_unavailable = ${quote(isWebsiteIncludeOnly ? 'block' : 'allow')}
block_auth = true
block_payments = true
allow_list = ${websiteRules(isWebsiteIncludeOnly ? hosts : [])}
block_list = ${websiteRules(isWebsiteIncludeOnly ? [] : hosts)}
[filter.capture_policy.ide]
block_env_files = ${settings.ideFileRules.sensitivePresets.blockEnvFiles}
on_file_name_unavailable = ${quote(settings.ideFileRules.onFileNameUnavailable)}
`;
}

export function policyRevision(config: string): string {
  return createHash('sha256').update(config).digest('hex');
}
