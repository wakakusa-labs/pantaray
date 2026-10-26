import type { ConnectionErrorCode } from '../../../../electron/src/ipc/schemas/aiConnection';
import type { Translate } from '../types';
import type { AiConnectionController } from '../useAiConnectionController';

/** The connection settings could not be read: why, a retry, and main's recovery when it has one. */
export function ConnectionLoadFailure({
  t,
  connection,
  error,
}: {
  t: Translate;
  connection: AiConnectionController;
  error: ConnectionErrorCode;
}) {
  const { recovery } = connection;
  return (
    <>
      <p role="alert" className="settings-section-error">
        {t('settings.aiConnection.loadFailed')} {t(`settings.aiConnection.error.${error}`)}
      </p>
      <button
        type="button"
        className="settings-action-button"
        onClick={() => void connection.refresh()}
      >
        {t('settings.aiConnection.retry')}
      </button>
      {recovery ? (
        <button
          type="button"
          className="settings-text-button"
          disabled={connection.pendingOperation !== null}
          onClick={() => void connection.run(recovery)}
        >
          {t(
            `settings.aiConnection.recovery.${recovery.operation}`,
            recovery.operation === 'remove_api_key'
              ? { provider: t(`settings.aiConnection.provider.${recovery.provider}`) }
              : undefined
          )}
        </button>
      ) : null}
    </>
  );
}
