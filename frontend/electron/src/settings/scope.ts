import fs from 'fs';
import path from 'path';

const SETTINGS_ROOT_DIR_NAME = 'settings';
const LOGGED_OUT_SCOPE_ID = '__logged_out__';
const SETTINGS_SCOPE_USER_ID_PATTERN = /^[A-Za-z0-9_-]+$/;
const SETTINGS_FILE_NAME_PATTERN = /^[A-Za-z0-9._-]+\.json$/;

export const SCOPED_PREFERENCE_FILES = {
  ui: 'ui-settings.json',
  capturePrivacy: 'capture-privacy-settings.json',
  recording: 'screenshot-settings.json',
  meetingTranscription: 'meeting-transcription-settings.json',
} as const;

type SettingsScopeParams = {
  userDataDir: string;
  userId: string | null;
};

function normalizeUserDataDir(userDataDir: string): string {
  const normalized = String(userDataDir || '').trim();
  if (!normalized) {
    throw new Error('userDataDir is required to resolve scoped settings paths.');
  }
  return normalized;
}

function normalizeScopeId(userId: string | null): string {
  if (userId === null) return LOGGED_OUT_SCOPE_ID;
  const normalized = String(userId).trim();
  if (!normalized) return LOGGED_OUT_SCOPE_ID;
  if (!SETTINGS_SCOPE_USER_ID_PATTERN.test(normalized)) {
    throw new Error(`Invalid settings scope user id: "${normalized}"`);
  }
  return normalized;
}

function normalizeFileName(fileName: string): string {
  const normalized = String(fileName || '').trim();
  if (!normalized) {
    throw new Error('fileName is required to resolve scoped settings paths.');
  }
  if (path.basename(normalized) !== normalized) {
    throw new Error(`fileName must be a basename: "${normalized}"`);
  }
  if (!SETTINGS_FILE_NAME_PATTERN.test(normalized)) {
    throw new Error(`Invalid settings file name: "${normalized}"`);
  }
  return normalized;
}

export function resolveSettingsScopeDirectory(params: SettingsScopeParams): string {
  const userDataDir = normalizeUserDataDir(params.userDataDir);
  const scopeId = normalizeScopeId(params.userId);
  const scopeDir = path.join(userDataDir, SETTINGS_ROOT_DIR_NAME, scopeId);
  fs.mkdirSync(scopeDir, { recursive: true });
  return scopeDir;
}

export function resolveScopedSettingsPath(
  params: SettingsScopeParams & { fileName: string }
): string {
  const scopeDir = resolveSettingsScopeDirectory(params);
  const fileName = normalizeFileName(params.fileName);
  return path.join(scopeDir, fileName);
}

/** Initialize a new account before any consumer opens or creates its scope. */
export function initializeAccountSettingsScope(params: {
  userDataDir: string;
  accountUserId: string;
}): void {
  const settingsRoot = path.join(normalizeUserDataDir(params.userDataDir), SETTINGS_ROOT_DIR_NAME);
  const source = path.join(settingsRoot, LOGGED_OUT_SCOPE_ID);
  const accountScopeId = normalizeScopeId(params.accountUserId);
  const destination = path.join(settingsRoot, accountScopeId);
  if (fs.existsSync(destination)) return;

  fs.mkdirSync(settingsRoot, { recursive: true });
  // Main holds the single-instance lock; discard a copy interrupted by process exit.
  const staging = path.join(settingsRoot, `.settings-inheritance-${accountScopeId}`);
  fs.rmSync(staging, { recursive: true, force: true });
  fs.mkdirSync(staging, { mode: 0o700 });
  try {
    // Read state and history share the scope directory, but never follow an account.
    for (const fileName of Object.values(SCOPED_PREFERENCE_FILES)) {
      let contents: Buffer;
      try {
        contents = fs.readFileSync(path.join(source, fileName));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue;
        throw error;
      }
      fs.writeFileSync(path.join(staging, fileName), contents, { mode: 0o600, flush: true });
    }
    // Persist all directory entries before the complete scope becomes visible.
    const directory = fs.openSync(staging, 'r');
    try {
      fs.fsyncSync(directory);
    } finally {
      fs.closeSync(directory);
    }
    fs.renameSync(staging, destination);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
}
