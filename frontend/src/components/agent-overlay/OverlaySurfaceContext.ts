import { createContext, useContext } from 'react';

/**
 * Where the Agent Overlay is rendered.
 *
 * - `panel`: the small floating panel. It sizes its window to the content and can collapse.
 * - `window`: an ordinary window (`notification.html?surface=window`). The user owns its size,
 *   and the title bar's buttons or ⌘W close it.
 */
export type OverlaySurface = 'panel' | 'window';

export const OverlaySurfaceContext = createContext<OverlaySurface>('panel');

export const useOverlaySurface = (): OverlaySurface => useContext(OverlaySurfaceContext);
