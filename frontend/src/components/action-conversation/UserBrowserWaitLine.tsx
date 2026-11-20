import { useI18n } from '@/context/useI18n';
import type { ActionUserBrowserWait } from '../../../electron/src/actions/actionLiveCore';
import { KNOWN_BROWSERS } from '../../../electron/src/browser/knownBrowsers';
import './userBrowserWaitLine.css';

/**
 * What the Action waits on the user for in each of their browsers. Nothing brings the browser
 * forward on its own: its button does, when the user presses it.
 */
export function UserBrowserWaitLine({ waits }: { waits: readonly ActionUserBrowserWait[] }) {
  if (waits.length === 0) return null;
  return (
    <div className="user-browser-wait" role="status">
      {waits.map((wait) => (
        <WaitLine key={wait.browser} wait={wait} />
      ))}
    </div>
  );
}

function WaitLine({ wait: { browser, kind } }: { wait: ActionUserBrowserWait }) {
  const { t } = useI18n();
  const { name } = KNOWN_BROWSERS[browser];
  const open = () => {
    window.electron?.actions?.openUserBrowser({ browser, kind }).catch((error: unknown) => {
      console.error('Failed to bring the user browser forward:', error);
    });
  };
  return (
    <div className="user-browser-wait__line">
      <p>
        {t(
          kind === 'allow'
            ? 'overlay.userBrowserWait.allow'
            : 'overlay.userBrowserWait.debuggingOff',
          { browser: name }
        )}
      </p>
      <button type="button" onClick={open}>
        {t(
          kind === 'allow'
            ? 'overlay.userBrowserWait.open'
            : 'overlay.userBrowserWait.openSettings',
          { browser: name }
        )}
      </button>
    </div>
  );
}
