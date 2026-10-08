import { Plus } from 'lucide-react';

import { ariaKeyShortcuts, acceleratorKeycaps } from '@/components/shortcut/acceleratorKeycaps';
import { ShortcutKeycaps } from '@/components/shortcut/ShortcutHint';
import type { ShortcutHintState } from '@/components/shortcut/useGlobalShortcutHint';
import { NEW_WORK_BUTTON_ID } from '@/history/newWork';
import type { MessageKey } from '@/i18n/types';

const SHORTCUT_UNAVAILABLE_ID = 'history-new-conversation-shortcut-unavailable';

/**
 * The global shortcut opens the same Overlay, so it sits inside the button the way menus show
 * shortcuts. The keycaps are drawn only; assistive technology gets `aria-keyshortcuts`.
 */
export function NewWorkButton({
  shortcutHint,
  t,
  onClick,
}: {
  shortcutHint: ShortcutHintState;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  onClick: () => void;
}) {
  const isUnavailable = shortcutHint.status === 'unavailable';
  const accelerator = shortcutHint.status === 'ready' ? shortcutHint.accelerator : null;
  return (
    <>
      <button
        type="button"
        id={NEW_WORK_BUTTON_ID}
        className="history-new-conversation-button"
        aria-keyshortcuts={
          accelerator === null
            ? undefined
            : ariaKeyShortcuts(
                acceleratorKeycaps(accelerator, window.electron?.process.platform === 'darwin')
              )
        }
        title={isUnavailable ? t('shortcut.hint.unavailable') : undefined}
        aria-describedby={isUnavailable ? SHORTCUT_UNAVAILABLE_ID : undefined}
        onClick={onClick}
      >
        <Plus size={16} aria-hidden="true" />
        {t('history.newConversation')}
        {accelerator === null ? null : (
          <span className="history-new-conversation-keys" aria-hidden="true">
            <ShortcutKeycaps accelerator={accelerator} t={t} />
          </span>
        )}
      </button>
      {isUnavailable ? (
        <span id={SHORTCUT_UNAVAILABLE_ID} hidden>
          {t('shortcut.hint.unavailable')}
        </span>
      ) : null}
    </>
  );
}
