import { useAuth } from '@/hooks/useAuth';
import type { ConnectionCommand } from '../../../../electron/src/ipc/schemas/aiConnection';
import type { Translate } from '../types';
import { useAiConnectionController } from '../useAiConnectionController';
import { useIntegrationConnectFlow } from './IntegrationConnectFlow';
import { useIntegrationSettingsController } from './IntegrationSettingsController';
import { IntegrationSettingsSection } from './IntegrationSettingsSection';

/**
 * The key belongs to the installation, like the other connection settings; the services and
 * accounts belong to the local owner.
 */
export function IntegrationSettings({ t }: { t: Translate }) {
  const { runtimeState } = useAuth();
  const ownerId = runtimeState.status === 'ready' ? (runtimeState.owner?.id ?? null) : null;
  const integrations = useIntegrationSettingsController(ownerId);
  const connection = useAiConnectionController();
  const { flow, actions: flowActions } = useIntegrationConnectFlow(ownerId, integrations.reload);
  const { state, loadError, recovery } = connection;

  async function saveKey(command: ConnectionCommand): Promise<void> {
    const saved = await connection.run(command);
    // Saving or removing the key adds or removes the backend's connection method.
    await integrations.reload();
    if (!saved) throw new Error('INTEGRATION_KEY_UPDATE_FAILED');
  }

  if (loadError) {
    return (
      <div className="dashboard-section">
        <h3 className="dashboard-section-title">{t('settings.integrations.title')}</h3>
        <p role="alert" className="settings-section-error">
          {t('settings.aiConnection.loadFailed')} {t(`settings.aiConnection.error.${loadError}`)}
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
      </div>
    );
  }
  if (!state) return <p role="status">{t('settings.loadingStatus')}</p>;

  return (
    <IntegrationSettingsSection
      t={t}
      integrationKey={{
        hasSavedKey: state.settings.hasSavedIntegrationKey,
        canStoreSecrets: state.settings.canStoreSecrets,
        disabled: connection.pendingOperation !== null,
        onSave: (apiKey) => saveKey({ operation: 'save_integration_key', apiKey }),
        onClear: () => saveKey({ operation: 'remove_integration_key' }),
      }}
      toolkits={integrations.toolkits}
      loadFailed={integrations.loadFailed}
      onReload={() => void integrations.reload()}
      rows={integrations.rows}
      actions={integrations.actions}
      flow={flow}
      flowActions={flowActions}
    />
  );
}
