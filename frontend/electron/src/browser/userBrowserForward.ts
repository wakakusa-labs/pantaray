import type { UserBrowserId } from './browserContract';
import { KNOWN_BROWSERS } from './knownBrowsers';
import type { CommandRunner, UserBrowserWaitKind } from './userBrowserConnection';

/**
 * Brings the user's browser to the front, on its remote debugging settings while that is what the
 * user is asked to turn on. Only the user's own press calls this: nothing takes focus on its own.
 */
export async function openUserBrowser(
  browser: UserBrowserId,
  kind: UserBrowserWaitKind,
  runner: CommandRunner
): Promise<void> {
  const { bundleId, inspectUrl } = KNOWN_BROWSERS[browser];
  const args = kind === 'debugging_off' ? ['-b', bundleId, inspectUrl] : ['-b', bundleId];
  const { exitCode } = await runner('/usr/bin/open', args, new AbortController().signal);
  if (exitCode !== 0) throw new Error(`open ${args.join(' ')} exited with ${exitCode}.`);
}
