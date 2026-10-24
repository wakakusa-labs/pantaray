import fs from 'node:fs';
import path from 'node:path';

import { writeFileAtomic } from '../atomicFile';
import { OnboardingStateSchema, type OnboardingState } from '../ipc/schemas/onboarding';
import { SCOPED_PREFERENCE_FILES } from './scope';

const FILE_NAME = 'onboarding.json';
/** The root `scope.ts` keeps each user scope's settings under. */
const SETTINGS_ROOT_DIR_NAME = 'settings';
const FIRST_STEP: OnboardingState = { status: 'in_progress', step: 'ai' };

export type OnboardingStateStore = {
  get: () => OnboardingState;
  set: (state: OnboardingState) => OnboardingState;
};

/**
 * Recording ran for some user on this device, which leaves that scope's recording
 * preference behind: the app was in use before onboarding existed.
 */
function recordedBefore(userDataDir: string): boolean {
  const root = path.join(userDataDir, SETTINGS_ROOT_DIR_NAME);
  let scopes: string[];
  try {
    scopes = fs.readdirSync(root);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
  return scopes.some((scope) =>
    fs.existsSync(path.join(root, scope, SCOPED_PREFERENCE_FILES.recording))
  );
}

/**
 * First-run setup state for this device, written only by main.
 *
 * Read when the store is created, which main does before recording can be restored or
 * started: the first recording leaves the same preference an existing user has, so a
 * later first read would take a new user's own recording for an earlier one.
 *
 * A file that cannot be read restarts setup rather than stopping the app: every step
 * reads what is already configured, so a user who had finished passes through quickly.
 */
export function createOnboardingStateStore(params: {
  userDataDir: string;
  report: (reason: string) => void;
}): OnboardingStateStore {
  const filePath = path.join(params.userDataDir, FILE_NAME);
  const write = (state: OnboardingState): void =>
    writeFileAtomic(filePath, params.userDataDir, Buffer.from(`${JSON.stringify(state)}\n`));

  const read = (): OnboardingState => {
    let raw: string;
    try {
      raw = fs.readFileSync(filePath, 'utf8');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        params.report('unreadable_file');
        return FIRST_STEP;
      }
      // Stored, so the next launch does not decide again from a preference that
      // recording during setup left behind.
      const decided: OnboardingState = recordedBefore(params.userDataDir)
        ? { status: 'completed' }
        : FIRST_STEP;
      try {
        write(decided);
      } catch {
        params.report('write_failed');
      }
      return decided;
    }
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      params.report('malformed_json');
      return FIRST_STEP;
    }
    const parsed = OnboardingStateSchema.safeParse(value);
    if (!parsed.success) {
      params.report('invalid_state');
      return FIRST_STEP;
    }
    return parsed.data;
  };

  let current = read();
  return {
    get: () => current,
    set: (state) => {
      write(state);
      current = state;
      return state;
    },
  };
}
