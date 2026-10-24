import { useI18n } from '@/context/useI18n';

/** The recording on/off switch. Turning recording off never waits on `canStart`. */
export function RecordingSwitch({
  isCapturing,
  canStart,
  isProcessing,
  onChange,
  compact = false,
}: {
  /** Null until the status IPC has answered. */
  isCapturing: boolean | null;
  /** Whether recording can be turned on now. */
  canStart: boolean;
  isProcessing: boolean;
  onChange: (enabled: boolean) => unknown;
  compact?: boolean;
}) {
  const { t } = useI18n();
  const className = compact ? 'settings-toggle settings-toggle--compact' : 'settings-toggle';

  // role="switch" cannot express an unknown state, so the switch appears only once the status
  // IPC has answered; the placeholder holds its place.
  if (isCapturing === null) return <span className={className} aria-hidden="true" />;

  return (
    <button
      type="button"
      role="switch"
      aria-checked={isCapturing}
      aria-label={t('settings.screenshotCapture.screenshotsLabel')}
      className={isCapturing ? `${className} settings-toggle--on` : className}
      disabled={isProcessing || (!isCapturing && !canStart)}
      onClick={() => void onChange(!isCapturing)}
    >
      <span className="settings-toggle-thumb" />
      <span className="settings-toggle-label">
        {isCapturing ? t('common.on') : t('common.off')}
      </span>
    </button>
  );
}
