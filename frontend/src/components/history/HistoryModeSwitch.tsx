import { historyModeButtonId, type HistoryViewMode } from '@/history/historyViewMode';
import type { MessageKey } from '@/i18n/types';

const MODES: readonly { mode: HistoryViewMode; labelKey: MessageKey }[] = [
  { mode: 'chat', labelKey: 'history.mode.chat' },
  { mode: 'list', labelKey: 'history.mode.list' },
];

/** Two pressed-state buttons: each names a view, and the pressed one is shown. */
export function HistoryModeSwitch({
  mode,
  t,
  onChange,
}: {
  mode: HistoryViewMode;
  t: (key: MessageKey) => string;
  onChange: (mode: HistoryViewMode) => void;
}) {
  return (
    <div className="history-mode-switch" role="group" aria-label={t('history.mode.label')}>
      {MODES.map((option) => (
        <button
          key={option.mode}
          type="button"
          id={historyModeButtonId(option.mode)}
          className="history-mode-switch__button"
          aria-pressed={option.mode === mode}
          onClick={() => {
            if (option.mode !== mode) onChange(option.mode);
          }}
        >
          {t(option.labelKey)}
        </button>
      ))}
    </div>
  );
}
