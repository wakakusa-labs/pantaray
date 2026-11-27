import { useI18n } from '@/context/useI18n';

/** A schedule's on/off switch, drawn as Settings draws its switches. */
export function ScheduleSwitch({
  on,
  describedBy,
  disabled,
  onChange,
}: {
  on: boolean;
  /** The schedule's own words, which tell apart the switches where several are shown. */
  describedBy: string;
  disabled: boolean;
  onChange: (on: boolean) => void;
}) {
  const { t } = useI18n();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={t('schedules.switch')}
      aria-describedby={describedBy}
      className={`settings-toggle settings-toggle--fixed${on ? ' settings-toggle--on' : ''}`}
      disabled={disabled}
      onClick={() => onChange(!on)}
    >
      <span className="settings-toggle-thumb" />
      <span className="settings-toggle-label">{t(on ? 'common.on' : 'common.off')}</span>
    </button>
  );
}
