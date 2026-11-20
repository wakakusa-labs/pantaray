import { useI18n } from '@/context/useI18n';
import type { UserBrowserId } from '../../../electron/src/browser/browserContract';
import { KNOWN_BROWSERS } from '../../../electron/src/browser/knownBrowsers';
import './userBrowserWaitLine.css';

/**
 * Each of the user's browsers the Action waits on for the user to allow Pantaray in. Nothing
 * brings the browser forward on its own: its button does, when the user presses it.
 */
export function UserBrowserWaitLine({ browsers }: { browsers: readonly UserBrowserId[] }) {
  const { t } = useI18n();
  if (browsers.length === 0) return null;
  const open = (browser: UserBrowserId) => {
    window.electron?.actions?.openUserBrowser({ browser }).catch((error: unknown) => {
      console.error('Failed to bring the user browser forward:', error);
    });
  };
  return (
    <div className="user-browser-wait" role="status">
      {browsers.map((browser) => {
        const { name } = KNOWN_BROWSERS[browser];
        return (
          <p key={browser} className="user-browser-wait__line">
            <span>{t('overlay.userBrowserWait', { browser: name })}</span>
            <button type="button" onClick={() => open(browser)}>
              {t('overlay.userBrowserWait.open', { browser: name })}
            </button>
          </p>
        );
      })}
    </div>
  );
}
