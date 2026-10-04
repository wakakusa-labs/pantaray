/**
 * What an editor window's title says about the file it shows.
 *
 * The always-on recorder keeps `.env` files out of what it records from Cursor and
 * Visual Studio Code, reading the file name from the window title (`zaneiConfig.ts`
 * writes the `ide` block; the recorder decides). `capture_screen` can photograph the
 * same window, so the same rule has to hold here. This is the TypeScript statement of
 * the recorder's `ide_file_name` and `is_env_file`, and
 * `tests/electron_ide_file_policy.test.js` replays the recorder's own `ide_cases`
 * (`tests/fixtures/zanei_privacy_parity_cases.json`) through it.
 *
 * A title is read as ` - `-separated parts (en and em dashes count as `-`), and the
 * first part that looks like a file name is the file. A part with a path separator is
 * never a file name, so a title that names its file only by path names none.
 */

export type IdeTitleOutcome = 'allow' | 'env_file' | 'file_name_unavailable';

const MAX_FILE_NAME_UTF16_UNITS = 255;

/** Editors whose window title the rule reads, by display name as the recorder matches them. */
const IDE_APP_NAMES = new Set(['cursor', 'visual studio code', 'code']);

export function isIdeApp(appName: string): boolean {
  return IDE_APP_NAMES.has(appName.trim().toLowerCase());
}

export function ideTitleOutcome(title: string | null): IdeTitleOutcome {
  const fileName = title === null ? null : ideFileName(title);
  if (fileName === null) return 'file_name_unavailable';
  return isEnvFile(fileName) ? 'env_file' : 'allow';
}

function ideFileName(rawTitle: string): string | null {
  const title = rawTitle.replace(/[–—]/g, '-');
  let start = 0;
  for (let index = title.indexOf('-'); index >= 0; index = title.indexOf('-', index + 1)) {
    if (/\s/.test(title[index - 1] ?? '') && /\s/.test(title[index + 1] ?? '')) {
      const name = fileNameCandidate(title.slice(start, index));
      if (name !== null) return name;
      start = index + 1;
    }
  }
  return fileNameCandidate(title.slice(start));
}

/** Unsaved-change markers (`●`, `•`) are not part of the name. */
function fileNameCandidate(candidate: string): string | null {
  const name = candidate.replace(/[●•]/g, '').trim();
  if (name.length > MAX_FILE_NAME_UTF16_UNITS || /[/\\]/.test(name)) return null;
  const lower = name.toLowerCase();
  const dot = name.lastIndexOf('.');
  const hasExtension = dot > 0 && /^[0-9A-Za-z]{1,10}$/.test(name.slice(dot + 1));
  return lower === '.env' || lower.startsWith('.env.') || hasExtension ? name : null;
}

/** `.env` and `.env.<anything>`, except the examples a project commits on purpose. */
function isEnvFile(name: string): boolean {
  const lower = name.toLowerCase();
  const envNamed = lower === '.env' || (lower.startsWith('.env.') && lower.length > '.env.'.length);
  return envNamed && !lower.includes('.example');
}
