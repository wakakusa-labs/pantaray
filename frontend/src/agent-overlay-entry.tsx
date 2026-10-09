import React from 'react';
import ReactDOM from 'react-dom/client';
import AgentOverlay from './components/AgentOverlay';
import {
  OverlaySurfaceContext,
  type OverlaySurface,
} from './components/agent-overlay/OverlaySurfaceContext';
import { UiLanguageProvider } from './context/UiLanguageContext';
import { resolveRendererInitialLanguage } from './i18n/initialLanguage';
import './index.css';

type OverlayEntry = {
  mode: 'overlay' | 'standalone';
  actionId: string | null;
  surface: OverlaySurface;
};

function getEntry(): OverlayEntry {
  try {
    const url = new URL(window.location.href);
    const mode = url.searchParams.get('mode');
    const actionId = url.searchParams.get('actionId');
    return {
      mode: mode === 'standalone' ? mode : 'overlay',
      actionId: actionId?.trim() || null,
      surface: url.searchParams.get('surface') === 'window' ? 'window' : 'panel',
    };
  } catch {
    return { mode: 'overlay', actionId: null, surface: 'panel' };
  }
}

const initialLanguage = resolveRendererInitialLanguage();
const entry = getEntry();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <UiLanguageProvider initialLanguage={initialLanguage}>
      <OverlaySurfaceContext.Provider value={entry.surface}>
        <AgentOverlay entryMode={entry.mode} initialActionId={entry.actionId} />
      </OverlaySurfaceContext.Provider>
    </UiLanguageProvider>
  </React.StrictMode>
);
