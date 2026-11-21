import { useContext } from 'react';

import { useI18n } from '@/context/useI18n';

import { WatchBrowserContext } from './watchBrowserContext';

/**
 * Opens the view of the app's page the Action works on, while the host offers it. It sits beside
 * a row's or a section's own toggle, never inside it.
 */
export function WatchBrowserButton() {
  const { t } = useI18n();
  const watchBrowser = useContext(WatchBrowserContext);
  if (watchBrowser === null) return null;
  return (
    <button type="button" className="action-conversation__watch-browser" onClick={watchBrowser}>
      {t('overlay.browserView.open')}
    </button>
  );
}
