import { historyModeButtonId, type HistoryViewMode } from '@/history/historyViewMode';
import type { MessageKey } from '@/i18n/types';
import { useChatUnreadCount } from '@/components/chat/chatUnread';

const MODES: readonly { mode: HistoryViewMode; labelKey: MessageKey }[] = [
  { mode: 'chat', labelKey: 'history.mode.chat' },
  { mode: 'list', labelKey: 'history.mode.list' },
];

const MAX_SHOWN_COUNT = 99;

/** Two pressed-state buttons: each names a view, and the pressed one is shown. */
export function HistoryModeSwitch({
  mode,
  t,
  onChange,
}: {
  mode: HistoryViewMode;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  onChange: (mode: HistoryViewMode) => void;
}) {
  const unread = useChatUnreadCount();
  return (
    <div className="history-mode-switch" role="group" aria-label={t('history.mode.label')}>
      {MODES.map((option) => {
        const count = option.mode === 'chat' ? unread : 0;
        return (
          <button
            key={option.mode}
            type="button"
            id={historyModeButtonId(option.mode)}
            className="history-mode-switch__button"
            aria-pressed={option.mode === mode}
            aria-describedby={count > 0 ? 'history-mode-chat-unread' : undefined}
            onClick={() => {
              if (option.mode !== mode) onChange(option.mode);
            }}
          >
            {t(option.labelKey)}
            {count > 0 ? (
              <span className="history-mode-switch__count" aria-hidden="true">
                {count > MAX_SHOWN_COUNT ? `${MAX_SHOWN_COUNT}+` : count}
              </span>
            ) : null}
          </button>
        );
      })}
      {unread > 0 ? (
        <span id="history-mode-chat-unread" hidden>
          {t('history.chat.unread', { count: unread })}
        </span>
      ) : null}
    </div>
  );
}
