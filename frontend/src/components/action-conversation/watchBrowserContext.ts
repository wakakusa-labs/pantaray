import { createContext } from 'react';

/**
 * Opens the view of the app's page the Action works on. A host with a preview provides it while
 * that page is live; without one (the Overlay), or with no live page, browser rows offer nothing.
 */
export const WatchBrowserContext = createContext<(() => void) | null>(null);
