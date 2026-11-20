import { Check, CircleAlert, Clipboard } from 'lucide-react';

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
  const copyLabel = t(COPY_LABEL[status]);
  return (
    <div className={`user-browser-wait__line user-browser-wait__line--${kind}`}>
      <p>
        {t(
          kind === 'allow'
            ? 'overlay.userBrowserWait.allow'
            : 'overlay.userBrowserWait.debuggingOff',
          { browser: name }
        )}
      </p>
      {kind === 'debugging_off' ? (
        <span className="user-browser-wait__address">
          <code>{inspectUrl}</code>
          <button
            type="button"
            className="user-browser-wait__copy"
            aria-label={copyLabel}
            data-tooltip={copyLabel}
            onClick={() => void copy(() => inspectUrl)}
          >
            {status === 'copied' ? (
              <Check size={15} strokeWidth={1.8} aria-hidden />
            ) : status === 'failed' ? (
              <CircleAlert size={15} strokeWidth={1.8} aria-hidden />
            ) : (
              <Clipboard size={15} strokeWidth={1.8} aria-hidden />
            )}
          </button>
        </span>
      ) : null}
      <button type="button" className="user-browser-wait__open" onClick={open}>
        {t('overlay.userBrowserWait.open', { browser: name })}
      </button>
    </div>
  );
}
