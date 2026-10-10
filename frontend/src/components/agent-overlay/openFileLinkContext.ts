import { createContext } from 'react';

/**
 * Where a `pantaray-file:///` link in Markdown opens. A host with a preview provides it; without
 * one (the Overlay), the link selects the file in Finder.
 */
export const OpenFileLinkContext = createContext<((path: string) => void) | null>(null);
