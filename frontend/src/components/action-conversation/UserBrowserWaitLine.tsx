import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';
import { useI18n } from '@/context/useI18n';
import type { ActionUserBrowserWait } from '../../../electron/src/actions/actionLiveCore';
import { KNOWN_BROWSERS } from '../../../electron/src/browser/knownBrowsers';
import './userBrowserWaitLine.css';

const COPY_LABEL = {
  idle: 'overlay.userBrowserWait.copy',
  copied: 'overlay.userBrowserWait.copied',
  failed: 'overlay.userBrowserWait.copyFailed',
} as const;

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
  const { status, copy } = useClipboardCopy();
  // A browser page such as chrome://inspect opens only from the browser's own address bar.
  const { name, inspectUrl } = KNOWN_BROWSERS[browser];
  const open = () => {
    window.electron?.actions?.openUserBrowser({ browser }).catch((error: unknown) => {
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
        {t('overlay.userBrowserWait.open', { browser: name })}
      </button>
      {kind === 'debugging_off' ? (
        <div className="user-browser-wait__address-row">
          <code className="user-browser-wait__address">{inspectUrl}</code>
          <button type="button" onClick={() => void copy(() => inspectUrl)}>
            {t(COPY_LABEL[status])}
          </button>
        </div>
      ) : null}
    </div>
  );
}
