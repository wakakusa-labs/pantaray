import type { UserBrowserId } from './browserContract';
import { KNOWN_BROWSERS } from './knownBrowsers';
import type { CommandRunner } from './userBrowserConnection';

/**
 * Brings the user's browser to the front, where it waits on the user. Only the user's own press
 * calls this: nothing takes focus on its own.
 */
export async function bringUserBrowserForward(
  browser: UserBrowserId,
  runner: CommandRunner
): Promise<void> {
  const { bundleId } = KNOWN_BROWSERS[browser];
  const { exitCode } = await runner(
    '/usr/bin/open',
    ['-b', bundleId],
    new AbortController().signal
  );
  if (exitCode !== 0) throw new Error(`open -b ${bundleId} exited with ${exitCode}.`);
}
