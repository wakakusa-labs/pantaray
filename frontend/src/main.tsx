import React from 'react';
import { clearConversationScrollPositions } from './components/agent-overlay/conversationScrollPosition';
import ReactDOM from 'react-dom/client';
import App from './App.tsx';
import './index.css';
import './iconTooltip.css';
import { AuthProvider } from './context/AuthContext';
import { UiLanguageProvider } from './context/UiLanguageContext';
import { resolveRendererInitialLanguage } from './i18n/initialLanguage';

// Auth changes destroy the Overlay windows before publishing reset. The main
// window remains alive (hidden on macOS close), so it owns shared-storage cleanup.
window.electron?.actions?.onConversationUpdated((update) => {
  if (update.kind === 'reset') clearConversationScrollPositions();
});

const initialLanguage = resolveRendererInitialLanguage();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <UiLanguageProvider initialLanguage={initialLanguage}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </UiLanguageProvider>
  </React.StrictMode>
);
