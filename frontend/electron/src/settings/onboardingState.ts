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
      // Decided once and stored: recording started during setup leaves the same
      // preference an existing user has, so deciding again on the next launch would
      // skip the rest of setup.
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

  let current: OnboardingState | null = null;
  return {
    get: () => (current ??= read()),
    set: (state) => {
      write(state);
      current = state;
      return state;
    },
  };
}
