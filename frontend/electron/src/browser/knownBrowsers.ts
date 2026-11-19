/**
 * The user's browsers Pantaray may connect to, by the ids `UserBrowserIdSchema` carries. The
 * names and user-data-dirs mirror `KNOWN_USER_BROWSERS` in
 * `agents/src/pantaray_agents/schema/user_browser.py`; the rest is what Electron needs to find
 * the running browser and check it is the real one.
 */

import type { UserBrowserId } from './browserContract';

export type KnownBrowser = {
  /** What the user reads, in messages and the notice. */
  name: string;
  /** Under `~/Library/Application Support`. */
  userDataDir: string;
  /** The app's code-signing identifier; its helper is `<bundleId>.helper`. */
  bundleId: string;
  /** The Apple team that signs it. */
  teamId: string;
  /** Where the user turns remote debugging on. */
  inspectUrl: string;
};

// Google Chrome's signature on a real Mac names its team and, in its designated requirement, the
// four channels' ids. Microsoft's and Brave's values are published ones, not yet checked there.
const GOOGLE_TEAM = 'EQHXZ8M8AV';
const CHROME_INSPECT = 'chrome://inspect/#remote-debugging';

export const KNOWN_BROWSERS: Readonly<Record<UserBrowserId, KnownBrowser>> = {
  chrome: {
    name: 'Google Chrome',
    userDataDir: 'Google/Chrome',
    bundleId: 'com.google.Chrome',
    teamId: GOOGLE_TEAM,
    inspectUrl: CHROME_INSPECT,
  },
  chrome_beta: {
    name: 'Google Chrome Beta',
    userDataDir: 'Google/Chrome Beta',
    bundleId: 'com.google.Chrome.beta',
    teamId: GOOGLE_TEAM,
    inspectUrl: CHROME_INSPECT,
  },
  chrome_dev: {
    name: 'Google Chrome Dev',
    userDataDir: 'Google/Chrome Dev',
    bundleId: 'com.google.Chrome.dev',
    teamId: GOOGLE_TEAM,
    inspectUrl: CHROME_INSPECT,
  },
  chrome_canary: {
    name: 'Google Chrome Canary',
    userDataDir: 'Google/Chrome Canary',
    bundleId: 'com.google.Chrome.canary',
    teamId: GOOGLE_TEAM,
    inspectUrl: CHROME_INSPECT,
  },
  edge: {
    name: 'Microsoft Edge',
    userDataDir: 'Microsoft Edge',
    bundleId: 'com.microsoft.edgemac',
    teamId: 'UBF8T346G9',
    inspectUrl: 'edge://inspect/#remote-debugging',
  },
  brave: {
    name: 'Brave',
    userDataDir: 'BraveSoftware/Brave-Browser',
    bundleId: 'com.brave.Browser',
    teamId: 'KL8N8XSYF4',
    inspectUrl: 'brave://inspect/#remote-debugging',
  },
};
